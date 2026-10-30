import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from test_automation import arguments, command, fix_report, load_source, PATCH
from test_automation import source as source_fixture
from budget import RunBudget
import coding_agent
import patches

source = source_fixture


def install_sdk(monkeypatch, execute):
    monkeypatch.setattr(coding_agent, "version", lambda name: "1.56.0")
    captured = {}
    agent = SimpleNamespace(
        tools=[
            SimpleNamespace(name=name)
            for name in ("terminal", "file_editor", "browser", "task")
        ]
    )

    class Conversation:
        def __init__(self, **kwargs):
            assert "max_budget_per_run" not in kwargs
            captured["conversation"] = kwargs
            captured["instance"] = self
            self.state = SimpleNamespace(
                execution_status=SimpleNamespace(value="finished")
            )
            self.id = "native-sdk-conversation"
            self.conversation_stats = SimpleNamespace(
                get_combined_metrics=lambda: SimpleNamespace(accumulated_cost=0.12)
            )

        def send_message(self, prompt):
            captured["prompt"] = prompt

        def run(self):
            execute(Path(captured["conversation"]["workspace"]))

        def close(self):
            captured["closed"] = True

    modules = {
        "openhands": {},
        "openhands.sdk": {
            "Conversation": Conversation,
            "LLM": lambda **kwargs: captured.update(llm=kwargs) or object(),
        },
        "openhands.sdk.tool": {"Tool": lambda name: SimpleNamespace(name=name)},
        "openhands.tools": {},
        "openhands.tools.preset": {},
        "openhands.tools.preset.default": {
            "get_default_agent": lambda **kwargs: agent,
            "register_builtins_agents": lambda: None,
        },
        "openhands.tools.task": {"TaskToolSet": SimpleNamespace(name="task")},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "private-test-key")
    return captured


def test_native_agent_captures_actual_large_multifile_diff_and_budget(
    source, tmp_path, monkeypatch
):
    checkout, path, config = source
    config["runtime"] = {"automation": {"timeout": 1200, "trigger": {"type": "event"}}}
    path.write_text(json.dumps(config))
    output = tmp_path / "repair"
    output.mkdir()

    def execute(worktree):
        (worktree / "value.py").write_text("VALUE = 4\n")
        for index in range(6):
            (worktree / f"module_{index}.py").write_text(
                "DATA = " + repr("x" * 5000) + "\n"
            )
        (output / "coding-agent/result.json").write_text(
            json.dumps({"proposals": [{"title": "Real edits"}], "rejections": []})
        )

    captured = install_sdk(monkeypatch, execute)
    args = arguments(output, source_config=path, coordinator="openai/gpt-6.1-sol")
    args.budget = RunBudget({"max_inference_cost_usd": 8, "max_run_seconds": 100})
    result, measurement, patch = coding_agent.coordinate(
        args, {"findings": []}, {"source": load_source(path)}
    )
    assert result["proposals"]
    assert patch.count("diff --git") == 7 and len(patch) > 20000
    assert captured["llm"]["model"] == "openai/gpt-6.1-sol"
    assert captured["instance"].max_budget_per_run == 8
    assert measurement["tools"] == ["terminal", "file_editor", "browser", "task"]
    brief = json.loads((output / "coding-agent/context.json").read_text())
    assert brief["snapshot"]["source"]["runtime"] == config["runtime"]
    assert args.budget.cost == 0.12 and captured["closed"]
    assert (checkout / "value.py").read_text() == "VALUE = 3\n"
    assert not (output / "coding-worktree").exists()
    assert (output / "coding-agent/events.json").stat().st_mode & 0o077 == 0


