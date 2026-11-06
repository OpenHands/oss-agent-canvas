import importlib.util
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "plugins/github-pr-reviewer-events/scripts/publish_review.py"
)
spec = importlib.util.spec_from_file_location("publish_review", SOURCE)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)

HEAD = "a" * 40
BODY = "_This review was posted by an AI agent (OpenHands)._\n\nNo material findings.\n\n✅ APPROVED"


class GitHubBoundary(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/user":
            response = {"login": "review-bot"}
        elif self.path == "/repos/example/project/pulls/7":
            response = {
                "head": {"sha": self.server.head},
                "user": {"login": self.server.author},
            }
        elif "/reviews?" in self.path:
            response = self.server.reviews
            if self.server.move_during_discovery:
                self.server.head = "b" * 40
        else:
            response = self.server.comments
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.posts.append(payload)
        self.server.post_started.set()
        self.server.release_post.wait(3)
        if self.server.reject_inline and payload.get("comments"):
            self.send_response(422)
            response = {
                "errors": [
                    {
                        "resource": "PullRequestReviewComment",
                        "field": "line",
                        "code": "invalid",
                    }
                ]
            }
            if self.server.move_after_rejection:
                self.server.head = "b" * 40
        elif self.server.reject:
            self.send_response(422)
            response = self.server.rejection_body
        else:
            self.send_response(200)
            response = {
                "id": 91,
                "body": payload["body"],
                "commit_id": HEAD,
                "state": "APPROVED" if payload["event"] == "APPROVE" else "COMMENTED",
                "user": {"login": "review-bot"},
            }
            response.update(self.server.receipt_overrides)
            self.server.reviews.append(response)
        if self.server.drop_response and not payload.get("comments"):
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.payload_file = self.directory / "review.json"
        self.payload = {"commit_id": HEAD, "event": "APPROVE", "body": BODY}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GitHubBoundary)
        self.server.head = HEAD
        self.server.author = "contributor"
        self.server.posts = []
        self.server.reviews = []
        self.server.comments = []
        self.server.receipt_overrides = {}
        self.server.reject_inline = False
        self.server.reject = False
        self.server.rejection_body = {"message": "Unprocessable Entity"}
        self.server.drop_response = False
        self.server.move_during_discovery = False
        self.server.move_after_rejection = False
        self.server.post_started = threading.Event()
        self.server.release_post = threading.Event()
        self.server.release_post.set()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.api = publisher.GitHub(
            "test-only", f"http://127.0.0.1:{self.server.server_port}"
        )

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def publish(self, write=True):
        if write:
            self.payload_file.write_text(json.dumps(self.payload))
        return publisher.publish(
            self.payload_file,
            "example/project",
            7,
            HEAD,
            "request-1",
            self.api,
            self.directory / "state",
        )

    def test_task_payload_paths_isolate_identical_heads(self):
        first = publisher.payload_path("example/project", 7, HEAD, "delivery-a")
        second = publisher.payload_path("example/project", 8, HEAD, "delivery-b")
        self.assertNotEqual(first, second)
        self.assertEqual(
            first, publisher.payload_path("EXAMPLE/project", 7, HEAD, "delivery-a")
        )
        self.assertNotEqual(
            first, publisher.payload_path("example/project", 7, HEAD, "delivery-b")
        )
        self.payload_file = self.directory / Path(first).name
        self.payload_file.write_text(json.dumps(self.payload))
        other = {
            **self.payload,
            "body": BODY.replace("No material findings.", "Different PR assessment."),
        }
        (self.directory / Path(second).name).write_text(json.dumps(other))
        self.assertEqual(self.publish(write=False)["body"], BODY)
        self.assertEqual(self.server.posts[0]["body"], BODY)

    def test_missing_file_never_posts(self):
        with self.assertRaises((OSError, ValueError)):
            self.publish(write=False)
        self.assertEqual(self.server.posts, [])

    def test_invalid_payloads_never_post(self):
        for change in (
            {"body": ""},
            {"body": " \n "},
            {"body": "✅ APPROVED"},
            {"body": BODY.replace("No material findings.", "")},
            {"commit_id": "b" * 40},
            {"event": "REQUEST_CHANGES"},
            {"comments": [{"path": "a.py", "line": 0, "side": "RIGHT", "body": "bug"}]},
        ):
            with self.subTest(change=change):
                self.payload = {
                    "commit_id": HEAD,
                    "event": "APPROVE",
                    "body": BODY,
                    **change,
                }
                with self.assertRaises(ValueError):
                    self.publish()
        self.assertEqual(self.server.posts, [])

    def test_valid_review_is_verified_and_repeat_does_not_post(self):
        self.assertEqual(self.publish()["id"], 91)
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(self.server.posts[0], self.payload)

    def test_changed_head_never_posts(self):
        self.server.head = "b" * 40
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.server.posts, [])

    def test_existing_empty_or_pending_review_blocks_replacement(self):
        for state in ("APPROVED", "PENDING"):
            self.server.reviews = [
                {
                    "id": 10,
                    "commit_id": HEAD,
                    "state": state,
                    "body": "",
                    "user": {"login": "review-bot"},
                }
            ]
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assertEqual(self.server.posts, [])

    def test_mismatched_receipt_never_retries(self):
        for change in (
            {"body": ""},
            {"body": "truncated"},
            {"commit_id": "b" * 40},
            {"state": "PENDING"},
            {"user": {"login": "someone-else"}},
            {"id": None},
        ):
            with self.subTest(change=change):
                self.server.receipt_overrides = change
                with self.assertRaises(RuntimeError):
                    self.publish()
                with self.assertRaises(RuntimeError):
                    self.publish()
                self.assertEqual(len(self.server.posts), 1)
                journal = json.loads(
                    next((self.directory / "state").glob("*.json")).read_text()
                )
                self.assertEqual(journal["status"], "unverified")
                self.assertEqual(journal["id"], change.get("id", 91))
                self.assertIn("verification", journal["reason"])
                with self.assertRaises(RuntimeError) as blocked:
                    publisher.publish(
                        self.payload_file,
                        "example/project",
                        7,
                        HEAD,
                        "different-delivery",
                        self.api,
                        self.directory / "state",
                    )
                self.assertIn(f"review {change.get('id', 91)}", str(blocked.exception))
                self.assertIn(
                    str(next((self.directory / "state").glob("*.json"))),
                    str(blocked.exception),
                )
                self.assertEqual(len(self.server.posts), 1)
                self.server.posts.clear()
                self.server.reviews.clear()
                for path in (self.directory / "state").glob("*"):
                    path.unlink()

    def test_self_approval_uses_comment_without_rejected_post(self):
        self.server.author = "review-bot"
        self.assertEqual(self.publish()["state"], "COMMENTED")
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(self.server.posts[0]["body"], BODY)

    def test_inline_rejection_moves_findings_into_body_once(self):
        self.server.reject_inline = True
        self.payload["event"] = "COMMENT"
        self.payload["body"] = BODY.replace(
            "No material findings.", "One actionable finding."
        ).replace("✅ APPROVED", "🔄 CHANGES REQUESTED")
        self.payload["comments"] = [
            {
                "path": "a.py",
                "line": 8,
                "side": "RIGHT",
                "body": "Demonstrated failure.",
            },
            {
                "path": "b.py",
                "line": 9,
                "side": "RIGHT",
                "body": "Another demonstrated failure.",
            },
        ]
        self.publish()
        self.assertEqual(len(self.server.posts), 2)
        self.assertNotIn("comments", self.server.posts[-1])
        self.assertIn("a.py:8", self.server.posts[-1]["body"])
        self.assertIn("Demonstrated failure.", self.server.posts[-1]["body"])
        self.assertIn(
            "b.py:9: Another demonstrated failure.", self.server.posts[-1]["body"]
        )
        self.assertIn("One actionable finding.", self.server.posts[-1]["body"])
        self.assertTrue(self.server.posts[-1]["body"].startswith(publisher.DISCLOSURE))
        self.assertTrue(self.server.posts[-1]["body"].endswith("🔄 CHANGES REQUESTED"))
        self.assertEqual(len(self.server.reviews), 1)

    def test_unspecified_422_does_not_trigger_fallback(self):
        self.server.reject = True
        with self.assertRaises(RuntimeError):
            self.publish()
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(len(self.server.posts), 1)

    def test_external_comment_allows_fresh_review_but_not_empty_repair(self):
        self.server.reviews = [
            {
                "id": 10,
                "commit_id": HEAD,
                "state": "APPROVED",
                "body": BODY,
                "user": {"login": "review-bot"},
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ]
        self.server.comments = [
            {"user": {"login": "maintainer"}, "created_at": "2026-01-02T00:00:00Z"}
        ]
        self.assertEqual(self.publish()["id"], 91)

    def test_head_moves_during_discovery(self):
        self.server.move_during_discovery = True
        with self.assertRaisesRegex(RuntimeError, "head moved"):
            self.publish()
        self.assertEqual(self.server.posts, [])

    def test_head_moves_before_inline_fallback(self):
        self.server.reject_inline = True
        self.server.move_after_rejection = True
        self.payload["comments"] = [
            {"path": "a.py", "line": 8, "side": "RIGHT", "body": "Bug."}
        ]
        with self.assertRaisesRegex(RuntimeError, "head moved"):
            self.publish()
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(self.server.reviews, [])

    def test_dropped_response_is_not_retried(self):
        self.server.drop_response = True
        with self.assertRaisesRegex(RuntimeError, "unverified"):
            self.publish()
        with self.assertRaisesRegex(RuntimeError, "unverified"):
            self.publish()
        self.assertEqual(len(self.server.posts), 1)
        journal = json.loads(
            next((self.directory / "state").glob("*.json")).read_text()
        )
        self.assertEqual(journal["status"], "unverified")

    def test_concurrent_distinct_deliveries_share_head_lock(self):
        self.payload_file.write_text(json.dumps(self.payload))
        self.server.release_post.clear()
        result = []
        thread = threading.Thread(
            target=lambda: result.append(self.publish(write=False))
        )
        thread.start()
        self.assertTrue(self.server.post_started.wait(3))
        try:
            with self.assertRaisesRegex(RuntimeError, "in progress"):
                publisher.publish(
                    self.payload_file,
                    "example/project",
                    7,
                    HEAD,
                    "request-2",
                    self.api,
                    self.directory / "state",
                )
        finally:
            self.server.release_post.set()
            thread.join(5)
        self.assertEqual(len(self.server.posts), 1)
        self.assertEqual(result[0]["id"], 91)

    def test_mixed_rejection_does_not_trigger_fallback(self):
        self.server.reject = True
        self.server.rejection_body = {
            "errors": [
                {
                    "resource": "PullRequestReviewComment",
                    "field": "line",
                    "code": "invalid",
                },
                {"resource": "PullRequestReview", "field": "body", "code": "invalid"},
            ]
        }
        self.payload["comments"] = [
            {"path": "a.py", "line": 8, "side": "RIGHT", "body": "Bug."}
        ]
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(len(self.server.posts), 1)

    def test_uncertain_fallback_is_not_replaced(self):
        self.server.reject_inline = True
        self.server.drop_response = True
        self.payload["comments"] = [
            {"path": "a.py", "line": 8, "side": "RIGHT", "body": "Bug."}
        ]
        with self.assertRaisesRegex(RuntimeError, "unverified"):
            self.publish()
        self.assertEqual(len(self.server.reviews), 1)
        with self.assertRaisesRegex(RuntimeError, "unverified"):
            publisher.publish(
                self.payload_file,
                "example/project",
                7,
                HEAD,
                "request-2",
                self.api,
                self.directory / "state",
            )
        self.assertEqual(len(self.server.posts), 2)

    def test_invisible_verified_review_is_not_replaced(self):
        self.publish()
        self.server.reviews = []
        with self.assertRaisesRegex(RuntimeError, "not visible"):
            publisher.publish(
                self.payload_file,
                "example/project",
                7,
                HEAD,
                "request-2",
                self.api,
                self.directory / "state",
            )
        self.assertEqual(len(self.server.posts), 1)

    def test_existing_complete_review_without_new_discussion_blocks_duplicate(self):
        self.server.reviews = [
            {
                "id": 10,
                "commit_id": HEAD,
                "state": "APPROVED",
                "body": BODY,
                "user": {"login": "review-bot"},
                "submitted_at": "2026-01-01T00:00:00Z",
            }
        ]
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.server.posts, [])


if __name__ == "__main__":
    unittest.main()
