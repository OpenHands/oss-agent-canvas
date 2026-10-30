import ast
import atexit
import importlib.util
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

# SDK imports can initialize telemetry; tool setup can discover user plugins.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
for key in tuple(os.environ):
    if key.startswith(("LMNR_", "OTEL_", "OPENHANDS_OBSERVABILITY_")):
        os.environ.pop(key)
TEST_HOME = tempfile.TemporaryDirectory()
atexit.register(TEST_HOME.cleanup)
os.environ["HOME"] = TEST_HOME.name

import httpx

from openhands.sdk import Agent, Conversation, LLM, LocalWorkspace, RemoteWorkspace, Tool
from openhands.sdk.event import Event, ObservationEvent, UserRejectObservation
from openhands.sdk.hooks import HookConfig, HookEvent, HookExecutor, HookManager
from openhands.sdk.llm import MessageToolCall
from openhands.tools.terminal import TerminalAction, TerminalTool

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/weekly-automation-health-review/scripts"
SYNTHETIC_SECRET = "synthetic-process-credential-for-regression"


def configured_hook():
    # Read the production wiring without running the write-capable entrypoint.
    tree = ast.parse((SCRIPTS / "main.py").read_text())
    config = next(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "conversation_kwargs"
                for t in node.targets)
    )
    values = {key.value: value for key, value in zip(config.keys, config.values)}
    if "hook_config" not in values:
        return HookConfig()
    expression = values["hook_config"]
    if not (isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Name)
            and expression.func.id == "get_process_audit_hook"):
        raise AssertionError("Unexpected process audit hook wiring")
    return load_audit().get_process_audit_hook()