def test_sdk_initialization_failure_cleans_worktree(source, tmp_path, monkeypatch):
    checkout, path, _ = source
    captured = install_sdk(monkeypatch, lambda _: None)
    module = importlib.import_module("openhands.tools.preset.default")

    def fail(**kwargs):
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(module, "get_default_agent", fail)
    args = arguments(tmp_path / "repair", source_config=path)
    args.output.mkdir()
    with pytest.raises(RuntimeError, match="initialization failed"):
        coding_agent.coordinate(args, {}, {"source": load_source(path)})
    assert not (args.output / "coding-worktree").exists()
    assert (
        command(["git", "worktree", "list", "--porcelain"], checkout).count("worktree ")
        == 1
    )
    assert "closed" not in captured


@pytest.mark.parametrize(
    "cause", ["workflow", "configuration", "infrastructure", "provider", "unknown"]
)
def test_identical_new_tests_execute_production_code_on_both_revisions(
    source, tmp_path, cause
):
    _, path, config = source
    config["editable_roots"] = ["tests/"]
    path.write_text(json.dumps(config))
    extra = 'diff --git a/tests/test_value.py b/tests/test_value.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_value.py\n@@ -0,0 +1,3 @@\n+import runpy\n+value = runpy.run_path("value.py")["VALUE"]\n+assert value == 4\n'
    report = fix_report(PATCH + extra)
    report["findings"][0]["cause"] = cause
    report["proposals"][0]["checks"] = [
        {
            "name": "target",
            "argv": [sys.executable, "tests/test_value.py"],
            "baseline": "fail",
        },
        config["checks"][1],
    ]
    args = arguments(tmp_path / "repair", source_config=path)
    args.output.mkdir()
    result = patches.propose_fix(args, report, {"source": load_source(path)})
    assert [row["exit_code"] for row in result["baseline"]] == [1, 0]
    assert [row["exit_code"] for row in result["candidate"]] == [0, 0]
    assert "AssertionError" in result["baseline"][0]["excerpt"]
    assert result["test_assets_sha256"]["tests/test_value.py"]


def test_dangling_symlink_is_rejected_before_checks(source, tmp_path, monkeypatch):
    _, path, config = source
    config["editable_roots"] = ["tests/"]
    path.write_text(json.dumps(config))
    patch = "diff --git a/tests/link b/tests/link\nnew file mode 120000\n--- /dev/null\n+++ b/tests/link\n@@ -0,0 +1 @@\n+missing\n"
    monkeypatch.setattr(
        patches, "run_checks", lambda *a: pytest.fail("must not execute")
    )
    args = arguments(tmp_path / "repair", source_config=path)
    args.output.mkdir()
    with pytest.raises(ValueError, match="regular source files"):
        patches.propose_fix(args, fix_report(patch), {"source": load_source(path)})


def test_known_credentials_block_public_patch(monkeypatch):
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "private-project-credential-value")
    with pytest.raises(ValueError, match="credential"):
        patches.validate_public_patch("+TOKEN = 'private-project-credential-value'")
    patches.validate_public_patch("+TOKEN = os.environ['LMNR_PROJECT_API_KEY']")


def test_evidence_review_is_independent_and_can_reject_import_failure(
    tmp_path, monkeypatch
):
    args = arguments(tmp_path)

    def model(args, model, policy, context, name, schema):
        assert "missing imports/files/dependencies" in policy
        assert context["baseline"][0]["excerpt"] == "ModuleNotFoundError"
        assert schema["properties"]["public_safe"] == {"type": "boolean"}
        return {
            "supported": False,
            "public_safe": True,
            "reason": "No production behavior executed",
        }, {}

    monkeypatch.setattr(patches, "call_model", model)
    review = patches.review_checks(
        args, {"patch": PATCH}, [], [], [{"excerpt": "ModuleNotFoundError"}], [], {}
    )
    assert not review["supported"]


