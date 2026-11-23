import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKERS = [
    ROOT / "plugins/issue-triage/scripts/worker.py",
    ROOT
    / "automations/issue-triage-multi-repo-openhands-software-agent-sdk/tarball/worker.py",
]


def load_worker(path):
    for name in ("agent_conversation", "github_client", "worker"):
        spec = importlib.util.spec_from_file_location(name, path.with_name(name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if name != "worker":
            sys.modules[name] = module
    return module


class DeliveryBoundary:
    def __init__(self):
        self.deliveries = []

    def deliver(self, **delivery):
        self.deliveries.append(delivery)
        return {"disposition": "created", "conversation_id": "isolated-test"}


def triage_with_comments(module, comments):
    class Triage(module.IssueTriage):
        def __init__(self):
            self.repository = "example/repository"
            self.token_name = "GITHUB_TOKEN_ISSUE_TRIAGE"
            self.dispatcher = DeliveryBoundary()

        def gh_pages(self, path):
            return comments

    return Triage()


class IssueTriageTests(unittest.TestCase):
    def test_prompt_requires_evidence_boundary_before_priority(self):
        for path in WORKERS:
            with self.subTest(worker=str(path.relative_to(ROOT))):
                module = load_worker(path)
                triage = triage_with_comments(module, [])
                prompt = triage._prompt(
                    {"number": 1, "body": "A helper test fails."},
                    [], [], [], "<!-- triage-source:test -->", None,
                )
                for requirement in (
                    "Priority evidence",
                    "direct-helper",
                    "mocked",
                    "related issues",
                    "normal product entry point",
                    "Before changing labels",
                    "reported, not independently reproduced",
                ):
                    self.assertIn(requirement, prompt)
                self.assertLess(prompt.index("Priority evidence"), prompt.index("READY:"))

    def test_previous_policy_is_retriaged_but_current_delivery_is_idempotent(self):
        for path in WORKERS:
            with self.subTest(worker=str(path.relative_to(ROOT))):
                module = load_worker(path)
                issue = {"number": 1, "title": "Example", "body": "Original report"}
                old_digest = hashlib.sha256(
                    json.dumps([2, "Example", "Original report", []], sort_keys=True).encode()
                ).hexdigest()
                issue["body"] += (
                    f"\n{module.TRIAGE_BODY_START}\n"
                    f"<!-- triage-source:{old_digest} -->\n{module.TRIAGE_BODY_END}"
                )
                triage = triage_with_comments(module, [])
                triage._submit(42, issue, [issue])
                self.assertEqual(len(triage.dispatcher.deliveries), 1)
                delivery = triage.dispatcher.deliveries[0]
                self.assertNotEqual(delivery["delivery"], old_digest)
                issue["body"] = (
                    f"Original report\n{module.TRIAGE_BODY_START}\n"
                    f"<!-- triage-source:{delivery['delivery']} -->\n{module.TRIAGE_BODY_END}"
                )
                triage._submit(42, issue, [issue])
                self.assertEqual(len(triage.dispatcher.deliveries), 1)

    def test_evidence_note_does_not_change_author_payload_or_decision_boundary(self):
        for path in WORKERS:
            with self.subTest(worker=str(path.relative_to(ROOT))):
                module = load_worker(path)
                triage = triage_with_comments(module, [])
                human_body = "A design decision is still missing."
                prompt = triage._prompt(
                    {"number": 1, "body": human_body},
                    [{"id": 7, "body": "What about compatibility?", "user": {"login": "author"}}],
                    [], [], "<!-- triage-source:test -->", "Missing screenshot",
                )
                payload = json.loads(prompt[prompt.index('\n\n{"issue":') + 2:])
                self.assertEqual(payload["issue"]["author_body"], human_body)
                self.assertEqual(payload["human_discussion"][0]["body"], "What about compatibility?")
                self.assertEqual(payload["readiness_check"], "Missing screenshot")
                self.assertIn("Do not include provisional acceptance criteria", prompt)
                self.assertIn("Never edit or delete human-authored", prompt)


if __name__ == "__main__":
    unittest.main()
