import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "plugins/github-pr-reviewer-events/scripts"
)
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("review_workflow", SCRIPTS / "main.py")
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)
sys.path.pop(0)


def prompt(head):
    return workflow._build_review_prompt("example/repo", {"number": 1}, head, {})


class ReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "review with spaces"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "implementation.py").write_text("VALUE = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.repo / "implementation.py").write_text("VALUE = 2\n")
        (self.repo / "regression.py").write_text(
            "from implementation import VALUE\nassert VALUE == 2\n"
        )
        self.git("add", ".")
        self.git("commit", "-qm", "head with regression")
        self.head = self.git("rev-parse", "HEAD").strip()

    def git(self, *args, cwd=None):
        return subprocess.check_output(
            ["git", "-C", str(cwd or self.repo), *args], text=True,
            stderr=subprocess.STDOUT,
        )

    def state(self):
        return (
            (self.repo / "implementation.py").read_bytes(),
            self.git("rev-parse", "HEAD"),
            (self.repo / ".git/index").read_bytes(),
            self.git("diff", "HEAD", "--"),
        )

    def setup_recipe(self, head):
        text = prompt(head)
        match = re.search(r"```sh\n(.*?)\n```", text, re.DOTALL)
        self.assertIsNotNone(match, "review must supply an isolated comparison recipe")
        return match.group(1)

    def run_setup(self, head):
        env = dict(os.environ, TMPDIR=str(self.root))
        return subprocess.run(
            ["sh", "-c", self.setup_recipe(head) + '\nresult=$?; '
             'printf "\\nCOMPARISON=%s\\n" "$comparison"; exit "$result"'],
            cwd=self.repo, env=env, text=True, capture_output=True,
        )

    def test_prompt_limits_edits_to_disposable_comparison(self):
        text = prompt(self.head)
        for requirement in (
            "Do not modify the reviewed checkout",
            "even temporarily",
            "git --work-tree",
            "HEAD's regression tests",
            "report the baseline comparison as unverified",
            "not head-test results",
        ):
            self.assertIn(requirement, text)

    def test_comparison_keeps_head_tests_and_review_source_index_and_head(self):
        before = self.state()
        result = self.run_setup(self.head)
        self.assertEqual(result.returncode, 0, result.stderr)
        comparison = Path(result.stdout.split("COMPARISON=")[-1].strip())
        self.assertNotEqual(
            self.git("rev-parse", "--git-path", "index"),
            self.git("rev-parse", "--git-path", "index", cwd=comparison),
        )
        self.assertEqual(self.state(), before)
        self.git(
            "restore", f"--source={self.base}", "--staged", "--worktree", "--",
            "implementation.py", cwd=comparison,
        )
        self.assertEqual(self.state(), before)
        self.assertEqual((comparison / "implementation.py").read_text(), "VALUE = 1\n")
        self.assertTrue((comparison / "regression.py").is_file())
        baseline = subprocess.run(
            [sys.executable, "regression.py"], cwd=comparison, capture_output=True,
        )
        self.assertNotEqual(baseline.returncode, 0)
        self.assertIn(b"AssertionError", baseline.stderr)
        head = subprocess.run(
            [sys.executable, "regression.py"], cwd=self.repo, capture_output=True,
        )
        self.assertEqual(head.returncode, 0, head.stderr)
        self.assertEqual(self.state(), before)
        self.git("worktree", "remove", "--force", str(comparison))
        comparison.parent.rmdir()
        self.assertEqual(self.state(), before)

    def test_unavailable_head_does_not_change_review(self):
        before = self.state()
        result = self.run_setup("0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state(), before)
        self.assertEqual(len(self.git("worktree", "list", "--porcelain").split("worktree ")), 2)

    def test_scope_gate_precedes_comparison_instructions(self):
        text = prompt(self.head)
        self.assertLess(text.index("4. SCOPE GATE"), text.index("Validation isolation:"))
        self.assertIn("Do not run tests or report implementation findings", text)


if __name__ == "__main__":
    unittest.main()
