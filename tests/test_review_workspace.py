import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEWERS = sorted(ROOT.glob("plugins/github-pr-reviewer*/scripts/main.py")) + sorted(
    ROOT.glob("automations/github-pr-reviewer*/tarball/main.py")
)


def load_workflow(path):
    spec = importlib.util.spec_from_file_location("review_workflow", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.pop("github_client", None)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
        sys.modules.pop("github_client", None)
        if previous is not None:
            sys.modules["github_client"] = previous
    return module


class ReviewWorkspaceTests(unittest.TestCase):
    def prompts(self):
        pr = {
            "number": 1,
            "title": "Regression fix",
            "body": "Compare the regression tests with the base implementation.",
            "head": {"sha": "a" * 40, "ref": "fix"},
            "base": {"sha": "b" * 40, "ref": "main"},
        }
        self.assertEqual(len(REVIEWERS), 5)
        for path in REVIEWERS:
            workflow = load_workflow(path)
            for workspace in (None, "Fetch the PR and check out the exact head."):
                yield path, workspace, workflow._build_review_prompt(
                    "example/project",
                    pr,
                    pr["head"]["sha"],
                    {},
                    workspace_instructions=workspace,
                )

    def test_head_is_not_a_base_comparison_scratchpad(self):
        for path, workspace, prompt in self.prompts():
            with self.subTest(package=str(path.relative_to(ROOT)), setup=workspace):
                self.assertIn("WORKSPACE INTEGRITY", prompt)
                self.assertIn("Never replace head files with base versions", prompt)
                self.assertIn("separate temporary directory outside the reviewed tree", prompt)
                self.assertIn("Do not copy or symlink writable test sources back", prompt)
                self.assertIn("If isolation is unavailable", prompt)

    def test_head_verification_survives_both_setup_paths(self):
        for path, workspace, prompt in self.prompts():
            with self.subTest(package=str(path.relative_to(ROOT)), setup=workspace):
                self.assertIn("before and after each head test command", prompt)
                self.assertIn("git rev-parse HEAD", prompt)
                self.assertIn("git diff --exit-code HEAD --", prompt)
                self.assertIn("git diff --cached --exit-code HEAD --", prompt)
                self.assertIn("path/type/content/mode manifest", prompt)
                self.assertIn("changes, additions and deletions", prompt)
                self.assertIn("git ls-files --others", prompt)
                self.assertIn("Do not repair the tree with checkout, reset or stash", prompt)
                self.assertIn("revision and directory", prompt)
                self.assertIn("After GitHub accepts", prompt)
                self.assertIn("SCOPE GATE", prompt)
                self.assertIn("Never use `REQUEST_CHANGES`", prompt)
                self.assertIn("Do not modify files, push commits, or merge", prompt)


if __name__ == "__main__":
    unittest.main()
