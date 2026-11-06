import contextlib
import copy
import importlib.util
import io
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    ROOT / "plugins/issue-triage/scripts",
    ROOT / "automations/issue-triage-multi-repo-openhands-software-agent-sdk/tarball",
]


def load_scripts(directory):
    modules = {}
    for name in ("agent_conversation", "github_client", "worker"):
        spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(module)
        modules[name] = module
    return modules


class GitHubBoundary:
    """Offline GitHub state; no customer issue is ever mutated by these tests."""

    def __init__(self, issue, publish=None):
        self.issue = copy.deepcopy(issue)
        self.comments = []
        self.publish = publish
        self.deliveries = []
        self.read_error = None

    def deliver(self, **kwargs):
        self.deliveries.append(kwargs)
        if self.publish:
            self.publish(self, kwargs["prompt"])
        return {"disposition": "created", "conversation_id": "isolated"}

    def gh(self, method, path):
        if self.read_error:
            raise self.read_error
        if path == "":
            return {"id": 1}
        return copy.deepcopy(self.issue)

    def gh_pages(self, path):
        if self.read_error:
            raise self.read_error
        return copy.deepcopy(self.comments)


def issue():
    return {
        "number": 1,
        "title": "Example request",
        "body": "Human-owned request.",
        "labels": [{"name": "type:feature"}],
    }


def marker(prompt):
    return prompt.split("\n\nMarker: ", 1)[1].split("\n", 1)[0]


def publish_ready(boundary, prompt):
    boundary.issue["body"] += (
        "\n\n<!-- openhands-ai-triage:start -->\n---\n## OpenHands AI triage\n"
        "**The following comments and acceptance criteria were added by the OpenHands AI agent.**\n"
        "### Triage\nBounded scope.\n### Acceptance Criteria\n- [ ] Observable result\n"
        + marker(prompt)
        + "\n<!-- openhands-ai-triage:end -->"
    )
    boundary.issue["labels"] += [
        {"name": "priority:low"},
        {"name": "ready-for-dev"},
    ]


def publish_decision(boundary, prompt):
    boundary.issue["labels"].append({"name": "priority:low"})
    boundary.comments.append(
        {
            "id": 10,
            "body": "---\n**The following comments were added by the OpenHands AI agent.**\n\n"
            "### Decision needed\nWhich behavior is intended?\n\n" + marker(prompt),
        }
    )


class TriageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = [load_scripts(path) for path in SCRIPTS]

    def repository(self, modules, boundary):
        worker = modules["worker"].IssueTriage.__new__(modules["worker"].IssueTriage)
        worker.repository = "example/repository"
        worker.token_name = "GITHUB_TOKEN_ISSUE_TRIAGE"
        worker.dispatcher = boundary
        worker.gh = boundary.gh
        worker.gh_pages = boundary.gh_pages
        return worker

    def test_denied_required_writes_do_not_complete(self):
        for modules in self.modules:
            boundary = GitHubBoundary(issue())
            worker = self.repository(modules, boundary)
            with self.subTest(module=modules["worker"].__file__):
                with self.assertRaisesRegex(RuntimeError, "not verified"):
                    worker._submit(1, issue(), [issue()])
                self.assertEqual(len(boundary.deliveries), 1)

    def test_verified_outcomes_complete_and_redelivery_skips(self):
        for modules in self.modules:
            for publish in (publish_ready, publish_decision):
                with self.subTest(module=modules["worker"].__file__, publish=publish):
                    boundary = GitHubBoundary(issue(), publish)
                    worker = self.repository(modules, boundary)
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        worker._submit(1, issue(), [issue()])
                    self.assertIn('"outcome":', output.getvalue())
                    worker._submit(1, boundary.issue, [boundary.issue])
                    self.assertEqual(len(boundary.deliveries), 1)

    def test_partial_ready_marker_does_not_suppress_missing_labels(self):
        for modules in self.modules:
            boundary = GitHubBoundary(issue(), publish_ready)
            worker = self.repository(modules, boundary)
            worker._submit(1, issue(), [issue()])
            boundary.issue["labels"] = [{"name": "type:feature"}]
            boundary.publish = None
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                worker._submit(1, boundary.issue, [boundary.issue])
            self.assertEqual(len(boundary.deliveries), 2)

    def test_human_body_or_unrelated_label_loss_fails(self):
        for modules in self.modules:
            for field in ("body", "labels"):

                def publish(boundary, prompt, field=field):
                    publish_ready(boundary, prompt)
                    if field == "body":
                        boundary.issue["body"] = boundary.issue["body"].replace(
                            "Human-owned request.", "Changed request."
                        )
                    else:
                        boundary.issue["labels"] = boundary.issue["labels"][1:]

                with self.subTest(module=modules["worker"].__file__, field=field):
                    boundary = GitHubBoundary(issue(), publish)
                    worker = self.repository(modules, boundary)
                    with self.assertRaisesRegex(RuntimeError, "not verified"):
                        worker._submit(1, issue(), [issue()])

    def test_final_read_failure_propagates(self):
        for modules in self.modules:

            def publish(boundary, prompt):
                publish_ready(boundary, prompt)
                boundary.read_error = RuntimeError("unavailable")

            boundary = GitHubBoundary(issue(), publish)
            worker = self.repository(modules, boundary)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                worker._submit(1, issue(), [issue()])

    def test_issue_failure_reaches_automation_context(self):
        for modules in self.modules:
            boundary = GitHubBoundary(issue())
            worker = self.repository(modules, boundary)
            worker._event_target = lambda: None
            worker.open_issues = lambda: [issue()]
            worker.dependencies_complete = lambda current: True
            with self.assertRaises(RuntimeError):
                worker.run()

    def test_optional_old_comment_cleanup_denial_is_not_required(self):
        for modules in self.modules:
            boundary = GitHubBoundary(issue(), publish_ready)
            boundary.comments = [{"id": 2, "body": "<!-- triage-source:old -->"}]
            worker = self.repository(modules, boundary)
            worker._submit(1, issue(), [issue()])
            self.assertEqual(len(boundary.comments), 1)

    def dispatcher(self, module):
        dispatcher = module.AgentConversationDispatcher.__new__(
            module.AgentConversationDispatcher
        )
        dispatcher._deadline = time.monotonic() + 10
        dispatcher._workspace = object()
        dispatcher._secrets = {}
        dispatcher.automation_id = str(uuid4())
        dispatcher.profile_id = uuid4()
        return dispatcher

    def test_dispatcher_waits_and_retries_unverified_finished_delivery(self):
        # Agent Server and KV are external boundaries; the real dispatcher and
        # SDK request models execute without a live server or customer writes.
        for modules in self.modules:
            module = modules["agent_conversation"]
            status = module.ConversationExecutionStatus
            for current_status in (
                status.FINISHED,
                status.ERROR,
                status.RUNNING,
                status.IDLE,
            ):
                with self.subTest(module=module.__file__, status=current_status):
                    remote = RemoteBoundary(current_status, status.FINISHED)
                    dispatcher = self.dispatcher(module)
                    with (
                        patch.object(
                            module, "_kv_request", return_value={"delivery": "same"}
                        ),
                        patch.object(
                            module.RemoteConversation, "attach", return_value=remote
                        ),
                    ):
                        dispatcher.deliver("issue:1", "same", "triage")
                    self.assertEqual(len(remote.runs), 1)
                    self.assertNotEqual(remote.runs[0].get("blocking"), False)
                    self.assertGreater(remote.runs[0]["timeout"], 0)
                    self.assertEqual(
                        bool(remote.messages),
                        current_status in (status.FINISHED, status.ERROR),
                    )
                    self.assertTrue(remote.closed)

    def test_dispatcher_incomplete_status_fails(self):
        for modules in self.modules:
            module = modules["agent_conversation"]
            status = module.ConversationExecutionStatus
            for end in (
                status.PAUSED,
                status.STUCK,
                status.ERROR,
                status.WAITING_FOR_CONFIRMATION,
            ):
                remote = RemoteBoundary(status.IDLE, end)
                dispatcher = self.dispatcher(module)
                with (
                    patch.object(
                        module, "_kv_request", return_value={"delivery": "same"}
                    ),
                    patch.object(
                        module.RemoteConversation, "attach", return_value=remote
                    ),
                    self.assertRaisesRegex(RuntimeError, "did not finish"),
                ):
                    dispatcher.deliver("issue:1", "same", "triage")
                self.assertTrue(remote.closed)

    def test_running_old_revision_cannot_acknowledge_new_revision(self):
        for modules in self.modules:
            module = modules["agent_conversation"]
            status = module.ConversationExecutionStatus
            remote = RemoteBoundary(status.RUNNING, status.FINISHED)
            dispatcher = self.dispatcher(module)
            with (
                patch.object(module, "_kv_request", return_value={"delivery": "old"}),
                patch.object(module.RemoteConversation, "attach", return_value=remote),
                self.assertRaisesRegex(RuntimeError, "still running"),
            ):
                dispatcher.deliver("issue:1", "new", "triage")
            self.assertEqual(remote.messages, [])
            self.assertTrue(remote.closed)

    def test_dispatcher_timeout_propagates_and_closes(self):
        for modules in self.modules:
            module = modules["agent_conversation"]
            status = module.ConversationExecutionStatus
            remote = RemoteBoundary(
                status.IDLE, status.FINISHED, TimeoutError("deadline")
            )
            dispatcher = self.dispatcher(module)
            with (
                patch.object(module, "_kv_request", return_value={"delivery": "same"}),
                patch.object(module.RemoteConversation, "attach", return_value=remote),
                self.assertRaises(TimeoutError),
            ):
                dispatcher.deliver("issue:1", "same", "triage")
            self.assertTrue(remote.closed)

    def test_human_comment_loss_fails(self):
        for modules in self.modules:

            def publish(boundary, prompt):
                publish_ready(boundary, prompt)
                boundary.comments = []

            boundary = GitHubBoundary(issue(), publish)
            boundary.comments = [
                {"id": 1, "body": "Human discussion", "user": {"login": "human"}}
            ]
            worker = self.repository(modules, boundary)
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                worker._submit(1, issue(), [issue()])


class RemoteBoundary:
    def __init__(self, status, end_status, error=None):
        self.state = SimpleNamespace(execution_status=status)
        self.end_status = end_status
        self.error = error
        self.runs = []
        self.messages = []
        self.closed = False

    def update_secrets(self, secrets):
        pass

    def send_message(self, message):
        self.messages.append(message)

    def run(self, **kwargs):
        self.runs.append(kwargs)
        if self.error:
            raise self.error
        self.state.execution_status = self.end_status

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