@pytest.mark.parametrize(
    "supported,public_safe", [(False, True), (True, False), (True, True)]
)
def test_publication_requires_review_and_preserves_historical_comparison(
    source, tmp_path, monkeypatch, supported, public_safe
):
    checkout, path, config = source
    config["comparative_validation"] = {"command": ["unused"]}
    path.write_text(json.dumps(config))
    command(
        ["git", "remote", "add", "origin", "https://github.com/Example/automation.git"],
        checkout,
    )
    args = arguments(tmp_path / "repair", source_config=path, publish_draft=True)
    args.output.mkdir()
    report = fix_report(PATCH)
    report["findings"][0].update(trace_urls=[], case_id="c1")
    report["proposals"][0]["checks"] = config["checks"]
    report["proposals"][0]["candidate_checks"] = [
        {
            "name": "Complete candidate safety suite",
            "argv": [
                sys.executable,
                "-c",
                "import runpy; assert runpy.run_path('value.py')['VALUE'] == 4; print('safety passed')",
            ],
        }
    ]
    original = patches.command
    calls = []

    def transport(argv, cwd=None, **kwargs):
        if argv[:2] == ["gh", "api"]:
            return config["revision"]
        if argv[:3] == ["gh", "pr", "list"]:
            return "[]"
        if argv[:2] == ["git", "ls-remote"]:
            return ""
        if argv[:2] == ["git", "push"]:
            calls.append("push")
            return ""
        if argv[:3] == ["gh", "pr", "create"]:
            calls.append("create")
            assert "[Mechanism tested]" in argv[argv.index("--title") + 1]
            assert "automation-improvement" in argv
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            assert (
                "Historical input comparison" in body
                and "Historical inconclusive" in body
            )
            assert "Complete candidate safety suite | 0" in body
            return "https://github.com/Example/automation/pull/2"
        return original(argv, cwd, **kwargs)

    def historical(*args, **kwargs):
        calls.append("historical")
        return {"status": "inconclusive", "markdown": "Historical inconclusive"}

    monkeypatch.setattr(patches, "command", transport)

    def review(*args):
        assert len(args[-1]) == 1
        assert args[-1][0]["exit_code"] == 0
        assert "safety passed" in args[-1][0]["excerpt"]
        return {
            "supported": supported,
            "public_safe": public_safe,
            "reason": "Independent review",
        }

    monkeypatch.setattr(patches, "review_checks", review)
    monkeypatch.setattr(patches, "before_publication", historical)
    result = patches.propose_fix(args, report, {"source": load_source(path)})
    if supported and public_safe:
        assert calls == ["historical", "push", "create"]
        assert result["status"] == "evidence_supported_draft"
    else:
        assert calls == [] and result["status"] == "rejected"
        assert (args.output / "paired-review-failure.json").exists()


def test_failed_candidate_safety_check_blocks_review(source, tmp_path, monkeypatch):
    _, path, config = source
    args = arguments(tmp_path / "repair", source_config=path)
    args.output.mkdir()
    report = fix_report(PATCH)
    report["proposals"][0]["checks"] = config["checks"]
    report["proposals"][0]["candidate_checks"] = [
        {
            "name": "safety",
            "argv": [sys.executable, "-c", "raise RuntimeError('unsafe')"],
        }
    ]
    monkeypatch.setattr(patches, "review_checks", lambda *a: pytest.fail("not safe"))
    result = patches.propose_fix(args, report, {"source": load_source(path)})
    assert result["status"] == "rejected"
    assert result["candidate_only"][0]["exit_code"] == 1
    assert [row["exit_code"] for row in result["candidate"]] == [0, 0]
    assert (args.output / "candidate-only-0-check.json").exists()


