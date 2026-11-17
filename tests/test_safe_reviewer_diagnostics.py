import importlib.util
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted(ROOT.glob("plugins/github-pr-reviewer*/scripts/main.py")) + sorted(
    ROOT.glob("automations/github-pr-reviewer*/tarball/main.py")
)


def review_prompt(path, token_name):
    spec = importlib.util.spec_from_file_location("reviewer", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module._build_review_prompt(
        "owner/repo", {"number": 1}, "head", {},
        github_token_secret=token_name,
    )


class ReviewerDiagnosticsTests(unittest.TestCase):
    def test_presence_check_outputs_only_status_even_with_shell_tracing(self):
        for path in MODULES:
            for token_name in ["GITHUB_TOKEN_REVIEW", "CUSTOM_REVIEW_CREDENTIAL"]:
                prompt = review_prompt(path, token_name)
                match = re.search(r"`(if test -n .*?; fi)`", prompt)
                self.assertIsNotNone(match)
                command = match.group(1)
                for value in [None, "", "private-fixture-with-spaces-and-$(false)"]:
                    with self.subTest(path=path, token_name=token_name, configured=bool(value)):
                        env = dict(os.environ)
                        env.pop(token_name, None)
                        if value is not None:
                            env[token_name] = value
                        result = subprocess.run(
                            ["bash", "-xc", command], env=env,
                            capture_output=True, text=True, check=True,
                        )
                        expected = "configured" if value else "missing"
                        self.assertEqual(result.stdout, f"credential {expected}\n")
                        self.assertNotIn("private-fixture", result.stdout + result.stderr)
                        self.assertNotIn("$(false)", result.stdout + result.stderr)

    def test_prompt_preserves_review_gates_and_explains_presence_limit(self):
        for path in MODULES:
            prompt = review_prompt(path, "GITHUB_TOKEN_REVIEW")
            with self.subTest(path=path):
                self.assertIn("Presence is not proof of authentication or permission", prompt)
                self.assertIn("Never print a credential prefix", prompt)
                self.assertIn("terminal observations", prompt)
                self.assertIn("SCOPE GATE", prompt)
                self.assertIn("Do not modify files, push commits, or merge", prompt)
                self.assertIn("re-fetch the current mutable GitHub state", prompt)


if __name__ == "__main__":
    unittest.main()
