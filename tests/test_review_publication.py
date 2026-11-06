import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MethodType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    ROOT / "plugins/github-pr-reviewer-events/scripts/review_publication.py",
    ROOT / "automations/github-pr-reviewer-multi-repo-extensions-openhands-software-agent-sdk/tarball/review_publication.py",
]
HEAD = "a" * 40
DISCLOSURE = "_This review was posted by an AI agent (OpenHands)._"
BODY = DISCLOSURE + '\n\nNo material findings. Unicode: café; `code` and "quotes".\n\n✅ APPROVED\n'


def load(path):
    spec = importlib.util.spec_from_file_location("publication", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GitHubBoundary:
    # Only the external GitHub boundary is replaced; payload, journal and policy are real.
    def __init__(self):
        self.head = HEAD
        self.state = "open"
        self.login = "review-bot"
        self.author = "contributor"
        self.reviews = []
        self.comments = []
        self.posts = []
        self.failure = None
        self.response_body = None

    def pages(self, path):
        return self.reviews if path.endswith("/reviews") else self.comments

    def request(self, method, path, body=None):
        if method == "POST":
            self.posts.append(json.loads(json.dumps(body)))
            if self.failure:
                raise self.failure
            return {
                "id": 10,
                "body": self.response_body if self.response_body is not None else body["body"],
                "commit_id": HEAD,
                "state": "APPROVED" if body["event"] == "APPROVE" else "COMMENTED",
                "user": {"login": self.login},
            }
        if path == "/user":
            return {"login": self.login}
        return {"state": self.state, "head": {"sha": self.head}, "user": {"login": self.author}}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.receipt = Path(self.directory.name) / "receipt.json"
        self.module = load(MODULES[0])
        self.api = GitHubBoundary()

    def payload(self, **changes):
        return {"body": BODY, "commit_id": HEAD, "event": "APPROVE", **changes}

    def publish(self, payload=None):
        return self.module.publish(
            self.api, "owner/repo", 1, HEAD, "review-bot",
            self.payload() if payload is None else payload, self.receipt,
        )

    def test_invalid_body_never_posts(self):
        for body in [None, "", " \n", DISCLOSURE, DISCLOSURE + "\n✅ APPROVED", 1]:
            with self.subTest(body=body), self.assertRaises(self.module.PublicationError):
                self.publish(self.payload(body=body))
        self.assertEqual(self.api.posts, [])

    def test_misnamed_body_and_wrong_target_never_post(self):
        bad = self.payload()
        bad["body_file"] = "/missing"
        del bad["body"]
        for payload in [bad, self.payload(commit_id="b" * 40), self.payload(event="REQUEST_CHANGES")]:
            with self.assertRaises(self.module.PublicationError):
                self.publish(payload)
        self.assertEqual(self.api.posts, [])

    def test_exact_body_is_preserved(self):
        self.assertEqual(self.publish()["id"], 10)
        self.assertEqual(self.api.posts[0]["body"], BODY)
        self.assertEqual(json.loads(self.receipt.read_text())["state"], "accepted")

    def test_same_attempt_cannot_publish_twice(self):
        self.publish()
        with self.assertRaises(self.module.PublicationError):
            self.publish()
        self.assertEqual(len(self.api.posts), 1)

    def test_accepted_empty_response_is_terminal(self):
        self.api.response_body = ""
        for _ in range(2):
            with self.assertRaises(self.module.PublicationError):
                self.publish()
        self.assertEqual(len(self.api.posts), 1)

    def test_ambiguous_transport_failure_is_terminal(self):
        self.api.failure = TimeoutError("acceptance unknown")
        for _ in range(2):
            with self.assertRaises(self.module.PublicationError):
                self.publish()
        self.assertEqual(len(self.api.posts), 1)

    def test_stale_closed_and_wrong_account_never_post(self):
        for field, value in [("head", "b" * 40), ("state", "closed"), ("login", "other")]:
            previous = getattr(self.api, field)
            setattr(self.api, field, value)
            with self.assertRaises(self.module.PublicationError):
                self.publish()
            setattr(self.api, field, previous)
        self.assertEqual(self.api.posts, [])

    def review(self, body=BODY, state="APPROVED"):
        return {"body": body, "commit_id": HEAD, "state": state,
                "user": {"login": "review-bot"}, "submitted_at": "2026-10-30T10:00:00Z"}

    def test_existing_valid_empty_or_pending_review_blocks(self):
        for review in [self.review(), self.review(body=""), self.review(state="PENDING")]:
            self.api.reviews = [review]
            with self.assertRaises(self.module.PublicationError):
                self.publish()
        self.assertEqual(self.api.posts, [])

    def test_later_third_party_clarification_allows_new_task(self):
        self.api.reviews = [self.review()]
        self.api.comments = [{"user": {"login": "maintainer"}, "created_at": "2026-10-30T11:00:00Z"}]
        self.publish()
        self.assertEqual(len(self.api.posts), 1)

    def test_empty_review_is_not_replaced_after_clarification(self):
        self.api.reviews = [self.review(body="")]
        self.api.comments = [{"user": {"login": "maintainer"}, "created_at": "2026-10-30T11:00:00Z"}]
        with self.assertRaises(self.module.PublicationError):
            self.publish()
        self.assertEqual(self.api.posts, [])

    def test_self_review_uses_comment_without_failed_approval(self):
        self.api.author = "review-bot"
        self.publish()
        self.assertEqual(self.api.posts[0]["event"], "COMMENT")

    def test_verdict_event_mismatch_never_posts(self):
        with self.assertRaises(self.module.PublicationError):
            self.publish(self.payload(body=BODY.replace("✅ APPROVED", "🔄 CHANGES REQUESTED")))
        self.assertEqual(self.api.posts, [])

    def test_comment_and_scope_gate_verdicts(self):
        for verdict in ["🔄 CHANGES REQUESTED", "🛑 MAINTAINER DECISION REQUIRED"]:
            self.receipt.unlink(missing_ok=True)
            self.publish(self.payload(event="COMMENT", body=BODY.replace("✅ APPROVED", verdict)))
        self.assertEqual(len(self.api.posts), 2)

    def test_inline_rejection_fallback_preserves_every_finding(self):
        original = self.api.request
        rejection = self.module.Rejected(422, {"errors": [
            {"resource": "PullRequestReviewComment", "field": "line"}]})

        def request(method, path, body=None):
            if method == "POST" and not self.api.posts:
                self.api.posts.append(body)
                raise rejection
            return original(method, path, body)

        self.api.request = request
        findings = [{"path": "file.py", "line": 10, "side": "RIGHT", "body": "Important defect."}]
        payload = self.payload(event="COMMENT", comments=findings,
                               body=BODY.replace("✅ APPROVED", "🔄 CHANGES REQUESTED"))
        self.publish(payload)
        self.assertEqual(len(self.api.posts), 2)
        self.assertNotIn("comments", self.api.posts[-1])
        self.assertIn("Important defect.", self.api.posts[-1]["body"])
        with self.assertRaises(self.module.PublicationError):
            self.publish(payload)
        self.assertEqual(len(self.api.posts), 2)

    def test_other_rejections_do_not_retry(self):
        for status in [403, 422, 500]:
            self.receipt.unlink(missing_ok=True)
            self.api.failure = self.module.Rejected(status, {"message": "Unrelated failure"})
            with self.assertRaises(self.module.PublicationError):
                self.publish()
        self.assertEqual(len(self.api.posts), 3)

    def test_rejected_self_approval_has_one_comment_fallback(self):
        original = self.api.request

        def request(method, path, body=None):
            if method == "POST" and not self.api.posts:
                self.api.posts.append(body)
                raise self.module.Rejected(422, {"message": "Cannot approve your own pull request"})
            return original(method, path, body)

        self.api.request = request
        self.publish()
        self.assertEqual([p["event"] for p in self.api.posts], ["APPROVE", "COMMENT"])
        self.assertEqual(self.api.posts[-1]["body"], BODY)

    def test_recorded_intent_after_crash_never_posts(self):
        self.receipt.write_text('{"state": "intent"}')
        with self.assertRaises(self.module.PublicationError):
            self.publish()
        self.assertEqual(self.api.posts, [])

    def test_real_http_transport_preserves_json_body(self):
        captured = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(captured[-1]).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = self.module.GitHub("test-credential", f"http://127.0.0.1:{server.server_port}")
            self.assertEqual(client.request("POST", "/reviews", self.payload()), self.payload())
        finally:
            server.shutdown()
            thread.join()
        self.assertEqual(captured, [self.payload()])

    def test_cli_missing_and_malformed_payload_fail_before_network(self):
        for text in [None, "{", json.dumps(self.payload(body=""))]:
            path = Path(self.directory.name) / "payload.json"
            if text is not None:
                path.write_text(text)
            runner = Path(self.directory.name) / "runner.py"
            runner.write_text(
                "import runpy, sys\n"
                "def audit(event, args):\n"
                "    if event in ('socket.getaddrinfo', 'socket.connect'):\n"
                "        raise SystemExit('UNEXPECTED_NETWORK_REQUEST')\n"
                "sys.addaudithook(audit)\n"
                "source = sys.argv.pop(1)\n"
                "runpy.run_path(source, run_name='__main__')\n"
            )
            result = subprocess.run([
                sys.executable, "-B", str(runner), str(MODULES[0]), "--repo", "owner/repo", "--number", "1",
                "--head", HEAD, "--secret", "REVIEW_TEST_CREDENTIAL", "--receipt", str(self.receipt), str(path),
            ], capture_output=True, text=True, env={"PATH": os.environ.get("PATH", ""), "REVIEW_TEST_CREDENTIAL": "local-test-only"})
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("GITHUB_REVIEW_POSTED", result.stdout)
            self.assertIn("REVIEW_PUBLICATION_STOPPED", result.stderr)
            self.assertNotIn("UNEXPECTED_NETWORK_REQUEST", result.stderr)

    def test_generated_prompt_delivers_exact_helper_and_fixed_target(self):
        for helper_path in MODULES:
            path = helper_path.with_name("main.py")
            sys.path.insert(0, str(path.parent))
            try:
                module = load(path)
            finally:
                sys.path.pop(0)
            pr = {"number": 1, "head": {"sha": HEAD}, "user": {"login": "contributor"}}
            prompt = module._build_review_prompt(
                "owner/repo", pr, HEAD, {"id": 12}, reviewer_login="review-bot",
                github_token_secret="REVIEW_TEST_SECRET",
            )
            source = prompt.split("<<'OPENHANDS_REVIEW_PUBLISHER'\n", 1)[1].split("OPENHANDS_REVIEW_PUBLISHER\n", 1)[0]
            self.assertEqual(source, helper_path.read_text())
            compile(source, "delivered-helper", "exec")
            command = re.findall(r"```sh\n(python3 .*?)\n```", prompt)[-1]
            self.assertIn("--reviewer review-bot", command)
            self.assertIn("--repo owner/repo", command)
            self.assertIn("--head " + HEAD, command)
            self.assertIn("--secret REVIEW_TEST_SECRET", command)
            self.assertIn("scope-stop reviews", prompt)
            other = module._build_review_prompt("owner/repo", pr, HEAD, {"id": 13})
            self.assertNotEqual(re.findall(r"--receipt (\S+)", prompt),
                                re.findall(r"--receipt (\S+)", other))

    def test_worker_delivers_helper_for_each_trigger_mode(self):
        for helper_path in MODULES:
            sys.path.insert(0, str(helper_path.parent))
            try:
                workflow = load(helper_path.with_name("main.py"))
            finally:
                sys.path.pop(0)
            tree = ast.parse(helper_path.with_name("worker.py").read_text())
            reviewer = next(node for node in tree.body
                            if isinstance(node, ast.ClassDef) and node.name == "PullRequestReviewer")
            methods = [node for node in reviewer.body if isinstance(node, ast.FunctionDef)
                       and node.name in ("_prompt", "_start_review")]
            namespace = {"workflow": workflow, "json": json}
            exec(compile(ast.Module(body=methods, type_ignores=[]), "worker-methods", "exec"), namespace)
            deliveries = []

            class DispatcherBoundary:
                def deliver(self, **delivery):
                    deliveries.append(delivery)
                    return {"disposition": "delivered", "conversation_id": "local-only"}

            worker = SimpleNamespace(repository="owner/repo", token_name="REVIEW_TEST_CREDENTIAL",
                                     trigger_reviewer="review-bot", dispatcher=DispatcherBoundary())
            worker._prompt = MethodType(namespace["_prompt"], worker)
            pr = {"number": 1, "head": {"sha": HEAD}, "user": {"login": "contributor"}}
            modes = [({"id": 12}, None, None), ({"id": 12}, None, None),
                     ({"id": 13}, None, None), ({"id": 14}, "review-label", None),
                     (None, None, "scheduled-head")]
            for trigger, label, key in modes:
                namespace["_start_review"](worker, 10, pr, trigger, HEAD, label, key)
            receipts = []
            for delivery in deliveries:
                self.assertEqual(delivery["subject"], "10:pr:1")
                prompt = delivery["prompt"]
                self.assertIn(helper_path.read_text(), prompt)
                self.assertIn("--reviewer review-bot", prompt)
                self.assertIn("--secret REVIEW_TEST_CREDENTIAL", prompt)
                receipts.append(re.findall(r"--receipt (\S+)", prompt)[-1])
            self.assertEqual(receipts[0], receipts[1])
            self.assertNotEqual(receipts[0], receipts[2])
            self.assertEqual(len(set(receipts)), 4)
            self.assertEqual(deliveries[-1]["delivery"], "scheduled-head")

    def test_modules_are_identical_and_standalone(self):
        self.assertEqual(MODULES[0].read_bytes(), MODULES[1].read_bytes())
        for path in MODULES:
            module = load(path)
            module.validate_payload(self.payload(), HEAD)


if __name__ == "__main__":
    unittest.main()
