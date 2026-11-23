import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "plugins/github-pr-reviewer-events/scripts"
)
SPEC = importlib.util.spec_from_file_location("review_publisher", SCRIPTS / "review_publisher.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)

HEAD = "a" * 40
DISCLOSURE = "_This review was posted by an AI agent (OpenHands)._"
APPROVAL = f"{DISCLOSURE}\n\nReviewed the tests. No findings.\n\n✅ APPROVED"


class GitHubBoundary(BaseHTTPRequestHandler):
    def do_POST(self):
        sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, sent))
        status, overrides = self.server.responses.pop(0) if self.server.responses else (201, {})
        received = {
            "id": 321,
            "body": sent["body"],
            "commit_id": sent["commit_id"],
            "state": {"APPROVE": "APPROVED", "COMMENT": "COMMENTED"}[sent["event"]],
        }
        received.update(overrides)
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(received).encode())

    def log_message(self, format, *args):
        pass


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.stage = Path(self.temp) / "review.json"
        self.receipt = Path(self.temp) / "publish.receipt"
        self.server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), GitHubBoundary))
        self.server.requests = []
        self.server.responses = []
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.enterContext(patch.object(publisher, "GITHUB_API", f"http://127.0.0.1:{self.server.server_port}"))
        self.enterContext(patch.dict(os.environ, {"GH_TOKEN": "test-only"}))

    def publish(self, **payload):
        if payload:
            self.stage.write_text(json.dumps(payload), encoding="utf-8")
        return publisher.publish(self.stage, self.receipt, "owner/repo", 42, HEAD)

    def test_missing_empty_or_invalid_stage_makes_no_request(self):
        with self.assertRaises(publisher.PublicationError):
            self.publish()
        for content in [b"", b"  ", b"{}", b"{", b"\xff", b"null"]:
            with self.subTest(content=content):
                self.stage.write_bytes(content)
                with self.assertRaises(publisher.PublicationError):
                    self.publish()
        self.assertEqual(self.server.requests, [])
        self.assertFalse(self.receipt.exists())

    def test_body_and_event_contract_is_checked_before_a_request(self):
        invalid = [
            {"body": ""},
            {"body": " \n\t"},
            {"body": None},
            {"body": APPROVAL.replace(DISCLOSURE, "")},
            {"body": f"{DISCLOSURE}\n\n✅ APPROVED"},
            {"body": APPROVAL.replace("\n\n✅ APPROVED", " ✅ APPROVED")},
            {"body": APPROVAL + "\nMore content"},
            {"body": APPROVAL.replace("✅ APPROVED", "🔄 CHANGES REQUESTED")},
            {"event": "REQUEST_CHANGES"},
            {"event": []},
            {"commit_id": "b" * 40},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(publisher.PublicationError):
                    self.publish(**({"body": APPROVAL, "event": "APPROVE", "commit_id": HEAD} | changes))
        self.assertEqual(self.server.requests, [])
        self.assertFalse(self.receipt.exists())

    def test_valid_body_and_inline_findings_are_sent_exactly_once(self):
        body = APPROVAL.replace("✅ APPROVED", "🔄 CHANGES REQUESTED") + "\n"
        payload = {
            "body": body,
            "event": "COMMENT",
            "commit_id": HEAD,
            "comments": [{"path": "app.py", "line": 5, "side": "RIGHT", "body": "Check this case."}],
        }
        self.assertEqual(self.publish(**payload), 321)
        self.assertEqual(self.server.requests, [("/repos/owner/repo/pulls/42/reviews", payload)])
        with self.assertRaisesRegex(publisher.PublicationError, "Do not retry"):
            self.publish()
        self.assertEqual(len(self.server.requests), 1)

    def test_scope_stop_self_review_and_clean_review_keep_existing_outcomes(self):
        for event, body in (
            ("COMMENT", APPROVAL),
            ("COMMENT", APPROVAL.replace("✅ APPROVED", "🛑 MAINTAINER DECISION REQUIRED")),
            ("APPROVE", APPROVAL),
        ):
            with self.subTest(event=event, body=body):
                self.assertEqual(self.publish(event=event, body=body, commit_id=HEAD), 321)
                self.receipt.unlink()
        self.assertEqual(len(self.server.requests), 3)

    def test_no_repeat_after_mismatch_or_ambiguous_response(self):
        for status, changes in (
            (201, {"body": ""}),
            (201, {"state": "COMMENTED"}),
            (201, {"commit_id": "b" * 40}),
            (201, {"id": None}),
            (500, {}),
        ):
            with self.subTest(status=status, changes=changes):
                self.server.responses.append((status, changes))
                before = len(self.server.requests)
                with self.assertRaisesRegex(publisher.PublicationError, "Do not retry"):
                    self.publish(event="APPROVE", body=APPROVAL, commit_id=HEAD)
                with self.assertRaisesRegex(publisher.PublicationError, "Do not retry"):
                    self.publish()
                self.assertEqual(len(self.server.requests), before + 1)
                self.receipt.unlink()

    def test_explicit_validation_rejection_allows_corrected_fallback(self):
        self.server.responses.append((422, {}))
        with self.assertRaisesRegex(publisher.PublicationError, "rejected"):
            self.publish(event="APPROVE", body=APPROVAL, commit_id=HEAD)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.publish(event="COMMENT", body=APPROVAL, commit_id=HEAD), 321)
        self.assertEqual([data["event"] for _, data in self.server.requests], ["APPROVE", "COMMENT"])
