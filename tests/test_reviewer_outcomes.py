import contextlib
import importlib.util
import io
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from openhands.sdk import LLM, Agent

ROOT = Path(os.environ.get("REVIEWER_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
SCRIPTS = ROOT / "plugins/github-pr-reviewer-events/scripts"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for dependency in ("github_client", "main", "maintainer_handoff", "agent_conversation"):
    load_module(dependency)
worker = load_module("worker")
conversation_module = sys.modules[worker.AgentConversationDispatcher.__module__]


class AgentServerFixture:
    """Local HTTP boundary; no model execution or GitHub writes."""

    def __init__(self, outcomes, delays=None):
        self.outcomes = list(outcomes)
        self.delays = delays or [0] * len(self.outcomes)
        self.records = {}
        self.kv = {}
        self.requests = []
        self.callbacks = []
        self.creations = []
        self.observations = []
        self.observing = False
        self.agent = Agent(llm=LLM(model="openai/test", api_key="fixture"), tools=[])
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlsplit(self.path).path
                fixture.requests.append(("GET", path))
                if path == "/api/settings/secrets":
                    return self.respond(200, {"secrets": []})
                if path.startswith("/v1/kv/"):
                    key = path.rsplit("/", 1)[1]
                    if key not in fixture.kv:
                        return self.respond(404, {})
                    return self.respond(200, {"value": fixture.kv[key]})
                if path.endswith("/events/search"):
                    return self.respond(200, {"items": [], "next_page_id": None})
                cid = path.rsplit("/", 1)[1]
                if cid not in fixture.records:
                    return self.respond(404, {})
                record = fixture.records[cid]
                if not fixture.observing:
                    return self.respond(200, record["info"])
                fixture.observations.append((cid, len(fixture.requests) - 1))
                if time.monotonic() < record["complete_at"]:
                    return self.respond(200, record["info"])
                states = record["states"]
                status = states.pop(0) if len(states) > 1 else states[0]
                if status == "http-error":
                    return self.respond(503, {"detail": "private provider detail"})
                if status == "malformed":
                    return self.respond(200, [])
                return self.respond(200, record["info"] | {"execution_status": status})

            def do_POST(self):
                path = urlsplit(self.path).path
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(("POST", path))
                if path == "/callback":
                    fixture.callbacks.append(body)
                    return self.respond(200, {})
                if path == "/api/conversations":
                    fixture.creations.append(body)
                    cid = body["conversation_id"]
                    info = {
                        "id": cid,
                        "agent": fixture.agent.model_dump(mode="json"),
                        "max_iterations": 10,
                        "execution_status": "running",
                    }
                    fixture.records[cid] = {
                        "info": info,
                        "states": list(fixture.outcomes[len(fixture.creations) - 1]),
                        "complete_at": time.monotonic()
                        + fixture.delays[len(fixture.creations) - 1],
                    }
                    return self.respond(200, info)
                return self.respond(400, {})

            def do_PUT(self):
                path = urlsplit(self.path).path
                fixture.requests.append(("PUT", path))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.kv[path.rsplit("/", 1)[1]] = body
                return self.respond(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def environment(self):
        return {
            "AGENT_SERVER_URL": self.url,
            "SESSION_API_KEY": "fixture",
            "AUTOMATION_AGENT_PROFILE_ID": "00000000-0000-0000-0000-000000000001",
            "AUTOMATION_EVENT_PAYLOAD": json.dumps({"automation_id": "fixture"}),
            "AUTOMATION_API_URL": self.url,
            "AUTOMATION_KV_TOKEN": "fixture",
            "AUTOMATION_CALLBACK_URL": self.url + "/callback",
            "AUTOMATION_CALLBACK_API_KEY": "fixture",
            "AUTOMATION_RUN_ID": "fixture",
            "OPENHANDS_REMOTE_WS_READY_TIMEOUT": "0",
            "OPENHANDS_REMOTE_WS_READY_REQUIRED": "false",
        }


class ReviewerOutcomeTests(unittest.TestCase):
    def run_scan(self, fixture, discovery_error=False, prompts=None):
        prompts = prompts or ["Review fixture PR"] * len(fixture.outcomes)

        def discover(automation_type, dispatcher):
            for index, prompt in enumerate(prompts):
                dispatcher.deliver(f"fixture:{index}", "delivery", prompt, head="head")
            fixture.observing = True
            if discovery_error:
                raise RuntimeError("discovery failed")

        # Replace external discovery, not dispatch, SDK transport, or observation.
        with (
            patch.dict(os.environ, fixture.environment()),
            patch.object(worker, "run_repositories", discover),
            patch.object(conversation_module, "OBSERVATION_SECONDS", 3, create=True),
            patch.object(conversation_module, "POLL_SECONDS", 0.005, create=True),
            patch.object(
                conversation_module, "FINISHED_GRACE_SECONDS", 0.02, create=True
            ),
            worker.AgentConversationDispatcher() as dispatcher,
        ):
            worker.run_scan(dispatcher)

    def test_provider_error_fails_worker_callback(self):
        with AgentServerFixture([["running", "error"]]) as fixture:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(RuntimeError):
                self.run_scan(fixture)
            self.assertEqual(fixture.callbacks[-1]["status"], "FAILED")
            self.assertIn("conversation-error", output.getvalue())
            self.assertNotIn("private provider detail", output.getvalue())
            self.assertEqual(len(fixture.creations), 1)
            self.assert_no_execution_writes(fixture)

    def assert_no_execution_writes(self, fixture):
        writes = [
            (method, path) for method, path in fixture.requests if method != "GET"
        ]
        self.assertTrue(
            all(
                path in ("/api/conversations", "/callback")
                or path.startswith("/v1/kv/")
                for _, path in writes
            ),
            writes,
        )

    def test_all_submissions_precede_observation_and_failures_aggregate(self):
        with AgentServerFixture([["error"], ["stuck"]]) as fixture:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(RuntimeError):
                self.run_scan(fixture)
            self.assertEqual(len(fixture.creations), 2)
            self.assertIn("conversation-error", output.getvalue())
            self.assertIn("conversation-stuck", output.getvalue())
            last_creation = max(
                i
                for i, request in enumerate(fixture.requests)
                if request == ("POST", "/api/conversations")
            )
            self.assertEqual(
                {cid for cid, _ in fixture.observations}, set(fixture.records)
            )
            self.assertTrue(
                all(index > last_creation for _, index in fixture.observations)
            )
            self.assert_no_execution_writes(fixture)

    def test_stable_finished_conversations_complete(self):
        with AgentServerFixture([["running", "finished"], ["finished"]]) as fixture:
            self.run_scan(fixture)
            self.assertEqual(fixture.callbacks[-1]["status"], "COMPLETED")
            self.assert_no_execution_writes(fixture)

    def test_deduplicated_delivery_is_still_observed(self):
        with AgentServerFixture([["finished"]]) as fixture:
            self.run_scan(fixture)
            self.run_scan(fixture)
            self.assertEqual(len(fixture.creations), 1)
            self.assertEqual(len(fixture.callbacks), 2)
            self.assertTrue(
                all(item["status"] == "COMPLETED" for item in fixture.callbacks)
            )
            self.assert_no_execution_writes(fixture)

    def test_stop_hook_can_reopen_finished(self):
        with AgentServerFixture([["finished", "running", "error"]]) as fixture:
            with self.assertRaises(RuntimeError):
                self.run_scan(fixture)
            self.assertEqual(fixture.callbacks[-1]["status"], "FAILED")

    def test_shared_deadline_does_not_cancel_running_conversations(self):
        with AgentServerFixture([["running"], ["running"]]) as fixture:
            started = time.monotonic()
            with self.assertRaises(RuntimeError):
                self.run_scan(fixture)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(fixture.callbacks[-1]["status"], "FAILED")
            self.assert_no_execution_writes(fixture)

    def test_unreadable_and_nonfinished_states_fail_closed(self):
        for state in (
            "http-error",
            "malformed",
            "unknown",
            "paused",
            "waiting_for_confirmation",
        ):
            with self.subTest(state=state), AgentServerFixture([[state]]) as fixture:
                with self.assertRaises(RuntimeError):
                    self.run_scan(fixture)
                self.assertEqual(fixture.callbacks[-1]["status"], "FAILED")

    def test_discovery_failure_still_observes_submitted_conversations(self):
        with AgentServerFixture([["error"]]) as fixture:
            output = io.StringIO()
            with (
                contextlib.redirect_stdout(output),
                self.assertRaisesRegex(RuntimeError, "discovery failed"),
            ):
                self.run_scan(fixture, discovery_error=True)
            self.assertIn("conversation-error", output.getvalue())
            self.assertEqual(fixture.callbacks[-1]["status"], "FAILED")

    def test_empty_scan_completes_without_polling(self):
        with AgentServerFixture([]) as fixture:
            self.run_scan(fixture)
            self.assertEqual(fixture.callbacks[-1]["status"], "COMPLETED")
            self.assertFalse(fixture.records)


if __name__ == "__main__":
    unittest.main()
