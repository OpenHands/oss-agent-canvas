import io
import json
import os
import runpy
import threading
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

# LiteLLM otherwise fetches mutable metadata when the SDK is imported.
with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
    from openhands.sdk import LLM
    from openhands.sdk.workspace.remote.base import RemoteWorkspace
    from openhands.sdk.workspace.repo import CloneResult, RepoMapping
    from openhands.workspace import OpenHandsCloudWorkspace
    from pydantic import SecretStr


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/docs-release-to-pr/scripts/main.py"
PROMPT = SCRIPT.with_name("prompt.txt").read_text()
REPO_URL = "https://github.com/OpenHands/docs"


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.callbacks = []
        callbacks = self.callbacks

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                callbacks.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def run_runner(self, raw_payload, *, cloud=False, run_error=None, repo_dir=None):
        self.callbacks.clear()
        calls = []
        conversations = []

        # Isolate remote/model writes; run the real entrypoint and SDK exit callback.
        class Conversation:
            def __init__(self, **kwargs):
                calls.append("conversation")
                conversations.append(self)
                self.id = "fixture-conversation"
                self.messages = []
                self.secrets = {}
                self.closed = False
                self.conversation_stats = SimpleNamespace(
                    get_combined_metrics=lambda: SimpleNamespace(accumulated_cost=0)
                )

            def update_secrets(self, secrets):
                self.secrets.update(secrets)

            def send_message(self, text):
                calls.append("send_message")
                self.messages.append(text)

            def run(self):
                calls.append("run")
                if run_error:
                    raise run_error

            def close(self):
                calls.append("close")
                self.closed = True

        def external_result(name, value):
            def invoke(*args, **kwargs):
                calls.append(name)
                return value
            return invoke

        stdout, stderr = io.StringIO(), io.StringIO()
        error = None
        with TemporaryDirectory() as directory, ExitStack() as stack:
            env = {
                "WORKSPACE_BASE": directory,
                "AUTOMATION_RUN_ID": "fixture-run",
                "AUTOMATION_CALLBACK_URL": (
                    f"http://127.0.0.1:{self.server.server_port}/complete"
                ),
                "OPENHANDS_SUPPRESS_BANNER": "1",
            }
            workspace_type = OpenHandsCloudWorkspace if cloud else RemoteWorkspace
            if cloud:
                env.update({
                    "OPENHANDS_API_KEY": "fixture-key",
                    "OPENHANDS_CLOUD_API_URL": "http://127.0.0.1:1",
                    "SANDBOX_ID": "fixture-sandbox",
                    "OH_SESSION_API_KEYS_0": "fixture-session-key",
                })
            else:
                env["AGENT_SERVER_URL"] = "http://127.0.0.1:1"
            if raw_payload is not None:
                env["AUTOMATION_EVENT_PAYLOAD"] = raw_payload
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch("openhands.sdk.Conversation", Conversation))
            stack.enter_context(patch("openhands.sdk.RemoteConversation", Conversation))
            mapping = RepoMapping(REPO_URL, "docs", repo_dir or f"{directory}/docs")
            results = {
                "clone_repos": CloneResult(1, [], {REPO_URL: mapping}),
                "load_skills_from_agent_server": ([], None),
                "get_llm": LLM(model="openai/gpt-4o-mini", api_key=SecretStr("fixture")),
                "get_secrets": {"GITHUB_TOKEN": "fixture-token"},
                "get_mcp_config": {},
            }
            for name, value in results.items():
                stack.enter_context(patch.object(
                    workspace_type, name, external_result(name, value)
                ))
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    runpy.run_path(str(SCRIPT), run_name="__main__")
                except Exception as exc:
                    error = exc
        return SimpleNamespace(
            error=error, calls=calls, conversations=conversations,
            stdout=stdout.getvalue(), stderr=stderr.getvalue(),
            callbacks=self.callbacks[:],
        )

    def assert_rejected(self, result):
        self.assertIsInstance(result.error, ValueError, result.stdout + result.stderr)
        self.assertIn("AUTOMATION_EVENT_PAYLOAD", str(result.error))
        self.assertIn("event.payload", str(result.error))
        self.assertEqual(result.calls, [], "invalid input must not start repository or agent work")
        self.assertEqual(result.conversations, [])
        self.assertNotIn("ALL_OK", result.stdout)
        self.assertEqual(len(result.callbacks), 1)
        self.assertEqual(result.callbacks[0]["status"], "FAILED")
        self.assertEqual(result.callbacks[0]["run_id"], "fixture-run")
        self.assertIn("AUTOMATION_EVENT_PAYLOAD", str(result.callbacks[0]["error"]))
        self.assertNotIn("conversation_id", result.callbacks[0])

    def assert_delivered(self, event, *, cloud=False):
        result = self.run_runner(json.dumps({"event": event}), cloud=cloud)
        self.assertIsNone(result.error, str(result.error))
        self.assertEqual(result.calls.count("clone_repos"), 1)
        self.assertEqual(result.calls.count("run"), 1)
        self.assertEqual(result.calls.count("close"), 1)
        self.assertEqual(len(result.conversations), 1)
        conversation = result.conversations[0]
        self.assertTrue(conversation.closed)
        self.assertEqual(conversation.secrets["GITHUB_TOKEN"], "fixture-token")
        self.assertEqual(len(conversation.messages), 1)
        message = conversation.messages[0]
        self.assertTrue(message.startswith("## Cloned Repositories"))
        section = message.split("## Event Payload\n", 1)[1]
        delivered = json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(delivered, event)
        self.assertTrue(message.endswith("## Task\n\n" + PROMPT))
        self.assertEqual(len(result.callbacks), 1)
        self.assertEqual(result.callbacks[0]["status"], "COMPLETED")
        self.assertIn("ALL_OK", result.stdout)
        return result


