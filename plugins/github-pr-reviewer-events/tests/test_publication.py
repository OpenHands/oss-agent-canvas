import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from review_gateway import ReviewGateway
from review_publication import DISCLOSURE, ReviewPublisher, validate_proposal

SHA = "a" * 40
PROPOSAL = {
    "decision": "approve",
    "summary": "Tests pass; no material findings.",
    "comments": [],
}


class GitHub:
    def __init__(self):
        self.pr = {"state": "open", "head": {"sha": SHA}, "user": {"login": "author"}}
        self.reviews, self.comments, self.writes = [], [], []
        self.failure, self.readback_bad, self.after_post = None, False, False

    def api(self, method, path, body=None):
        path = path.split("?")[0]
        if method == "POST":
            self.writes.append(body)
            if self.failure:
                raise self.failure
            review = {
                "id": 1,
                "body": body["body"],
                "commit_id": body["commit_id"],
                "state": "APPROVED" if body["event"] == "APPROVE" else "COMMENTED",
                "user": {"login": "bot"},
                "submitted_at": "2026-10-28T00:00:00Z",
            }
            self.reviews.append(review)
            if self.after_post:
                self.pr["head"]["sha"] = "b" * 40
            return dict(review)
        if path.endswith("/reviews"):
            return self.reviews
        if "/reviews/" in path:
            return {**self.reviews[-1], **({"body": ""} if self.readback_bad else {})}
        if path.endswith("/comments"):
            return self.comments
        if path.endswith("/files"):
            return [{"filename": "a.py", "patch": "@@ -1 +1 @@\n-old\n+new"}]
        return self.pr


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.api = GitHub()
        self.publisher = self.make_publisher()

    def make_publisher(self, delivery="event-1"):
        return ReviewPublisher(
            self.api.api, "owner/repo", 7, SHA, "bot", delivery, self.directory.name
        )

    def test_success_is_pinned_canonical_and_verified(self):
        result = self.publisher.publish(PROPOSAL)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["final_response"], "GITHUB_REVIEW_POSTED")
        body = self.api.writes[0]
        self.assertEqual(body["commit_id"], SHA)
        self.assertTrue(body["body"].startswith(DISCLOSURE))
        self.assertTrue(body["body"].endswith("✅ APPROVED"))
        self.assertEqual(
            self.publisher.publish(PROPOSAL)["status"], "already_published"
        )
        self.assertEqual(len(self.api.writes), 1)

    def test_changed_head_blocks_all_writes(self):
        self.api.pr["head"]["sha"] = "b" * 40
        self.assertEqual(self.publisher.publish(PROPOSAL)["status"], "head_changed")
        self.assertFalse(self.api.writes)

    def test_move_between_prechecks_is_blocked(self):
        original = self.api.api
        reads = []

        def api(method, path, body=None):
            if method == "GET" and path.endswith("/pulls/7"):
                reads.append(path)
                if len(reads) == 3:
                    self.api.pr["head"]["sha"] = "b" * 40
            return original(method, path, body)

        self.publisher.api = api
        self.assertEqual(self.publisher.publish(PROPOSAL)["status"], "head_changed")
        self.assertFalse(self.api.writes)

    def test_move_during_post_is_detected_without_false_completion(self):
        self.api.after_post = True
        result = self.publisher.publish(PROPOSAL)
        self.assertEqual(result["status"], "head_changed_after_post")
        self.assertNotIn("final_response", result)
        self.assertEqual(self.api.writes[0]["commit_id"], SHA)

    def test_uncertain_write_is_never_retried(self):
        self.api.failure = TimeoutError()
        self.assertEqual(self.publisher.publish(PROPOSAL)["status"], "needs_operator")
        self.api.failure = None
        self.assertEqual(self.publisher.publish(PROPOSAL)["status"], "needs_operator")
        self.assertEqual(len(self.api.writes), 1)

    def test_malformed_accepted_readback_is_never_replaced(self):
        self.api.readback_bad = True
        self.assertEqual(self.publisher.publish(PROPOSAL)["status"], "needs_operator")
        self.publisher.publish(PROPOSAL)
        self.assertEqual(len(self.api.writes), 1)

    def test_empty_existing_review_blocks_publication(self):
        self.api.reviews.append(
            {
                "id": 1,
                "body": "",
                "commit_id": SHA,
                "user": {"login": "bot"},
                "submitted_at": "2026-10-28",
            }
        )
        self.assertEqual(self.publisher.publish(PROPOSAL)["status"], "needs_operator")
        self.assertFalse(self.api.writes)

    def test_other_delivery_requires_clarification(self):
        self.publisher.publish(PROPOSAL)
        next_publisher = self.make_publisher("event-2")
        self.assertEqual(next_publisher.publish(PROPOSAL)["status"], "already_reviewed")
        self.api.comments = [{"user": {"login": "human"}, "created_at": "2026-10-29"}]
        self.assertEqual(next_publisher.publish(PROPOSAL)["status"], "published")
        self.assertEqual(len(self.api.writes), 2)

    def test_self_approval_uses_comment(self):
        self.api.pr["user"]["login"] = "bot"
        self.publisher.publish(PROPOSAL)
        self.assertEqual(self.api.writes[0]["event"], "COMMENT")
        self.assertTrue(self.api.writes[0]["body"].endswith("✅ APPROVED"))

    def test_invalid_inline_locations_go_in_body(self):
        proposal = {
            "decision": "changes",
            "summary": "Bug",
            "comments": [{"path": "a.py", "line": 9, "body": "Wrong result"}],
        }
        self.publisher.publish(proposal)
        self.assertFalse(self.api.writes[0]["comments"])
        self.assertIn("a.py:9: Wrong result", self.api.writes[0]["body"])

    def test_inline_rejection_falls_back_only_after_explicit_rejection(self):
        original = self.api.api

        def api(method, path, body=None):
            if method == "POST" and body.get("comments"):
                self.api.writes.append(body)
                raise HTTPError(
                    path,
                    422,
                    "bad inline",
                    {},
                    io.BytesIO(b'{"message":"Invalid diff position"}'),
                )
            return original(method, path, body)

        self.publisher.api = api
        proposal = {
            "decision": "changes",
            "summary": "Bug",
            "comments": [{"path": "a.py", "line": 1, "body": "Wrong result"}],
        }
        self.assertEqual(self.publisher.publish(proposal)["status"], "published")
        self.assertEqual(len(self.api.writes), 2)
        self.assertFalse(self.api.writes[-1]["comments"])

    def test_scope_stop_and_skip(self):
        result = self.publisher.publish({**PROPOSAL, "decision": "skip"})
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(self.api.writes)
        self.publisher.publish({**PROPOSAL, "decision": "scope_stop"})
        self.assertTrue(
            self.api.writes[0]["body"].endswith("🛑 MAINTAINER DECISION REQUIRED")
        )

    def test_parallel_publisher_cannot_enter(self):
        with self.publisher.locked():
            with self.assertRaises(RuntimeError):
                self.make_publisher().publish(PROPOSAL)

    def test_target_and_event_cannot_be_agent_controlled(self):
        for proposal in (
            {**PROPOSAL, "commit_id": "b" * 40},
            {**PROPOSAL, "event": "APPROVE"},
            {**PROPOSAL, "decision": []},
            {**PROPOSAL, "summary": ""},
        ):
            with self.assertRaises((ValueError, TypeError)):
                self.publisher.publish(proposal)
        self.assertFalse(self.api.writes)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.reads = []
        self.gateway = ReviewGateway(
            "owner/repo", 7, SHA, "job-token", self.read, lambda: b"archive"
        )

    def read(self, path):
        self.reads.append(path)
        return {"ok": True}

    def request(self, method, path, body=None, token="job-token"):
        return self.gateway.handle(
            method, path, json.dumps(body).encode() if body else b"", "Bearer " + token
        )

    def test_only_scoped_reads_and_proposals_are_allowed(self):
        self.assertEqual(
            self.request("GET", "/github/repos/owner/repo/pulls/7")[0], 200
        )
        for path in (
            "/github/repos/other/repo",
            "/github/repos/owner/repo/../other",
            "/github/repos/owner/repo/%2e%2e/other",
        ):
            self.assertEqual(self.request("GET", path)[0], 403)
        self.assertEqual(
            self.request("POST", "/github/repos/owner/repo/pulls/7/reviews", PROPOSAL)[
                0
            ],
            405,
        )
        self.assertEqual(self.request("GET", "/checkout", token="wrong")[0], 401)
        self.assertEqual(len(self.reads), 1)

    def test_one_immutable_proposal_is_not_publication(self):
        code, result = self.request("POST", "/proposal", PROPOSAL)
        self.assertEqual(code, 200)
        self.assertFalse(result["published"])
        self.assertEqual(self.request("POST", "/proposal", PROPOSAL)[0], 200)
        self.assertEqual(
            self.request("POST", "/proposal", {**PROPOSAL, "summary": "Changed"})[0],
            409,
        )
        self.assertEqual(
            self.request("POST", "/proposal", {**PROPOSAL, "target": "other"})[0], 400
        )
        self.assertFalse(self.reads)


if __name__ == "__main__":
    unittest.main()
