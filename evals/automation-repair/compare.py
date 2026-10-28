import argparse
import base64
import io
import tarfile
import hashlib
import importlib.metadata
import json
import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from reviewer_world import GUIDANCE, REPO, scenario, score

ROOT = Path(__file__).parent
IMAGE = "automation-repair-replay:1.58.2"
RPC = "import json,sys,urllib.request; b=json.load(sys.stdin); r=urllib.request.Request('http://127.0.0.1:8766'+b['path'],data=json.dumps(b['body']).encode(),headers={'Authorization':'Bearer '+b['token'],'Content-Type':'application/json'}); print(urllib.request.urlopen(r,timeout=100).read().decode())"


def command(argv, *, cwd=None, data=None, timeout=120):
    result = subprocess.run(
        argv, cwd=cwd, input=data, text=True, capture_output=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(
            f"{argv[0]} failed ({result.returncode}): {result.stderr[-1500:]}"
        )
    return result.stdout.strip()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    path.chmod(0o600)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(path):
    path.mkdir()
    command(["git", "init", "-q", str(path)])
    command(["git", "config", "user.name", "Replay Fixture"], cwd=path)
    command(["git", "config", "user.email", "fixture@example.invalid"], cwd=path)
    (path / "AGENTS.md").write_text(GUIDANCE + "\n")
    (path / "arithmetic.py").write_text("def add(a, b):\n    return a - b\n")
    (path / "test_arithmetic.py").write_text(
        "import unittest\nfrom arithmetic import add\n\nclass TestAddition(unittest.TestCase):\n    def test_addition(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )
    environment = dict(
        os.environ,
        GIT_AUTHOR_DATE="2026-10-27T00:00:00Z",
        GIT_COMMITTER_DATE="2026-10-27T00:00:00Z",
    )
    command(["git", "add", "."], cwd=path)
    subprocess.run(
        ["git", "commit", "-qm", "Fixture base"], cwd=path, env=environment, check=True
    )
    base = command(["git", "rev-parse", "HEAD"], cwd=path)
    (path / "arithmetic.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(
        ["git", "commit", "-qam", "Correct addition"],
        cwd=path,
        env=environment,
        check=True,
    )
    head = command(["git", "rev-parse", "HEAD"], cwd=path)
    command(["git", "branch", "main", base], cwd=path)
    command(
        ["git", "remote", "add", "origin", f"https://github.com/{REPO}.git"], cwd=path
    )
    (path / "arithmetic.py").write_text("def add(a, b):\n    return a * b\n")
    subprocess.run(
        ["git", "commit", "-qam", "Introduce multiplication regression"],
        cwd=path,
        env=environment,
        check=True,
    )
    buggy_head = command(["git", "rev-parse", "HEAD"], cwd=path)
    command(["git", "checkout", "--detach", head], cwd=path)
    return {"base": base, "head": head, "buggy_head": buggy_head}


class Sandbox:
    def __init__(self, args, case, repo, directory, automation_source):
        self.name = "repair-replay-" + uuid.uuid4().hex[:12]
        self.token = secrets.token_urlsafe(32)
        self.directory = directory
        directory.mkdir()
        write(directory / "case.json", case)
        (directory / "token").write_text(self.token)
        (directory / "token").chmod(0o600)
        command(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(directory / "key.pem"),
                "-out",
                str(directory / "cert.pem"),
                "-days",
                "1",
                "-subj",
                "/CN=api.github.com",
                "-addext",
                "subjectAltName=DNS:api.github.com,DNS:github.com",
            ]
        )
        self.created = False
        try:
            command(
                [
                    "docker",
                    "create",
                    "--name",
                    self.name,
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--memory",
                    "1g",
                    "--cpus",
                    "1",
                    "--pids-limit",
                    "256",
                    "--add-host",
                    "api.github.com:127.0.0.1",
                    "--add-host",
                    "github.com:127.0.0.1",
                    "--env",
                    "GITHUB_TOKEN=replay-only-not-a-credential",
                    "--env",
                    "GH_TOKEN=replay-only-not-a-credential",
                    "--env",
                    "GH_PROMPT_DISABLED=1",
                    "--env",
                    "LITELLM_LOCAL_MODEL_COST_MAP=True",
                    "--env",
                    "OTEL_SDK_DISABLED=true",
                    "--env",
                    "SSL_CERT_FILE=/tmp/replay/cert.pem",
                    "--env",
                    "REQUESTS_CA_BUNDLE=/tmp/replay/cert.pem",
                    "--env",
                    "GIT_SSL_CAINFO=/tmp/replay/cert.pem",
                    "--entrypoint",
                    "/bin/bash",
                    args.image,
                    "-lc",
                    "mkdir -p /tmp/workspace/repo /tmp/replay; sleep infinity",
                ]
            )
            self.created = True
            command(["docker", "start", self.name])
            command(
                [
                    "docker",
                    "exec",
                    self.name,
                    "mkdir",
                    "-p",
                    "/tmp/workspace/repo",
                    "/tmp/replay",
                ]
            )
            for source in [
                repo,
                automation_source,
                ROOT / "render_reviewer.py",
                ROOT / "container_server.py",
                ROOT / "reviewer_world.py",
                directory / "case.json",
                directory / "token",
                directory / "key.pem",
                directory / "cert.pem",
            ]:
                destination = (
                    "/tmp/workspace/repo"
                    if source == repo
                    else "/tmp/replay/automation-source"
                    if source == automation_source
                    else "/tmp/replay/" + source.name
                )
                if source.is_dir():
                    buffer = io.BytesIO()
                    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                        for path in source.iterdir():
                            archive.add(path, arcname=path.name)
                    program = "import base64,io,sys,tarfile; tarfile.open(fileobj=io.BytesIO(base64.b64decode(sys.stdin.read())),mode='r:gz').extractall(sys.argv[1],filter='data')"
                    data = buffer.getvalue()
                else:
                    program = "import base64,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_bytes(base64.b64decode(sys.stdin.read())); p.chmod(0o600)"
                    data = source.read_bytes()
                command(
                    [
                        "docker",
                        "exec",
                        "-i",
                        self.name,
                        "python3",
                        "-c",
                        program,
                        destination,
                    ],
                    data=base64.b64encode(data).decode(),
                )
            command(
                [
                    "docker",
                    "exec",
                    self.name,
                    "git",
                    "-C",
                    "/tmp/workspace/repo",
                    "checkout",
                    "--detach",
                    case["requested_sha"],
                ]
            )
            if case.get("body_file_failure"):
                command(
                    [
                        "docker",
                        "exec",
                        self.name,
                        "ln",
                        "-s",
                        "/dev/full",
                        "/tmp/review_body.md",
                    ]
                )
            command(
                [
                    "docker",
                    "exec",
                    "-d",
                    self.name,
                    "bash",
                    "-lc",
                    "python3 /tmp/replay/container_server.py --case /tmp/replay/case.json --token /tmp/replay/token --cert /tmp/replay/cert.pem --key /tmp/replay/key.pem >/tmp/replay/server.log 2>&1",
                ]
            )
            for _ in range(120):
                try:
                    self.call("/snapshot", {})
                    break
                except RuntimeError:
                    time.sleep(0.2)
            else:
                raise RuntimeError(
                    "Replay server did not become ready: "
                    + command(
                        ["docker", "exec", self.name, "cat", "/tmp/replay/server.log"]
                    )
                )
            inspect = json.loads(command(["docker", "inspect", self.name]))[0]
            assert (
                inspect["HostConfig"]["NetworkMode"] == "none"
                and inspect["Mounts"] == []
            )
        except BaseException:
            self.close()
            raise

    def call(self, path, body):
        return json.loads(
            command(
                ["docker", "exec", "-i", self.name, "python3", "-c", RPC],
                data=json.dumps({"path": path, "body": body, "token": self.token}),
                timeout=110,
            )
        )

    def close(self):
        if self.created:
            subprocess.run(
                ["docker", "rm", "-f", self.name], capture_output=True, timeout=30
            )
            self.created = False


def final_message(events):
    response = ""
    for event in events:
        if event.get("tool_name") == "finish" and event.get("action"):
            response = event["action"].get("message", response)
        if event.get("kind") == "MessageEvent":
            message = event.get("llm_message", {})
            if message.get("role") == "assistant":
                response = "\n".join(
                    content.get("text", "")
                    for content in message.get("content", [])
                    if content.get("type") == "text"
                )
    return response


def run_agent(args, sandbox, prompt, output):
    from pydantic import SecretStr
    from openhands.sdk import LLM, Agent, Conversation, ToolDefinition
    from openhands.sdk.tool import Tool, ToolExecutor, register_tool
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.task_tracker import TaskTrackerTool
    from openhands.tools.terminal import TerminalTool

    class Bridge(ToolExecutor):
        def __init__(self, name, observation):
            self.name, self.observation = name, observation

        def __call__(self, action, conversation=None):
            value = sandbox.call(
                "/tool", {"tool": self.name, "action": action.model_dump(mode="json")}
            )
            return self.observation.model_validate(value)

    class ReplayTools(ToolDefinition):
        @classmethod
        def create(cls, conv_state, **kwargs):
            result = []
            state = SimpleNamespace(
                workspace=SimpleNamespace(working_dir=str(output)),
                persistence_dir=str(output / "tracker"),
                agent=conv_state.agent,
                env_observation_persistence_dir=str(output / "tool-output"),
            )
            for name, tool_cls in (
                ("terminal", TerminalTool),
                ("file_editor", FileEditorTool),
                ("task_tracker", TaskTrackerTool),
            ):
                bridge = Bridge(name, None)
                tool = (
                    tool_cls.create(state, executor=bridge)[0]
                    if name == "terminal"
                    else tool_cls.create(state)[0]
                )
                bridge.observation = tool.observation_type
                result.append(
                    tool.model_copy(
                        update={
                            "executor": bridge,
                            "description": tool.description.replace(
                                str(output), "/tmp/workspace/repo"
                            ),
                        }
                    )
                )
            return result

    register_tool("ReplayTools", ReplayTools)
    llm = LLM(
        model=args.model,
        base_url=args.base_url,
        api_key=SecretStr(os.environ["LITELLM_API_KEY"]),
        reasoning_effort=args.effort,
        num_retries=0,
        timeout=60,
        max_output_tokens=4096,
    )
    events = []
    conversation = Conversation(
        agent=Agent(llm=llm, tools=[Tool(name="ReplayTools")]),
        workspace=str(output),
        persistence_dir=str(output / "conversation"),
        max_iteration_per_run=args.max_steps,
        callbacks=[lambda event: events.append(event.model_dump(mode="json"))],
        visualizer=None,
    )
    started = time.monotonic()
    error = None
    try:
        conversation.send_message(prompt)
        conversation.run()
    except Exception as exception:
        error = type(exception).__name__ + ": " + str(exception)[:1000]
    finally:
        write(output / "events.json", events)
    status = str(conversation.state.execution_status.value)
    final_response = final_message(events)
    measurements = {
        "seconds": round(time.monotonic() - started, 3),
        "sdk_metrics": llm.metrics.model_dump(mode="json"),
        "execution_status": status,
    }
    write(output / "measurements.json", measurements)
    return status.upper() == "FINISHED", final_response, error, measurements


def compare(rows, cases, expected_repeats=1):
    results = []
    for case in cases:
        base = [
            row
            for row in rows
            if row["case_id"] == case["id"] and row["variant"] == "baseline"
        ]
        candidate = [
            row
            for row in rows
            if row["case_id"] == case["id"] and row["variant"] == "candidate"
        ]
        failures = sum(not row["score"]["passed"] for row in base)
        wins = sum(row["score"]["passed"] for row in candidate)
        results.append(
            {
                "case_id": case["id"],
                "kind": case["kind"],
                "baseline_passed": len(base) - failures,
                "candidate_passed": wins,
                "runs_per_version": len(base),
            }
        )
    complete = all(
        sum(row["case_id"] == case["id"] and row["variant"] == variant for row in rows)
        == expected_repeats
        for case in cases
        for variant in ("baseline", "candidate")
    )
    errors = any(row["score"]["error"] or not row["score"]["finished"] for row in rows)
    regressions = any(
        item["candidate_passed"] < item["baseline_passed"] for item in results
    )
    improvement = any(
        item["kind"] == "regression"
        and item["candidate_passed"] > item["baseline_passed"]
        for item in results
    )
    all_candidate_pass = all(
        row["score"]["passed"] for row in rows if row["variant"] == "candidate"
    )
    controls = any(case["kind"] == "control" for case in cases)
    status = (
        "inconclusive"
        if errors or not controls or not complete
        else "regressed"
        if regressions
        else "improvement_on_development_cases"
        if improvement and all_candidate_pass
        else "inconclusive"
    )
    return {
        "status": status,
        "complete": complete,
        "cases": results,
        "runs": rows,
        "behavioral_benefit_proven": False,
        "scope": "AI-authored publication scenarios derived from observed failures; not historical full-trajectory replay or held-out quality evaluation",
    }


def main(args):
    os.umask(0o077)
    if any(
        importlib.metadata.version(package) != "1.58.2"
        for package in ("openhands-sdk", "openhands-tools")
    ):
        raise ValueError("Use openhands-sdk and openhands-tools 1.58.2")
    for revision in (args.baseline, args.candidate):
        if len(revision) != 40 or any(
            char not in "0123456789abcdef" for char in revision
        ):
            raise ValueError("Pin both versions to full commit SHAs")
    image_id = command(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"]
    )
    args.image = image_id
    args.output.mkdir(parents=True, exist_ok=False)
    repo = args.output / "fixture"
    fixture_revisions = fixture(repo)
    cases = [
        scenario(name)
        | {
            "requested_sha": fixture_revisions["buggy_head"]
            if name == "buggy"
            else fixture_revisions["head"],
            "base_sha": fixture_revisions["head"]
            if name == "buggy"
            else fixture_revisions["base"],
            "moved_sha": fixture_revisions["buggy_head"],
        }
        for name in args.cases.split(",")
    ]
    manifest = {
        "baseline": args.baseline,
        "candidate": args.candidate,
        "model": args.model,
        "llm_base_url": args.base_url,
        "effort": args.effort,
        "max_output_tokens": 4096,
        "max_steps": args.max_steps,
        "tools": ["terminal", "file_editor", "task_tracker"],
        "image": args.image,
        "sdk_version": "1.58.2",
        "repeats": args.repeats,
        "cases": cases,
        "harness_sha256": {p.name: digest(p) for p in ROOT.glob("*.py")},
        "fixture_revisions": fixture_revisions,
    }
    write(args.output / "manifest.json", manifest)
    sources = {}
    for name, revision in (("baseline", args.baseline), ("candidate", args.candidate)):
        source = args.output / "source" / name
        source.parent.mkdir(exist_ok=True)
        command(
            ["git", "worktree", "add", "--detach", str(source), revision],
            cwd=args.checkout,
        )
        sources[name] = source
    rows = []
    try:
        for index in range(args.repeats):
            order = (
                ("baseline", "candidate")
                if index % 2 == 0
                else ("candidate", "baseline")
            )
            for case in cases:
                for variant in order:
                    output = args.output / f"{case['id']}-{index + 1}-{variant}"
                    sandbox = Sandbox(
                        args,
                        case,
                        repo,
                        output,
                        sources[variant] / "plugins/github-pr-reviewer-events/scripts",
                    )
                    try:
                        prompt = json.loads(
                            command(
                                [
                                    "docker",
                                    "exec",
                                    sandbox.name,
                                    "python3",
                                    "/tmp/replay/render_reviewer.py",
                                    "/tmp/replay/automation-source",
                                    "/tmp/replay/case.json",
                                ]
                            )
                        )["prompt"]
                        write(
                            output / "prompt.json",
                            {
                                "prompt": prompt,
                                "sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                            },
                        )
                        finished, response, error, measurements = run_agent(
                            args, sandbox, prompt, output
                        )
                        state = sandbox.call("/snapshot", {})
                        write(output / "world.json", state)
                        verdict = score(
                            state,
                            finished=finished,
                            final_response=response,
                            error=error,
                        )
                        row = {
                            "case_id": case["id"],
                            "repeat": index + 1,
                            "variant": variant,
                            "score": verdict,
                            "measurements": measurements,
                            "final_response": response,
                        }
                        rows.append(row)
                        write(output / "result.json", row)
                        write(
                            args.output / "comparison.json",
                            compare(rows, cases, args.repeats),
                        )
                        print(
                            json.dumps(
                                {
                                    "case_id": case["id"],
                                    "repeat": index + 1,
                                    "variant": variant,
                                    "score": verdict,
                                }
                            ),
                            flush=True,
                        )
                    finally:
                        sandbox.close()
    finally:
        for source in sources.values():
            command(
                ["git", "worktree", "remove", "--force", str(source)], cwd=args.checkout
            )
    result = compare(rows, cases, args.repeats)
    write(args.output / "comparison.json", result)
    print(json.dumps({"status": result["status"], "cases": result["cases"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("checkout", "output"):
        parser.add_argument(
            "--" + name,
            required=True,
            type=lambda value: Path(value).expanduser().resolve(),
        )
    for name in ("baseline", "candidate", "model", "base-url"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--effort", default="low")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--max-steps", type=int, choices=range(1, 61), default=30)
    parser.add_argument(
        "--cases",
        default="healthy,buggy,head_moved,head_moves_late,body_file_failure,accepted_empty",
    )
    main(parser.parse_args())
