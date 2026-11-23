import sys
import unittest
from pathlib import Path

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "plugins/github-pr-reviewer-events/scripts"
)
sys.path.insert(0, str(SCRIPTS))
import main
import worker
sys.path.pop(0)


class ReviewCompletionTests(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "number": 17,
            "head": {"sha": "a" * 40},
            "user": {"login": "contributor"},
        }
        self.reviewer = worker.PullRequestReviewer.__new__(worker.PullRequestReviewer)
        self.reviewer.repository = "example/project"
        self.reviewer.token_name = "GITHUB_TOKEN_REVIEW"
        self.reviewer.config = {"trigger_reviewer": "review-bot"}

    def assert_terminal_contract(self, prompt):
        self.assertEqual(prompt.count("Publication and completion contract:"), 1)
        self.assertIn("Before submitting the review, finish", prompt)
        self.assertIn("task-tracker updates", prompt)
        self.assertIn("Do not chain cleanup", prompt)
        self.assertIn("Do not record publication as successful in advance", prompt)
        self.assertIn("new native review submitted during this task", prompt)
        self.assertIn("No terminal, task tracker, browser, or file tools", prompt)
        self.assertIn("no summary, URL, verdict, Markdown, or explanation", prompt)
        self.assertTrue(prompt.endswith("Never emit the success token on failure."))

    def test_event_prompt_ends_with_terminal_contract(self):
        prompt = self.reviewer._prompt(self.pr, {"id": 123})
        self.assert_terminal_contract(prompt)
        self.assertLess(
            prompt.index("Acceptance reporting:"),
            prompt.index("Publication and completion contract:"),
        )
        self.assertIn("do NOT publish a second review", prompt)
        self.assertIn("immediately before submitting a review or reporting a skip", prompt)

    def test_self_review_note_precedes_terminal_contract(self):
        self.pr["user"]["login"] = "review-bot"
        prompt = self.reviewer._prompt(self.pr, {"id": 123})
        self.assert_terminal_contract(prompt)
        self.assertLess(
            prompt.index("authored by the configured reviewer"),
            prompt.index("Publication and completion contract:"),
        )

    def test_alternate_intake_preserves_moved_head_guards(self):
        for kwargs, expected in [
            ({"label": "review-me"}, "leave `review-me` in place"),
            ({"delivery_key": "scan"}, "a later scheduled scan reviews the new head"),
        ]:
            with self.subTest(kwargs=kwargs):
                prompt = self.reviewer._prompt(self.pr, {}, **kwargs)
                self.assert_terminal_contract(prompt)
                self.assertIn(expected, prompt)

    def test_direct_prompt_has_same_contract_and_failure_fallback(self):
        prompt = main._build_review_prompt("example/project", self.pr, "a" * 40, {})
        self.assert_terminal_contract(prompt)
        self.assertIn("output the complete review text instead", prompt)
        self.assertIn("If the API rejects the inline positions, retry", prompt)
        self.assertIn("MAINTAINER DECISION REQUIRED", prompt)
        self.assertIn("Never use `REQUEST_CHANGES`", prompt)

    def test_completion_outcomes_are_mutually_exclusive(self):
        prompt = self.reviewer._prompt(self.pr, {"id": 123})
        contract = prompt.split("Publication and completion contract:\n", 1)[1]
        for branch in ["DUPLICATE SKIP", "MOVED-HEAD SKIP", "NEW PUBLICATION", "PUBLICATION FAILURE"]:
            self.assertIn(branch, contract)
        self.assertIn("An earlier accepted review is not a new publication", contract)
        self.assertIn("Do not emit the success token for either skip", contract)
        self.assertIn("Do not make another submission attempt", contract)
        self.assertIn("automation alone performs any configured human-review handoff", prompt)

    def test_accepted_output_uses_only_one_delivery_channel(self):
        prompt = self.reviewer._prompt(self.pr, {"id": 123})
        self.assertIn("call it exactly once", prompt)
        self.assertIn("with message exactly GITHUB_REVIEW_POSTED", prompt)
        self.assertIn("no accompanying assistant text or other tool calls", prompt)
        self.assertIn("Only if finish is unavailable", prompt)
        self.assertIn("Do not repeat the token", prompt)


if __name__ == "__main__":
    unittest.main()