class MissingPayloadTests(RunnerTestCase):
    def test_manual_dispatch_without_payload_stops_before_setup(self):
        self.assert_rejected(self.run_runner(None))

    def test_missing_nested_webhook_stops_before_setup(self):
        self.assert_rejected(self.run_runner(json.dumps({"event": {"event_key": "release"}})))


class ValidPayloadControls(RunnerTestCase):
    def test_published_release_is_delivered_unchanged(self):
        self.assert_delivered({"payload": {
            "action": "published", "repository": {"full_name": "OpenHands/OpenHands"},
            "release": {"tag_name": "v1.2.3", "body": "Release notes — keep links intact."},
        }, "event_key": "release", "source_override": "github-events"})

    def test_issue_opened_and_unlabeled_are_delivered_unchanged(self):
        for action in ("opened", "unlabeled"):
            with self.subTest(action=action):
                self.assert_delivered({"payload": {
                    "action": action, "repository": {"full_name": "OpenHands/docs"},
                    "issue": {"number": 123, "title": "automation: Docs update for OpenHands v1.2.3 release"},
                }})

    def test_skip_decisions_remain_with_existing_workflow(self):
        for payload in (
            {},
            {"action": "edited"},
            {"action": "published", "release": {"draft": True, "prerelease": True}},
            {"action": "published", "repository": {"full_name": "other/repo"}},
            {"action": "unlabeled", "issue": {"state": "closed", "labels": [{"name": "ready-for-human"}]}},
        ):
            with self.subTest(payload=payload):
                self.assert_delivered({"payload": payload})

    def test_cloud_delivery_preserves_session_secret(self):
        result = self.assert_delivered({"payload": {"action": "opened", "issue": {"number": 123}}}, cloud=True)
        self.assertIn("AUTOMATION_SESSION_URL", result.conversations[0].secrets)

    def test_execution_errors_still_close_and_fail_callback(self):
        result = self.run_runner('{"event":{"payload":{}}}', run_error=RuntimeError("fixture failure"))
        self.assertIsInstance(result.error, RuntimeError)
        self.assertTrue(result.conversations[0].closed)
        self.assertEqual(result.callbacks[0]["status"], "FAILED")
        self.assertNotIn("ALL_OK", result.stdout)


class InputSafetyTests(RunnerTestCase):
    def test_malformed_and_wrong_shape_inputs_fail_closed(self):
        for raw in (
            "", " ", "{", "null", "[]", "true", "1", '"text"', "{}",
            '{"event":null}', '{"event":[]}', '{"event":"payload"}',
            '{"event":{"payload":null}}', '{"event":{"payload":[]}}',
            '{"event":{"payload":"text"}}', '{"event":{"payload":false}}',
        ):
            with self.subTest(raw=raw):
                self.assert_rejected(self.run_runner(raw))

    def test_cloud_missing_payload_uses_failure_callback(self):
        self.assert_rejected(self.run_runner(None, cloud=True))

    def test_invalid_payload_content_is_not_in_error_or_callback(self):
        marker = "private-fixture-marker"
        result = self.run_runner(json.dumps({"event": {"payload": marker}}))
        self.assert_rejected(result)
        self.assertNotIn(marker, str(result.error) + json.dumps(result.callbacks))
        self.assertNotIn(marker, result.stdout + result.stderr)


class CapturedInputTestCase(RunnerTestCase):
    def captured_inputs(self, classification):
        path = os.environ.get("DOCS_RELEASE_CAPTURED_INPUTS")
        if not path:
            self.skipTest("private captured inputs not configured")
        cases = json.loads(Path(path).read_text())
        selected = [case for case in cases if case["classification"] == classification]
        self.assertTrue(selected)
        return selected


class CapturedMissingPayloadTests(CapturedInputTestCase):
    def test_missing_section_projection_is_rejected_before_setup(self):
        for case in self.captured_inputs("negative"):
            with self.subTest(case=case["case_id"]):
                self.assertIsNone(case["event"])
                self.assertNotIn("## Event Payload\n", case["message"])
                self.assertTrue(case["message"].endswith("## Task\n\n" + PROMPT))
                self.assert_rejected(self.run_runner(None, repo_dir=case["repo_dir"]))


class CapturedDeliveryControls(CapturedInputTestCase):
    def test_original_webhook_messages_are_preserved(self):
        for case in self.captured_inputs("non_negative"):
            with self.subTest(case=case["case_id"]):
                result = self.run_runner(
                    json.dumps({"event": case["event"]}), repo_dir=case["repo_dir"]
                )
                self.assertIsNone(result.error, str(result.error))
                self.assertEqual(result.calls.count("run"), 1)
                self.assertTrue(result.conversations[0].closed)
                self.assertEqual(len(result.conversations[0].messages), 1)
                self.assertEqual(result.conversations[0].messages[0], case["message"])
                self.assertEqual(len(result.callbacks), 1)
                self.assertEqual(result.callbacks[0]["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
