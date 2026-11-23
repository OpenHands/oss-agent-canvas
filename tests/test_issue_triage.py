import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    ROOT / "plugins/issue-triage/scripts/worker.py",
    ROOT / "automations/issue-triage-multi-repo-openhands-software-agent-sdk/tarball/worker.py",
]


def load_worker(path):
    adjacent = ("github_client", "agent_conversation")
    previous = {name: sys.modules.pop(name, None) for name in adjacent}
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("triage_worker", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)
        for name, old in previous.items():
            sys.modules.pop(name, None)
            if old is not None:
                sys.modules[name] = old


class CapturingDispatcher:
    def __init__(self):
        self.deliveries = []

    def deliver(self, **delivery):
        self.deliveries.append(delivery)
        return {"disposition": "captured", "conversation_id": "local"}


class IssueTriageTests(unittest.TestCase):
    def check_marker_refresh(self, in_comment):
        for path in MODULES:
            with self.subTest(source=str(path)):
                module = load_worker(path)
                comments = []

                class LocalWorker(module.IssueTriage):
                    def gh_pages(worker, endpoint):
                        self.assertEqual(endpoint, "/issues/41/comments")
                        return list(comments)

                    def gh(worker, *args, **kwargs):
                        raise AssertionError("Live GitHub calls are forbidden")

                # Replace only network boundaries; exercise real submission and prompt code.
                worker = object.__new__(LocalWorker)
                worker.repository = "local/triage"
                worker.token_name = "UNUSED_TOKEN"
                worker.dispatcher = CapturingDispatcher()
                author_body = "Human-owned issue text."
                issue = {"number": 41, "title": "A bounded issue", "body": author_body, "labels": []}
                legacy_digest = hashlib.sha256(json.dumps(
                    [2, issue["title"], author_body, []], sort_keys=True
                ).encode()).hexdigest()
                legacy_marker = f"<!-- triage-source:{legacy_digest} -->"

                def store_marker(marker):
                    if in_comment:
                        comments.append({"id": len(comments) + 1, "body": marker,
                                         "user": {"login": "automation"}})
                    else:
                        issue["body"] = (f"{author_body}\n\n{module.TRIAGE_BODY_START}\n"
                                         f"{marker}\n{module.TRIAGE_BODY_END}")

                def submit():
                    with contextlib.redirect_stdout(io.StringIO()):
                        worker._submit(17, issue, [issue])

                store_marker(legacy_marker)
                submit()
                self.assertEqual(len(worker.dispatcher.deliveries), 1)
                delivery = worker.dispatcher.deliveries[0]
                self.assertEqual(delivery["subject"], "17:issue:41")
                marker = f"<!-- triage-source:{delivery['delivery']} -->"
                self.assertNotEqual(marker, legacy_marker)
                store_marker(marker)
                submit()
                self.assertEqual(len(worker.dispatcher.deliveries), 1)
                self.assertEqual(module.author_body(issue["body"]), author_body)

    def test_old_body_marker_refreshes_once(self):
        self.check_marker_refresh(in_comment=False)

    def test_old_comment_marker_refreshes_once(self):
        self.check_marker_refresh(in_comment=True)


if __name__ == "__main__":
    unittest.main()