def test_reviewer_receives_executed_candidate_safety_output(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    candidate_only = [
        {"name": "whole worker", "exit_code": 0, "excerpt": "48 tests OK"}
    ]

    def model(args, model, policy, context, name, schema):
        assert context["candidate_only"] == candidate_only
        assert "do not establish a baseline defect" in policy
        return {
            "supported": True,
            "public_safe": True,
            "reason": "Executed safety checks",
        }, {}

    monkeypatch.setattr(patches, "call_model", model)
    assert patches.review_checks(
        args, {"patch": PATCH}, [], [], [], [], {}, candidate_only
    )["supported"]


def test_failed_native_attempt_consumes_shared_budget(source, tmp_path, monkeypatch):
    _, path, _ = source

    def execute(worktree):
        (worktree / "value.py").write_text("VALUE = 4\n")
        raise RuntimeError("native model failed")

    captured = install_sdk(monkeypatch, execute)
    args = arguments(tmp_path / "repair", source_config=path)
    args.output.mkdir()
    args.budget = RunBudget({"max_inference_cost_usd": 8, "max_run_seconds": 100})
    with pytest.raises(RuntimeError, match="native model failed"):
        coding_agent.coordinate(args, {}, {"source": load_source(path)})
    assert args.budget.cost == 0.12 and captured["closed"]
    assert not (args.output / "coding-worktree").exists()
    measurement = json.loads(
        (args.output / "coding-agent/measurement.json").read_text()
    )
    assert measurement["sdk_estimated_cost_usd"] == 0.12
    worktree = json.loads((args.output / "coding-agent/worktree.json").read_text())
    assert "+VALUE = 4" in worktree["patch"]


def test_issue_reads_do_not_require_laminar_credentials(tmp_path, monkeypatch, capsys):
    import topic_issue
    from test_topic_issue import GitHub, snapshot

    monkeypatch.delenv("LMNR_PROJECT_API_KEY", raising=False)
    monkeypatch.delenv("LMNR_BASE_URL", raising=False)
    monkeypatch.setattr(topic_issue, "command", GitHub())
    value = snapshot(tmp_path)
    issue = topic_issue.TopicIssue.ensure(value, tmp_path / "issue")
    value["repair_issue"] = issue.refresh()
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"snapshot": value, "inventory": {}}))
    coding_agent.evidence(context, "read_issue", {"section": "Goal", "offset": 0})
    result = json.loads(capsys.readouterr().out)
    assert result["content"].startswith("## Goal")


def test_brief_removes_duplicate_traces_without_losing_originals(
    source, tmp_path, monkeypatch
):
    _, path, _ = source
    output = tmp_path / "repair"
    output.mkdir()
    original = {"source": load_source(path), "samples": [{"raw": "trace-" * 10000}]}
    captured = install_sdk(
        monkeypatch,
        lambda wt: (output / "coding-agent/result.json").write_text(
            '{"proposals":[],"rejections":[]}'
        ),
    )
    args = arguments(output, source_config=path)
    coding_agent.coordinate(
        args,
        {"source": original["source"], "cases": [{"original_task": "task"}]},
        original,
    )
    brief_path = output / "coding-agent/context.json"
    brief = json.loads(brief_path.read_text())
    assert brief_path.stat().st_size < 4000
    assert brief["context"]["cases"] == [{"original_task": "task"}]
    assert brief["snapshot"]["source"]["files"] == ["value.py"]
    assert json.loads(Path(brief["snapshot_path"]).read_text()) == original
    assert json.loads(Path(brief["inventory_path"]).read_text()) == {}
    assert "targeted JSON extraction" in captured["prompt"]


def test_independent_review_receives_operational_constraints(tmp_path, monkeypatch):
    runtime = {"automation": {"timeout": 1200, "trigger": {"type": "event"}}}
    captured = []

    def review(args, model, prompt, context, *rest):
        captured.append((prompt, context))
        return {
            "supported": False,
            "public_safe": True,
            "reason": "Missing timeout margin",
        }, {}

    monkeypatch.setattr(patches, "call_model", review)
    result = patches.review_checks(
        arguments(tmp_path, coordinator="sol"),
        {"patch": ""},
        [],
        [],
        [],
        [],
        {"source": {"runtime": runtime}},
    )
    assert not result["supported"]
    assert captured[0][1]["runtime"] == runtime
    assert "dispatch/concurrency" in captured[0][0]


