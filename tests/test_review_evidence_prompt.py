import importlib
import sys
import unittest
from pathlib import Path

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "plugins/github-pr-reviewer-events/scripts"
)
PR = {
    "number": 7,
    "title": "Fix link rendering",
    "body": "Focused regression test included.",
    "head": {"sha": "a" * 40, "ref": "fix-link"},
    "base": {"ref": "main"},
    "user": {"login": "contributor"},
}


class ReviewEvidencePromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS))
        try:
            cls.workflow = importlib.import_module("main")
            cls.worker = importlib.import_module("worker")
        finally:
            sys.path.pop(0)

    def prompt(self):
        return self.workflow._build_review_prompt(
            "owner/repo", PR, PR["head"]["sha"], {}
        )

    def test_verification_contract_precedes_publication(self):
        prompt = self.prompt()
        self.assertLess(
            prompt.index("Verification evidence:"), prompt.index("7. Publish one review")
        )
        self.assertIn("Before submitting, reconcile", prompt)

    def test_pipeline_status_is_not_check_success(self):
        prompt = self.prompt()
        self.assertIn("underlying check's exit status", prompt)
        self.assertIn("pipeline or trailing command", prompt)
        self.assertIn("crash, timeout, kill", prompt)
        self.assertIn("failed, incomplete, blocked, skipped or not run", prompt)

    def test_partial_coverage_cannot_be_promoted(self):
        prompt = self.prompt()
        self.assertIn("command or result URL", prompt)
        self.assertIn("exact head", prompt)
        self.assertIn("files/tests actually covered", prompt)
        self.assertIn("subset pass is not a full changed-file or full-suite pass", prompt)
        self.assertIn("name the successful file targets or give their count", prompt)
        self.assertIn("do not imply a directory was checked", prompt)

    def test_ci_job_success_does_not_prove_unread_coverage(self):
        prompt = self.prompt()
        self.assertIn("CI job's success alone does not establish", prompt)
        self.assertIn("read its steps/configuration or logs", prompt)
        self.assertIn("do not substitute inferred CI coverage", prompt)

    def test_external_claims_require_read_evidence(self):
        prompt = self.prompt()
        self.assertIn("author-reported", prompt)
        self.assertIn("Never invent evaluation scores", prompt)
        self.assertIn("cost comparisons", prompt)
        self.assertIn("exact version", prompt)
        self.assertIn("source you actually read", prompt)

    def test_required_verification_gap_is_not_code_defect_or_approval(self):
        prompt = self.prompt()
        self.assertIn("required verification remains incomplete", prompt)
        self.assertIn("not asserting a code defect", prompt)
        self.assertIn("end with `🔄 CHANGES REQUESTED`", prompt)
        self.assertIn("and required verification is complete", prompt)

    def test_future_approval_conditions_preserve_all_verification_gaps(self):
        prompt = self.prompt()
        self.assertIn("current or future approval", prompt)
        self.assertIn("every unresolved required verification gap", prompt)
        self.assertIn("a separate description gate does not resolve", prompt)
        self.assertIn("Verification still needed:", prompt)
        self.assertIn("another gate is the only obstacle", prompt)

    def test_observed_actions_and_baseline_reproduction_need_evidence(self):
        prompt = self.prompt()
        self.assertIn("Do not claim an inspection, configuration read or base run", prompt)
        self.assertIn("unchanged files or a diff alone", prompt)
        self.assertIn("Preserve supported base reproductions", prompt)
        self.assertIn("A job name is not evidence of its steps", prompt)
        self.assertIn("obligations, not evidence that those actions occurred", prompt)
        self.assertIn("qualitative evaluation conclusions", prompt)

    def test_verification_gap_completes_without_approval_handoff(self):
        class LocalReviewer(self.worker.PullRequestReviewer):
            # Replace only GitHub I/O to prevent writes to real pull requests.
            def __init__(self):
                self.github_login = "all-hands-bot"

            def gh_pages(self, path):
                if path != "/pulls/7/reviews":
                    raise AssertionError(path)
                return [{
                    "commit_id": PR["head"]["sha"],
                    "user": {"login": self.github_login},
                    "submitted_at": "2026-01-02T00:00:00Z",
                    "state": "COMMENTED",
                    "body": "Required type check aborted; verification needed.\n"
                    "🔄 CHANGES REQUESTED",
                }]

            def gh(self, method, path, body=None):
                if (method, path) != ("GET", "/pulls/7"):
                    raise AssertionError((method, path))
                return PR

        self.assertTrue(LocalReviewer()._finish_completed_review(
            PR, {"created_at": "2026-01-01T00:00:00Z"}
        ))

    def test_existing_scope_and_publication_contracts_remain(self):
        prompt = self.prompt()
        self.assertIn("Do not run tests or report implementation findings", prompt)
        self.assertIn("🛑 MAINTAINER DECISION REQUIRED", prompt)
        self.assertIn("Never use `REQUEST_CHANGES`", prompt)
        self.assertIn("The native GitHub review is the only result channel", prompt)
        self.assertIn("GITHUB_REVIEW_POSTED", prompt)
        self.assertIn("Only create inline comments for actionable findings", prompt)

    def test_worker_delivers_contract_for_every_trigger(self):
        reviewer = self.worker.PullRequestReviewer.__new__(
            self.worker.PullRequestReviewer
        )
        reviewer.repository = "owner/repo"
        reviewer.token_name = "GITHUB_TOKEN_REVIEW"
        reviewer.config = {"trigger_reviewer": "all-hands-bot"}
        for kwargs in ({}, {"label": "review"}, {"delivery_key": "scan"}):
            with self.subTest(trigger=kwargs):
                prompt = reviewer._prompt(PR, {"id": 1}, **kwargs)
                self.assertEqual(prompt.count("Verification evidence:"), 1)
                self.assertIn("Before submitting, reconcile", prompt)
                self.assertIn("Acceptance reporting:", prompt)
                self.assertIn("stop immediately", prompt)


if __name__ == "__main__":
    unittest.main()
