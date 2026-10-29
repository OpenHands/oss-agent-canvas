import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted(ROOT.glob("**/maintainer_handoff.py"))


class RepositoryBoundary:
    """In-memory GitHub boundary; tests must never send real review requests."""

    repository = "OpenHands/OpenHands"

    def __init__(self, reviews):
        self.reviews = reviews
        self.requests = []

    def gh_pages(self, path):
        if path.endswith("/reviews"):
            return self.reviews
        return []

    def api(self, method, path, params):
        return {"total_count": 0}

    def gh(self, method, path, body):
        self.requests.append((method, path, body))


class HandoffTests(unittest.TestCase):
    def check_handoff(self, reviews, expected_requests, requested=None):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                spec = importlib.util.spec_from_file_location("handoff", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                repository = RepositoryBoundary(reviews)
                pr = {
                    "number": 18913,
                    "head": {"sha": "current"},
                    "user": {"login": "contributor"},
                    "requested_reviewers": requested or [],
                }
                self.assertEqual(
                    module.request_maintainer_review(repository, pr, ["neubig"]),
                    "neubig",
                )
                self.assertEqual(len(repository.requests), expected_requests)

    @staticmethod
    def review(state, head="current", login="neubig"):
        return {"state": state, "commit_id": head, "user": {"login": login}}

    def test_comment_only_feedback_prevents_repeated_handoff(self):
        self.check_handoff(
            [
                self.review("APPROVED", login="all-hands-bot"),
                self.review("COMMENTED"),
                self.review("COMMENTED"),
            ],
            0,
        )

    def test_old_head_comment_does_not_block_new_head_handoff(self):
        self.check_handoff([self.review("COMMENTED", "old")], 1)

    def test_old_head_approval_still_prevents_handoff(self):
        self.check_handoff([self.review("APPROVED", "old")], 0)

    def test_comment_does_not_erase_approval_across_heads(self):
        self.check_handoff(
            [self.review("APPROVED", "old"), self.review("COMMENTED", "old")], 0
        )

    def test_dismissed_comment_allows_handoff(self):
        self.check_handoff([self.review("COMMENTED"), self.review("DISMISSED")], 1)

    def test_changes_requested_on_current_head_prevents_handoff(self):
        self.check_handoff([self.review("CHANGES_REQUESTED")], 0)

    def test_first_handoff_requests_review(self):
        self.check_handoff([], 1)

    def test_existing_request_is_not_duplicated(self):
        self.check_handoff([], 0, [{"login": "neubig"}])


if __name__ == "__main__":
    unittest.main()
