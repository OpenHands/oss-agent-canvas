import importlib.util
import sys
import time
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

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
        self.observations = 0
        self.state = SimpleNamespace(refresh_from_server=self.refresh,
                                     events=self.events)

    def refresh(self):
        if self.status == "running":
            self.observations += 1
            if self.observations > 1:
                outcome = next(self.outcomes)
                if outcome == "published":
                    self.published = True
                    self.status = "finished"
                else:
                    self.status = "error"
                    self.events.append(ConversationErrorEvent(
                        source="environment", code=outcome, detail=""))
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
                deadline=900, fresh_delivery=False, reserve_attempt=None):
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
                clock=clock, sleep=clock.sleep, fresh_delivery=fresh_delivery,
                reserve_attempt=reserve_attempt,
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
            self.assertEqual(conversation.runs, 0)

    def test_observation_classifies_the_event_before_restart(self):
        for result, conversation, _, record, _ in self.execute(
            ["LLMAuthenticationError"], status="running", record={"attempts": 1}
        ):
            self.assertEqual(result, "incomplete")
            self.assertEqual(conversation.runs, 0)
            self.assertEqual(record["reason"], "unconfirmed-error")

    def test_new_delivery_can_run_after_an_old_error(self):
        for result, conversation, _, record, _ in self.execute(
            ["published"], status="error", fresh_delivery=True
        ):
            self.assertEqual(result, "published")
            self.assertEqual(conversation.runs, 1)
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

    def test_shared_reservation_can_reject_a_stale_local_budget(self):
        for result, conversation, _, record, _ in self.execute(
            [], record={"attempts": 0}, reserve_attempt=lambda: 4
        ):
            self.assertEqual(result, "incomplete")
            self.assertEqual(conversation.runs, 0)
            self.assertEqual(record["reason"], "attempts-exhausted")

    def test_deadline_is_rechecked_after_reserving_attempt(self):
        for scripts in SCRIPTS:
            module = load(scripts / "review_delivery.py")
            conversation = ConversationBoundary([])
            clock = Clock()
            record = {}

            def slow_save():
                clock.now = 2

            result = module.complete_review(conversation, lambda: "pending", record,
                                            slow_save, 1, clock=clock, sleep=clock.sleep)
            self.assertEqual(result, "incomplete")
            self.assertEqual(conversation.runs, 0)

    def test_interrupted_fresh_delivery_keeps_first_run_eligibility(self):
        for result, conversation, _, record, _ in self.execute(
            [], status="error", fresh_delivery=True,
            record={"fresh_delivery": True},
            check=lambda: "verification-unavailable",
        ):
            self.assertEqual(result, "incomplete")
            self.assertEqual(conversation.runs, 0)
            self.assertTrue(record["fresh_delivery"])

    def test_cached_error_cannot_authorize_an_unowned_restart(self):
        for result, conversation, _, record, _ in self.execute([], status="error"):
            self.assertEqual(result, "incomplete")
            self.assertEqual(conversation.runs, 0)
            self.assertEqual(record["reason"], "unconfirmed-error")

    def test_plugin_copies_match(self):
        for filename in ("review_delivery.py", "agent_conversation.py", "worker.py"):
            with self.subTest(filename=filename):
                self.assertEqual((SCRIPTS[0] / filename).read_bytes(),
                                 (SCRIPTS[1] / filename).read_bytes())


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


class DispatchConversationBoundary(ConversationBoundary):
    def __init__(self, outcomes, status="finished"):
        super().__init__(outcomes, status)
        self.prompts = []
        self.trace = []
        self.closed = 0

    def refresh(self):
        return {"execution_status": self.status}

    def send_message(self, prompt):
        self.trace.append("prompt")
        self.prompts.append(prompt)
        self.status = "idle"

    def update_secrets(self, secrets):
        assert secrets == {}

    def close(self):
        self.closed += 1


class KVBoundary:
    def __init__(self, values):
        self.values = deepcopy(values)
        self.calls = []

    def __call__(self, key, method, value=None):
        assert key.startswith(("agent-conversation-", "review-attempts-",
                               "review-delivery-claim-"))
        self.calls.append((key, method, deepcopy(value)))
        if method == "GET":
            assert value is None
            return deepcopy(self.values.get(key))
        if method == "PUT":
            assert key.startswith("agent-conversation-")
            assert isinstance(value, dict)
            self.values[key] = deepcopy(value)
            return {"value": deepcopy(value)}
        assert method == "POST" and key.endswith(("/incr", "/decr"))
        assert set(value) == {"by"} and isinstance(value["by"], int)
        base, operation = key.rsplit("/", 1)
        self.values[base] = self.values.get(base, 0) + (
            value["by"] if operation == "incr" else -value["by"]
        )
        return {"value": self.values[base]}


