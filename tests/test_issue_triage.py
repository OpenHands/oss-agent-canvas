import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
WORKERS = [
    ROOT / "plugins/issue-triage/scripts/worker.py",
    ROOT / "automations/issue-triage-multi-repo-openhands-software-agent-sdk/tarball/worker.py",
]
START = "<!-- openhands-ai-triage:start -->"
END = "<!-- openhands-ai-triage:end -->"
HUMAN = "Issue description.  \n\n### Acceptance Criteria\n- [ ] Keep author requirement.\n"


def checker(path):
    tree = ast.parse(path.read_text())
    return ast.literal_eval(next(
        node.value for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "TRIAGE_BODY_CHECK" for t in node.targets)
    ))


def managed(text="Additional requirement."):
    return f"{START}\n### Acceptance Criteria\n- [ ] {text}\n{END}"


class BodyChangeTests(unittest.TestCase):
    def test_rendered_prompt_contains_executable_checker(self):
        prompts = []
        for worker in WORKERS:
            tree = ast.parse(worker.read_text())
            nodes = [
                node for node in tree.body
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id.startswith("TRIAGE_")
                    for target in node.targets
                ) or isinstance(node, ast.FunctionDef) and node.name == "author_body"
            ]
            nodes.append(next(
                node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "_prompt"
            ))
            namespace = {"json": json}
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(worker), "exec"), namespace)
            prompt = namespace["_prompt"](
                SimpleNamespace(token_name="TEST_TOKEN", repository="example/repository"),
                {"number": 1, "body": HUMAN}, [], [], [], "test-marker", None,
            )
            rendered = prompt.split("```python\n", 1)[1].split("```", 1)[0]
            self.assertEqual(rendered, checker(worker))
            compile(rendered, "triage_body_check.py", "exec")
            self.assertIn("python3 triage_body_check.py 'READY'", prompt)
            self.assertIn("not prove artifact freshness", prompt)
            prompts.append(prompt)
        self.assertEqual(*prompts)

    def check(self, before, intended, after, outcome="READY", expected=None):
        for worker in WORKERS:
            with self.subTest(worker=str(worker.relative_to(ROOT))):
                with tempfile.TemporaryDirectory() as directory:
                    files = []
                    for name, body in [("before", before), ("intended", intended), ("after", after)]:
                        path = Path(directory) / f"{name}.json"
                        path.write_text(json.dumps({"body": body}))
                        files.append(str(path))
                    result = subprocess.run(
                        [sys.executable, "-c", checker(worker), outcome, *files],
                        capture_output=True, text=True,
                    )
                if expected is None:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('"author_text_preserved": true', result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual(report["body_change"], expected)
                    self.assertTrue(report["author_text_preserved"])

    def test_append_preserves_author_criteria(self):
        after = HUMAN + "\n" + managed()
        self.check(HUMAN, after, after, expected="appended managed triage section")

    def test_replace_only_existing_managed_section(self):
        before = HUMAN + "\n" + managed("Old managed requirement.")
        after = HUMAN + "\n" + managed()
        self.check(before, after, after, expected="replaced managed triage section")

    def test_unchanged_readback(self):
        body = HUMAN + "\n" + managed()
        self.check(body, body, body, expected="unchanged")

    def test_decision_needed_removes_only_managed_section(self):
        before = HUMAN + "\n" + managed()
        self.check(before, HUMAN, HUMAN, "DECISION NEEDED", "removed managed triage section")

    def test_decision_needed_without_managed_section(self):
        self.check(HUMAN, HUMAN, HUMAN, "DECISION NEEDED", "unchanged")

    def test_author_criteria_cannot_be_replaced(self):
        after = "Issue description.\n\n" + managed()
        self.check(HUMAN, after, after)

    def test_author_trailing_spaces_cannot_be_removed(self):
        after = HUMAN.replace("description.  ", "description.") + "\n" + managed()
        self.check(HUMAN, after, after)

    def test_readback_must_match_intended_body(self):
        intended = HUMAN + "\n" + managed()
        self.check(HUMAN, intended, HUMAN)

    def test_ready_requires_exactly_one_complete_managed_section(self):
        for after in [HUMAN, HUMAN + managed() + managed(), HUMAN + START, HUMAN + END]:
            with self.subTest(after=after):
                self.check(HUMAN, after, after)

    def test_malformed_or_duplicate_before_is_not_guessed(self):
        after = HUMAN + "\n" + managed()
        for before in [HUMAN + START, HUMAN + END + START, HUMAN + managed() + managed()]:
            with self.subTest(before=before):
                self.check(before, after, after)

    def test_decision_needed_cannot_leave_managed_criteria(self):
        after = HUMAN + "\n" + managed()
        self.check(HUMAN, after, after, "DECISION NEEDED")

    def test_ready_requires_managed_section_at_end(self):
        after = managed() + "\n" + HUMAN
        self.check(HUMAN, after, after)

    def test_human_suffix_survives_managed_section_move(self):
        before = "Prefix\n" + managed("Old") + "\nHuman suffix."
        after = "Prefix\n\nHuman suffix.\n\n" + managed()
        self.check(before, after, after, expected="replaced managed triage section")

    def test_null_body_can_receive_managed_section(self):
        self.check(None, managed(), managed(), expected="appended managed triage section")

    def test_unknown_outcome_is_failure(self):
        self.check(HUMAN, HUMAN, HUMAN, "SUCCESS")

    def test_missing_readback_is_failure(self):
        for worker in WORKERS:
            with tempfile.TemporaryDirectory() as directory:
                before = Path(directory) / "before.json"
                before.write_text(json.dumps({"body": HUMAN}))
                missing = Path(directory) / "missing-after.json"
                result = subprocess.run(
                    [sys.executable, "-c", checker(worker), "READY",
                     str(before), str(before), str(missing)],
                    capture_output=True, text=True,
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FileNotFoundError", result.stderr)
            self.assertNotIn('"author_text_preserved": true', result.stdout)

    def test_workers_match_and_embed_checker_in_prompt(self):
        self.assertEqual(WORKERS[0].read_text(), WORKERS[1].read_text())
        tree = ast.parse(WORKERS[0].read_text())
        prompt = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_prompt")
        self.assertTrue(any(
            isinstance(n, ast.FormattedValue)
            and isinstance(n.value, ast.Name) and n.value.id == "TRIAGE_BODY_CHECK"
            for n in ast.walk(prompt)
        ))


if __name__ == "__main__":
    unittest.main()
