import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from openhands.sdk.conversation.exceptions import ConversationRunError
from openhands.sdk.event.conversation_error import ConversationErrorEvent

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [ROOT / "plugins" / name / "scripts" for name in (
    "github-pr-reviewer", "github-pr-reviewer-events"
)]


def load(path):
    spec = importlib.util.spec_from_file_location("review_delivery", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class ConversationBoundary:
    # Only the remote service is replaced; SDK error models and policy are real.
    def __init__(self, outcomes, status="idle"):
        self.outcomes = iter(outcomes)
        self.status = status
        self.events = []
        self.runs = 0
        self.published = False
        self.state = SimpleNamespace(refresh_from_server=self.refresh,
                                     events=self.events)

    def refresh(self):
        return {"execution_status": self.status}

    def run(self, **kwargs):
        self.runs += 1
        outcome = next(self.outcomes)
        if outcome == "timeout":
            self.status = "running"
            raise ConversationRunError(uuid4(), TimeoutError("still running"))
        if outcome == "published":
            self.published = True
            self.status = "finished"
        elif outcome == "finished":
            self.status = "finished"
        else:
            self.status = "error"
            error = ConversationErrorEvent(source="environment", code=outcome, detail="")
            self.events.append(error)
            raise ConversationRunError(uuid4(), RuntimeError("private detail"),
                                       conversation_error=error)


class DeliveryTests(unittest.TestCase):
    def execute(self, outcomes, *, status="idle", record=None, check=None,
                deadline=900):
        results = []
        for scripts in SCRIPTS:
            conversation = ConversationBoundary(outcomes, status)
            clock = Clock()
            saved = []
            state = dict(record or {})
            module = load(scripts / "review_delivery.py")
            result = module.complete_review(
                conversation,
                check or (lambda: "published" if conversation.published else "pending"),
                state, lambda: saved.append(dict(state)), deadline,
                clock=clock, sleep=clock.sleep,
            )
            results.append((result, conversation, clock, state, saved))
        return results

    def test_transient_recovers_without_new_prompt(self):
        for result, conversation, clock, record, saved in self.execute(
            ["LLMServiceUnavailableError", "published"]
        ):
            self.assertEqual(result, "published")
            self.assertEqual(conversation.runs, 2)
            self.assertEqual(record["attempts"], 2)
            self.assertEqual(clock.waits, [20])
            self.assertEqual(saved[-1]["outcome"], "published")

    def test_persistent_outage_is_bounded_and_incomplete(self):
        for result, conversation, clock, record, _ in self.execute(
            ["LLMServiceUnavailableError"] * 3
        ):
            self.assertEqual(result, "incomplete")
            self.assertEqual(conversation.runs, 3)
            self.assertEqual(clock.waits, [20, 60])
            self.assertEqual(record["reason"], "attempts-exhausted")

    def test_non_transient_failures_are_not_retried(self):
        for code in ("LLMAuthenticationError", "MaxBudgetReached",
                     "LLMBadRequestError", "AssertionError", "UnrecognizedError"):
            with self.subTest(code=code):
                for result, conversation, _, record, _ in self.execute([code]):
                    self.assertEqual(result, "incomplete")
                    self.assertEqual(conversation.runs, 1)
                    self.assertEqual(record["reason"], "non-transient-error")

    def test_accepted_review_wins_over_post_publication_failure(self):
        for result, conversation, _, _, _ in self.execute(
            [], status="error", check=lambda: "published"
        ):
            self.assertEqual(result, "published")
            self.assertEqual(conversation.runs, 0)

    def test_superseded_or_withdrawn_delivery_never_runs(self):
        for verdict in ("superseded", "request-withdrawn", "invalid-review"):
            for result, conversation, _, _, _ in self.execute(
                [], check=lambda: verdict
            ):
                self.assertIn(result, ("superseded", "incomplete"))
                self.assertEqual(conversation.runs, 0)

    def test_paused_and_finished_without_review_are_not_restarted(self):
        for status in ("paused", "finished", "stuck"):
            for result, conversation, _, _, _ in self.execute([], status=status):
                self.assertEqual(result, "incomplete")
                self.assertEqual(conversation.runs, 0)

    def test_remote_timeout_does_not_start_another_turn(self):
        for result, conversation, _, record, _ in self.execute(["timeout"]):
            self.assertEqual(result, "incomplete")
            self.assertEqual(conversation.runs, 1)
            self.assertEqual(record["reason"], "run-unconfirmed")

    def test_existing_running_turn_is_observed_not_counted_as_restart(self):
        for result, conversation, _, record, _ in self.execute(
            ["published"], status="running", record={"attempts": 1}
        ):
            self.assertEqual(result, "published")
            self.assertEqual(record["attempts"], 1)

    def test_deadline_stops_before_start(self):
        for result, conversation, _, record, _ in self.execute([], deadline=0):
            self.assertEqual(result, "incomplete")
            self.assertEqual(conversation.runs, 0)
            self.assertEqual(record["reason"], "deadline-exhausted")

    def test_exhausted_delivery_cannot_reset_budget(self):
        for result, conversation, _, record, _ in self.execute(
            [], record={"attempts": 3}
        ):
            self.assertEqual(result, "incomplete")
            self.assertEqual(conversation.runs, 0)
            self.assertEqual(record["reason"], "attempts-exhausted")

    def test_plugin_copies_match(self):
        self.assertEqual((SCRIPTS[0] / "review_delivery.py").read_bytes(),
                         (SCRIPTS[1] / "review_delivery.py").read_bytes())


class PublicationTests(unittest.TestCase):
    def workers(self):
        for scripts in SCRIPTS:
            sys.path.insert(0, str(scripts))
            try:
                for name in ("main", "agent_conversation", "github_client",
                             "maintainer_handoff", "review_delivery"):
                    sys.modules.pop(name, None)
                spec = importlib.util.spec_from_file_location("worker", scripts / "worker.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                yield module
            finally:
                sys.path.pop(0)

    def verdict(self, *, reviews=(), head="head", state="open", requested=True,
                label=None, delivery_key=None):
        results = []
        for module in self.workers():
            reviewer = module.PullRequestReviewer.__new__(module.PullRequestReviewer)
            reviewer.config = {}
            reviewer.github_login = "review-bot"
            # These are external GitHub reads, not review-generation substitutes.
            reviewer.gh = lambda *args: {
                "state": state, "head": {"sha": head}, "labels": [],
                "requested_reviewers": [{"login": "all-hands-bot"}] if requested else [],
            }
            reviewer.gh_pages = lambda *args: list(reviews)
            results.append(reviewer._review_delivery_state(
                1, "head", {"created_at": "2026-01-01T00:00:00Z"}, label, delivery_key
            ))
        return results

    @staticmethod
    def review(**changes):
        review = {"commit_id": "head", "user": {"login": "review-bot"},
                  "state": "APPROVED", "submitted_at": "2026-01-01T00:01:00Z",
                  "body": "Isolated test result.\n\n✅ APPROVED"}
        review.update(changes)
        return review

    def test_verified_native_review_is_delivered_even_when_request_removed(self):
        self.assertEqual(self.verdict(reviews=[self.review()], requested=False),
                         ["published"] * 2)

    def test_old_other_user_pending_and_dismissed_reviews_do_not_complete(self):
        for changes in ({"commit_id": "old"}, {"user": {"login": "human"}},
                        {"state": "PENDING"}, {"state": "DISMISSED"},
                        {"submitted_at": "2025-12-31T23:59:59Z"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.verdict(reviews=[self.review(**changes)]),
                                 ["pending"] * 2)

    def test_invalid_native_review_does_not_trigger_duplicate(self):
        self.assertEqual(self.verdict(reviews=[self.review(body="")]),
                         ["invalid-review"] * 2)

    def test_changed_head_and_closed_pull_are_superseded(self):
        self.assertEqual(self.verdict(head="new"), ["superseded"] * 2)
        self.assertEqual(self.verdict(state="closed"), ["superseded"] * 2)

    def test_removed_request_or_label_is_incomplete(self):
        self.assertEqual(self.verdict(requested=False), ["request-withdrawn"] * 2)
        self.assertEqual(self.verdict(label="canary"), ["request-withdrawn"] * 2)

    def test_clarification_does_not_require_reviewer_request(self):
        self.assertEqual(self.verdict(requested=False, delivery_key="clarification:1"),
                         ["pending"] * 2)

    def test_failed_new_conversations_still_consume_scan_budget(self):
        for module in self.workers():
            intake = module.ReviewIntake()
            calls = []

            def fail():
                calls.append(True)
                raise module.IncompleteReviewDelivery({
                    "reason": "attempts-exhausted", "disposition": "created"
                })

            for number in range(3):
                intake.register({"start": fail, "priority": 0, "created_at": "",
                                 "repository": "test/repo", "number": number,
                                 "config": {"max_new_per_run": 1}})
            with self.assertRaises(RuntimeError):
                intake.drain()
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