@pytest.mark.parametrize("kind", ["openhands", "acp", "missing"])
def test_saved_profile_owns_model_tools_and_secrets(
    source, tmp_path, monkeypatch, kind
):
    import sources

    _, path, _ = source
    output = tmp_path / "repair"
    output.mkdir()

    def execute(worktree):
        (output / "coding-agent/result.json").write_text(
            json.dumps({"proposals": [], "rejections": []})
        )

    captured = install_sdk(monkeypatch, execute)
    monkeypatch.setenv("AGENT_SERVER_URL", "http://127.0.0.1:18000")
    monkeypatch.setenv("SESSION_API_KEY", "private-session-key")
    profile = SimpleNamespace(
        name="chosen",
        id="profile-id",
        revision=3,
        agent_kind=kind,
        llm_profile_ref="cheap-model",
        public_skills=[],
    )
    custom_agent = SimpleNamespace(tools=[SimpleNamespace(name="custom_tool")])

    class ProfileLLM:
        @classmethod
        def model_validate(cls, value):
            captured["profile_llm"] = value
            instance = cls()
            instance.model = value["model"]
            return instance

        def model_copy(self, update):
            captured["instrumentation"] = update
            return self

    def resolve(value, *, llm_store, mcp_config, available_skills, public_skill_names):
        assert value is profile
        assert mcp_config == {"example": {"url": "https://mcp.test"}}
        assert available_skills == ["user-skill"] and public_skill_names == set()
        return SimpleNamespace(
            llm=llm_store.load("cheap-model"), create_agent=lambda: custom_agent
        )

    class Workspace:
        def __init__(self, *, host, api_key, working_dir):
            assert working_dir == str(output / "coding-agent")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_secrets(self, *, agent_profile_id):
            assert agent_profile_id == "profile-id"
            return {"EXTRA_TOKEN": "profile-secret-value"}

    for name, fields in {
        "openhands.sdk.profiles": {
            "validate_agent_profile": lambda x: profile,
            "resolve_agent_profile": resolve,
        },
        "openhands.sdk.skills": {
            "load_available_skills": lambda **kwargs: {"user": "user-skill"}
        },
        "openhands.sdk.workspace": {"RemoteWorkspace": Workspace},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(fields)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys.modules["openhands.sdk"], "LLM", ProfileLLM)
    monkeypatch.setattr(
        sys.modules["openhands.tools"],
        "register_default_tools",
        lambda: None,
        raising=False,
    )

    def request(host, endpoint, *, expose_secrets=False):
        if endpoint == "/api/agent-profiles/chosen":
            if kind == "missing":
                raise FileNotFoundError("Unknown profile")
            return json.dumps({"profile": {}})
        assert expose_secrets
        if endpoint == "/api/profiles/cheap-model":
            return json.dumps(
                {
                    "config": {
                        "model": "alternate-model",
                        "api_key": "profile-model-key",
                        "reasoning_effort": "low",
                    }
                }
            )
        assert endpoint == "/api/settings"
        return json.dumps(
            {"agent_settings": {"mcp_config": {"example": {"url": "https://mcp.test"}}}}
        )

    monkeypatch.setattr(sources, "native_request", request)
    args = arguments(
        output,
        source_config=path,
        coordinator="openai/gpt-6.1-sol",
        repair_agent_profile="chosen",
    )
    if kind != "openhands":
        with pytest.raises((ValueError, FileNotFoundError)):
            coding_agent.coordinate(args, {}, {"source": load_source(path)})
        assert "instance" not in captured
        return
    _, measurement, _ = coding_agent.coordinate(args, {}, {"source": load_source(path)})
    assert captured["conversation"]["agent"] is custom_agent
    assert captured["conversation"]["secrets"] == {
        "EXTRA_TOKEN": "profile-secret-value"
    }
    assert measurement["model"] == "alternate-model" and measurement["tools"] == [
        "custom_tool"
    ]
    assert measurement["profile"]["revision"] == 3
    assert captured["profile_llm"]["reasoning_effort"] == "low"
    assert "using alternate-model" in captured["prompt"]
    for file in (output / "coding-agent").glob("*.json"):
        assert "profile-model-key" not in file.read_text()
        assert "profile-secret-value" not in file.read_text()