def load_audit():
    spec = importlib.util.spec_from_file_location(
        "process_audit", SCRIPTS / "process_audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolReplay(unittest.TestCase):
    def execute(self, command, config=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            with sqlite3.connect(work / "health.db") as db:
                db.execute("CREATE TABLE automation_runs (status TEXT)")
                db.execute("INSERT INTO automation_runs VALUES ('COMPLETED')")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            # Captured external process output is replaced with synthetic data.
            ps = bin_dir / "ps"
            ps.write_text(
                "#!/bin/sh\nprintf '%s\\n' "
                + shlex.quote(
                    "23 worker openvscode-server --connection-token "
                    + SYNTHETIC_SECRET
                ) + "\n"
            )
            ps.chmod(0o700)
            (bin_dir / "head").symlink_to("/usr/bin/head")
            agent = Agent(
                llm=LLM(model="openai/gpt-4o-mini", api_key="unused-offline-key",
                        log_completions=False),
                tools=[Tool(name=TerminalTool.name, params={
                    "terminal_type": "subprocess", "shell_path": "/bin/bash",
                    "env": {"HOME": str(root), "PATH": str(bin_dir)},
                })],
                include_default_tools=[], mcp_config={}, critic=None,
                system_prompt="Offline process audit test.",
            )
            with closing(Conversation(
                agent=agent, workspace=LocalWorkspace(working_dir=work),
                persistence_dir=root / "conversations", visualizer=None,
                hook_config=configured_hook() if config is None else config,
                delete_on_close=False,
            )) as conversation:
                conversation._ensure_agent_ready()
                agent = conversation.agent
                call = MessageToolCall(
                    id=f"call-{uuid.uuid4()}", name="terminal",
                    arguments=TerminalAction(command=command).model_dump_json(),
                    origin="completion",
                )
                with conversation.state:
                    action = agent._get_action_event(
                        tool_call=call, conversation=conversation,
                        llm_response_id="offline-test",
                        on_event=conversation._on_event,
                    )
                    agent._execute_actions(
                        conversation=conversation, action_events=[action],
                        on_event=conversation._on_event,
                    )
                events_dir = Path(conversation.state.persistence_dir) / "events"
                serialized = "\n".join(
                    path.read_text() for path in events_dir.glob("event-*.json")
                )
                events = [Event.model_validate_json(line)
                          for line in serialized.splitlines()]
                result = next(
                    event for event in events
                    if isinstance(event, (ObservationEvent, UserRejectObservation))
                    and event.action_id == action.id
                )
                return result, serialized, (work / "witness").exists()

    def test_process_listing_cannot_print_credentials(self):
        result, persisted, executed = self.execute(
            "ps -eo pid,user,cmd 2>/dev/null | head -40; printf done > witness"
        )
        self.assertFalse(SYNTHETIC_SECRET in persisted,
                         "Raw process credential reached persisted tool output")
        self.assertIsInstance(result, UserRejectObservation)
        self.assertEqual(result.rejection_source, "hook")
        self.assertFalse(executed, "The blocked compound command was executed")
        self.assertIn("credential-option audit", result.rejection_reason)

    def test_safe_diagnostic_still_executes(self):
        result, _, executed = self.execute(
            "printf 'healthy\\n'; printf done > witness"
        )
        self.assertIsInstance(result, ObservationEvent)
        self.assertEqual(result.observation.exit_code, 0)
        self.assertIn("healthy", result.observation.text)
        self.assertTrue(executed)

    def test_read_only_database_control(self):
        script = (
            "import json, sqlite3; "
            "db = sqlite3.connect('file:health.db?mode=ro', uri=True); "
            "print(json.dumps(db.execute("
            "'SELECT status, count(*) FROM automation_runs GROUP BY status'"
            ").fetchall()))"
        )
        result, persisted, executed = self.execute(
            shlex.quote(sys.executable) + " -c " + shlex.quote(script)
        )
        self.assertIsInstance(result, ObservationEvent)
        self.assertEqual(result.observation.exit_code, 0)
        self.assertIn('[["COMPLETED", 1]]', result.observation.text)
        self.assertNotIn(SYNTHETIC_SECRET, persisted)
        self.assertFalse(executed)

    def test_remote_create_transmits_hook(self):
        config = configured_hook()
        captured = []

        def capture(request):
            self.assertEqual((request.method, request.url.path),
                             ("POST", "/api/conversations"))
            captured.append(json.loads(request.content))
            # Stop before a remote conversation or websocket can be created.
            return httpx.Response(422, json={"detail": "capture-only boundary"})

        workspace = RemoteWorkspace(
            host="http://capture.invalid", working_dir="/workspace"
        )
        agent = Agent(
            llm=LLM(model="test/not-called", api_key="unused", log_completions=False),
            tools=[], include_default_tools=[],
        )
        with (
            closing(agent),
            httpx.Client(base_url=workspace.host, trust_env=False,
                         transport=httpx.MockTransport(capture)) as client,
        ):
            workspace._client = client
            with self.assertRaises(httpx.HTTPStatusError) as error:
                Conversation(agent=agent, workspace=workspace, hook_config=config,
                             visualizer=None, delete_on_close=False)
            self.assertEqual(error.exception.response.status_code, 422)
        self.assertEqual(len(captured), 1)
        self.assertEqual(HookConfig.model_validate(captured[0]["hook_config"]), config)
        self.assertTrue(config.pre_tool_use)

    def test_stalled_audit_denies_before_outer_hook_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / "23").mkdir()
            os.mkfifo(proc / "23/cmdline")
            config = configured_hook()
            script = (
                "import runpy; from pathlib import Path; "
                f"m = runpy.run_path({str(SCRIPTS / 'process_audit.py')!r}); "
                f"m['run_hook'](Path({str(proc)!r}))"
            )
            config.pre_tool_use[0].hooks[0].command = (
                shlex.quote(sys.executable) + " -c " + shlex.quote(script)
            )
            result, persisted, executed = self.execute(
                "ps -eo cmd; printf done > witness", config=config
            )
            self.assertIsInstance(result, UserRejectObservation)
            self.assertIn("audit unavailable", result.rejection_reason)
            self.assertNotIn(SYNTHETIC_SECRET, persisted)
            self.assertFalse(executed)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.audit = load_audit()
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.proc = Path(self.directory)

    def process(self, pid, *argv):
        path = self.proc / str(pid)
        path.mkdir()
        (path / "cmdline").write_bytes(b"\0".join(argv) + b"\0")

    def test_only_fixed_labels_and_option_names_leave_collector(self):
        secret = SYNTHETIC_SECRET.encode()
        self.process(23, b"/usr/bin/sh", b"/private/openvscode-server",
                     b"--connection-token", secret)
        self.process(27, b"/private/" + secret, b"--api-key=" + secret,
                     b"--password", secret, b"--unknown-" + secret)
        self.process(28, b"/usr/bin/node", b"/private/" + secret,
                     b"--client-secret=" + secret, b"\xff" + secret)
        result = self.audit.audit_processes(self.proc)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["scanned"], 3)
        self.assertEqual(result["findings"], [
            {"pid": 23, "program": "openvscode-server",
             "options": ["--connection-token"]},
            {"pid": 27, "program": "other",
             "options": ["--api-key", "--password"]},
            {"pid": 28, "program": "node", "options": ["--client-secret"]},
        ])
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(result))

    def test_no_value_no_finding(self):
        self.process(1, b"worker", b"--connection-token-file", b"private/path",
                     b"--token=", b"--api-key", b"", b"--password")
        self.process(2, b"worker", b"--connection-token", b"--help")
        result = self.audit.audit_processes(self.proc)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["findings"], [])

    def test_unavailable_namespace_has_no_raw_fallback(self):
        result = self.audit.audit_processes(self.proc / SYNTHETIC_SECRET)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["findings"], [])
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(result))

    def test_partial_coverage_and_bounded_output(self):
        (self.proc / "1").mkdir()
        self.process(2, b"worker", b"--token=" + b"x" * 65536)
        for pid in range(100, 155):
            self.process(pid, b"worker", b"--token=" + SYNTHETIC_SECRET.encode())
        result = self.audit.audit_processes(self.proc)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unreadable"], 1)
        self.assertEqual(result["truncated"], 1)
        self.assertEqual(result["omitted"], 5)
        self.assertEqual(len(result["findings"]), 50)
        self.assertLess(len(json.dumps(result)), 8192)
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(result))

    def test_serialized_hook_runs_without_worker_module(self):
        config = HookConfig.model_validate_json(configured_hook().model_dump_json())
        hook = config.pre_tool_use[0].hooks[0]
        self.assertFalse(hook.async_)
        self.assertEqual(hook.timeout, 5)
        self.assertNotIn(str(SCRIPTS), hook.command)
        manager = HookManager(config=config, working_dir=self.directory)
        for command in ("ps -eo cmd", "/bin/ps aux", "sh -c 'ps -ef'"):
            with self.subTest(command=command):
                allowed, results = manager.run_pre_tool_use(
                    "terminal", {"command": command}
                )
                self.assertFalse(allowed)
                self.assertTrue(results[0].success)
                self.assertIn("Value-free", results[0].reason)
        for command in ("df -h", "du -sh workspaces", "printf healthy"):
            with self.subTest(command=command):
                allowed, _ = manager.run_pre_tool_use("terminal", {"command": command})
                self.assertTrue(allowed)

    def test_missing_hook_interpreter_blocks_instead_of_failing_open(self):
        hook = configured_hook().pre_tool_use[0].hooks[0]
        result = HookExecutor(working_dir=self.directory).execute(
            hook, HookEvent(event_type="PreToolUse", tool_name="terminal",
                            tool_input={"command": "ps -eo cmd"}),
            env={"PATH": self.directory},
        )
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.should_continue)

    def test_malformed_event_is_denied_without_echo(self):
        hook = configured_hook().pre_tool_use[0].hooks[0]
        completed = subprocess.run(
            hook.command, shell=True, cwd=self.directory,
            input=SYNTHETIC_SECRET, text=True, capture_output=True, timeout=6,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["decision"], "deny")
        self.assertNotIn(SYNTHETIC_SECRET, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
