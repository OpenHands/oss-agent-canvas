import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT_MODULES = [
    ROOT / "plugins" / name / "scripts" / "main.py"
    for name in (
        "github-pr-reviewer",
        "github-pr-reviewer-events",
        "github-pr-reviewer-canary",
    )
] + [
    ROOT / "automations" / name / "tarball" / "main.py"
    for name in (
        "github-pr-reviewer-multi-repo-extensions-openhands-software-agent-sdk",
        "github-pr-reviewer-continuous-canary",
    )
]


def build_prompt(path):
    # Each bundle imports siblings by name, so load it in its own interpreter.
    code = """
import json
import main
print(json.dumps(main._build_review_prompt(
    "example/project",
    {"number": 42, "title": "Example", "body": "Author reports tests passed."},
    "reviewed-head",
    {},
)))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class VerificationPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prompts = {path: build_prompt(path) for path in PROMPT_MODULES}

    def check_all(self, *requirements):
        for path, prompt in self.prompts.items():
            with self.subTest(bundle=str(path.relative_to(ROOT))):
                for requirement in requirements:
                    self.assertIn(requirement, prompt)

    def test_completed_command_status_not_display_pipeline(self):
        self.check_all(
            "Verification claims (before publication):",
            "check process itself finished with exit code 0",
            "A shell/pipeline exit code from `tail`, `tee`, `echo`, or `grep`",
            "Crash, OOM, signal, timeout, background/still-running",
            "capture the check's exit status before displaying the log",
        )

    def test_success_scope_and_failures_must_both_survive_reporting(self):
        self.check_all(
            "exact command, reviewed revision, tested files/suites",
            "A successful subset is not all changed files or the full suite",
            "failed, incomplete, skipped, or not-run checks",
            "Keep local checks separate from GitHub Actions",
            "Preserve genuine successes",
        )

    def test_external_evidence_needs_source_and_revision(self):
        self.check_all(
            "source actually read and the revisions it evaluated",
            "An author statement, label, or earlier-head result",
            "Do not invent scores, coverage, cost, or latency",
            "Remove or qualify every unsupported claim",
        )

    def test_review_safeguards_remain(self):
        self.check_all(
            "SCOPE GATE",
            "Do not run tests or report implementation findings",
            "Never use `REQUEST_CHANGES`",
            "The native GitHub review is the only result channel",
            "GITHUB_REVIEW_POSTED",
            "Do not modify files, push commits, or merge",
            "do not weaken repository acceptance criteria",
        )

    def test_policy_matches_across_independently_packaged_reviewers(self):
        policies = [
            prompt.split("Verification claims (before publication):\n", 1)[1].split(
                "\nReview instructions:", 1
            )[0]
            for prompt in self.prompts.values()
        ]
        self.assertTrue(policies[0].strip())
        self.assertEqual(len(set(policies)), 1)


if __name__ == "__main__":
    unittest.main()