class DispatcherTests(unittest.TestCase):
    workers = PublicationTests.workers

    # Remote conversation, GitHub and KV doubles avoid production network/writes;
    # dispatcher, complete_review and SDK error classification are not replaced.
    @contextmanager
    def boundary(self, worker, *, status="finished", attempts=1, outcomes=()):
        module = sys.modules["agent_conversation"]
        dispatcher = module.AgentConversationDispatcher.__new__(
            module.AgentConversationDispatcher
        )
        dispatcher.automation_id = "isolated-test"
        dispatcher.profile_id = UUID(int=1)
        dispatcher._workspace = object()
        dispatcher._secrets = {}
        dispatcher._delivery_deadline = time.monotonic() + 900
        conversation_id = uuid5(NAMESPACE_URL, "isolated-test:repo:pr:1")
        state_key = "agent-conversation-" + str(conversation_id)
        counter_key = "review-attempts-" + str(uuid5(conversation_id, "head"))
        previous = {"subject": "repo:pr:1", "conversation_id": str(conversation_id),
                    "delivery": "old", "head": "head", "attempts": attempts,
                    "outcome": "published", "reason": "review-verified"}
        kv = KVBoundary({state_key: previous, counter_key: attempts})
        conversation = DispatchConversationBoundary(outcomes, status)

        class RemoteBoundary:
            @staticmethod
            def attach(workspace, identity, *, visualizer):
                assert workspace is dispatcher._workspace
                assert identity == conversation_id and visualizer is None
                return conversation

            @staticmethod
            def create(*args, **kwargs):
                raise AssertionError("Existing conversation must be reused")

        with patch.object(module, "_kv_request", kv), patch.object(
            module, "RemoteConversation", RemoteBoundary
        ):
            yield dispatcher, conversation, kv, state_key, counter_key

    @staticmethod
    def dispatch(dispatcher, conversation, *, delivery="new", head="head",
                 allow=None, verdict=None):
        def check():
            conversation.trace.append("check")
            return verdict or ("published" if conversation.published else "pending")

        kwargs = {} if allow is None else {"allow_same_head_refresh": allow}
        return dispatcher.deliver(subject="repo:pr:1", delivery=delivery,
                                  prompt="Clarified review request", head=head,
                                  check_delivery=check, **kwargs)

    def test_finished_clarified_request_sends_one_fresh_prompt(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(
                worker, outcomes=["published"]
            ) as (dispatcher, conversation, kv, key, counter):
                result = self.dispatch(dispatcher, conversation, allow=True)
                self.assertEqual(result["outcome"], "published")
                self.assertEqual(len(conversation.prompts), 1)
                self.assertEqual(conversation.runs, 1)
                self.assertLess(conversation.trace.index("check"),
                                conversation.trace.index("prompt"))
                self.assertEqual(kv.values[key]["delivery"], "new")
                self.assertEqual(kv.values[counter], 2)
                self.assertEqual(conversation.closed, 1)

    def test_changed_trigger_without_flag_preserves_previous_identity(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(worker) as state:
                dispatcher, conversation, kv, key, counter = state
                self.dispatch(dispatcher, conversation)
                self.assertEqual(conversation.prompts, [])
                self.assertEqual(conversation.runs, 0)
                self.assertEqual(kv.values[key]["delivery"], "old")
                self.assertEqual(kv.values[counter], 1)

    def test_same_delivery_is_duplicate_even_with_refresh_permission(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(worker) as state:
                dispatcher, conversation, kv, key, counter = state
                self.dispatch(dispatcher, conversation, delivery="old")
                self.dispatch(dispatcher, conversation, delivery="old", allow=True)
                self.assertEqual(conversation.prompts, [])
                self.assertEqual(conversation.runs, 0)
                self.assertEqual(kv.values[counter], 1)

    def test_publication_is_checked_before_any_fresh_prompt(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(worker) as state:
                dispatcher, conversation, kv, key, counter = state
                result = self.dispatch(dispatcher, conversation, allow=True,
                                       verdict="published")
                self.assertEqual(result["outcome"], "published")
                self.assertEqual(conversation.prompts, [])
                self.assertEqual(conversation.runs, 0)
                self.assertEqual(kv.values[key]["delivery"], "old")
                self.assertEqual(kv.values[counter], 1)

    def test_active_owner_is_never_reprompted_or_reidentified(self):
        for worker in self.workers():
            for status in ("running", "paused"):
                for allow in (False, True):
                    with self.subTest(plugin=worker.__file__, status=status,
                                      allow=allow), self.boundary(
                        worker, status=status
                    ) as (dispatcher, conversation, kv, key, counter):
                        result = self.dispatch(dispatcher, conversation, allow=allow)
                        self.assertEqual(result["outcome"], "incomplete")
                        self.assertEqual(conversation.prompts, [])
                        self.assertEqual(conversation.runs, 0)
                        self.assertEqual(kv.values[key]["delivery"], "old")
                        self.assertEqual(kv.values[counter], 1)

    def test_changed_head_still_gets_a_new_turn_and_its_own_budget(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(
                worker, attempts=3, outcomes=["published"]
            ) as (dispatcher, conversation, kv, key, counter):
                result = self.dispatch(dispatcher, conversation, head="new-head")
                self.assertEqual(result["outcome"], "published")
                self.assertEqual(len(conversation.prompts), 1)
                self.assertEqual(conversation.runs, 1)
                self.assertEqual(kv.values[key]["head"], "new-head")
                self.assertEqual(kv.values[counter], 3)

    def test_clarifications_share_remaining_atomic_head_budget(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(
                worker, outcomes=["published", "published"]
            ) as (dispatcher, conversation, kv, key, counter):
                for delivery in ("new", "newer"):
                    conversation.published = False
                    result = self.dispatch(dispatcher, conversation,
                                           delivery=delivery, allow=True)
                    self.assertEqual(result["outcome"], "published")
                self.assertEqual(kv.values[counter], 3)
                conversation.published = False
                result = self.dispatch(dispatcher, conversation,
                                       delivery="exhausted", allow=True)
                self.assertEqual(result["outcome"], "incomplete")
                self.assertEqual(result["reason"], "attempts-exhausted")
                self.assertEqual(len(conversation.prompts), 2)
                self.assertEqual(conversation.runs, 2)
                self.assertEqual(kv.values[key]["delivery"], "newer")
                reservations = [call for call in kv.calls
                                if call[:2] == (counter + "/incr", "POST")]
                self.assertGreaterEqual(len(reservations), 2)

    def test_exhausted_head_does_not_send_prompt_or_overwrite_delivery(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(
                worker, attempts=3
            ) as (dispatcher, conversation, kv, key, counter):
                result = self.dispatch(dispatcher, conversation, allow=True)
                self.assertEqual(result["reason"], "attempts-exhausted")
                self.assertEqual(conversation.prompts, [])
                self.assertEqual(conversation.runs, 0)
                self.assertEqual(kv.values[key]["delivery"], "old")

    def test_clarified_delivery_classifies_real_sdk_errors_without_resetting_budget(self):
        for worker in self.workers():
            for code, reason in (("LLMServiceUnavailableError", "attempts-exhausted"),
                                 ("LLMAuthenticationError", "non-transient-error")):
                with self.subTest(plugin=worker.__file__, code=code), self.boundary(
                    worker, attempts=2, outcomes=[code]
                ) as (dispatcher, conversation, kv, key, counter):
                    result = self.dispatch(dispatcher, conversation, allow=True)
                    self.assertEqual(result["outcome"], "incomplete")
                    self.assertEqual(result["reason"], reason)
                    self.assertEqual(conversation.runs, 1)
                    self.assertEqual(len(conversation.prompts), 1)
                    self.assertEqual(kv.values[counter], 3)
                    self.assertEqual(kv.values[key]["delivery"], "new")

    def test_atomic_counter_exhaustion_beats_stale_local_attempts(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(
                worker, attempts=0
            ) as (dispatcher, conversation, kv, key, counter):
                kv.values[counter] = 3
                result = self.dispatch(dispatcher, conversation, allow=True)
                self.assertEqual(result["reason"], "attempts-exhausted")
                self.assertEqual(conversation.prompts, [])
                self.assertEqual(conversation.runs, 0)
                self.assertEqual(kv.values[key]["delivery"], "old")

    def test_contended_delivery_claim_does_not_admit_a_refresh(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(worker) as state:
                dispatcher, conversation, kv, key, counter = state
                claim = "review-delivery-claim-" + key.removeprefix("agent-conversation-")
                kv.values[claim] = 1
                result = self.dispatch(dispatcher, conversation, allow=True)
                self.assertEqual(result["reason"], "delivery-active")
                self.assertEqual(conversation.prompts, [])
                self.assertEqual(conversation.runs, 0)
                self.assertEqual(kv.values[key]["delivery"], "old")
                self.assertEqual(kv.values[counter], 1)
                self.assertEqual(kv.values[claim], 1)

    def test_clarification_after_rejected_trigger_can_admit_that_same_trigger(self):
        for worker in self.workers():
            with self.subTest(plugin=worker.__file__), self.boundary(
                worker, outcomes=["published"]
            ) as (dispatcher, conversation, kv, key, counter):
                self.dispatch(dispatcher, conversation, allow=False)
                self.assertEqual(kv.values[key]["delivery"], "old")
                result = self.dispatch(dispatcher, conversation, allow=True)
                self.assertEqual(result["outcome"], "published")
                self.assertEqual(len(conversation.prompts), 1)
                self.assertEqual(conversation.runs, 1)


class WorkerClarificationTests(unittest.TestCase):
    workers = PublicationTests.workers
    review = staticmethod(PublicationTests.review)

    def reviewer(self, module, reviews, comments):
        reviewer = module.PullRequestReviewer.__new__(module.PullRequestReviewer)
        reviewer.config = {}
        reviewer.github_login = "review-bot"
        reviewer.repository = "test/repo"
        reviewer.token_name = "ISOLATED_GITHUB_TOKEN"

        def pages(path):
            if path == "/pulls/1/reviews":
                return list(reviews)
            if path == "/issues/1/comments":
                return list(comments)
            raise AssertionError("Unexpected GitHub read: " + path)

        reviewer.gh_pages = pages
        return reviewer

    def test_latest_authenticated_current_head_review_is_used(self):
        for module in self.workers():
            reviews = [self.review(submitted_at="2026-01-01T00:03:00Z"),
                       self.review(submitted_at="2026-01-01T00:01:00Z"),
                       self.review(commit_id="other", submitted_at="2026-01-01T00:09:00Z"),
                       self.review(user={"login": "human"},
                                   submitted_at="2026-01-01T00:10:00Z")]
            reviewer = self.reviewer(module, reviews, [])
            self.assertEqual(reviewer._latest_reviewed_at(1, "head"),
                             "2026-01-01T00:03:00Z")
            self.assertFalse(reviewer._latest_reviewed_at(1, "missing"))

    def test_clarified_request_routing_and_start_review_flag(self):
        prior = self.review()
        latest = self.review(submitted_at="2026-01-01T00:03:00Z")
        trigger = {"id": 2, "created_at": "2026-01-01T00:04:00Z"}
        comment = {"user": {"login": "author"},
                   "created_at": "2026-01-01T00:02:00Z"}
        cases = [
            ("clarified", [prior], [comment], trigger, True),
            ("no-comment", [prior], [], trigger, False),
            ("no-prior-review", [], [comment], trigger, False),
            ("clarification-arrives-after-request", [prior],
             [dict(comment, created_at="2026-01-01T00:05:00Z")], trigger, True),
            ("comment-at-review-time", [prior],
             [dict(comment, created_at=prior["submitted_at"])], trigger, False),
            ("request-at-review-time", [prior], [comment],
             {"id": 2, "created_at": prior["submitted_at"]}, False),
            ("older-than-latest-review", [prior, latest], [comment], trigger, False),
            ("reviewer-own-comment", [prior],
             [dict(comment, user={"login": "REVIEW-BOT"})], trigger, False),
            ("trigger-account-comment", [prior],
             [dict(comment, user={"login": "all-hands-bot"})], trigger, False),
            ("scan", [prior], [comment], None, False),
            ("missing-trigger-time", [prior], [comment], {"id": 2}, False),
            ("request-before-review", [prior], [comment],
             {"id": 2, "created_at": "2026-01-01T00:00:00Z"}, False),
            ("newer-accepted-review", [prior, self.review(
                submitted_at="2026-01-01T00:05:00Z")], [comment], trigger, False),
        ]
        for module in self.workers():
            for name, reviews, comments, request, expected in cases:
                with self.subTest(plugin=module.__file__, case=name):
                    reviewer = self.reviewer(module, reviews, comments)
                    self.assertEqual(reviewer._clarified_review_request(
                        1, "head", request), expected)
                    captured = []

                    class DispatcherBoundary:
                        def deliver(self, **kwargs):
                            captured.append(kwargs)
                            return {"disposition": "resumed", "conversation_id": "test",
                                    "outcome": "published", "reason": "review-verified"}

                    reviewer.dispatcher = DispatcherBoundary()
                    pr = {"number": 1, "head": {"sha": "head"},
                          "title": "Isolated test", "body": "", "user": {"login": "author"},
                          "html_url": "https://example.invalid/test/repo/pull/1"}
                    reviewer._start_review("repo", pr, request, "head", None,
                                           "scan:test/repo:1:head" if request is None else None)
                    self.assertEqual(captured[0]["allow_same_head_refresh"], expected)
                    self.assertEqual(captured[0]["head"], "head")


if __name__ == "__main__":
    unittest.main()
