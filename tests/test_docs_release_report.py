"""Offline runner tests with real Git and SDK message persistence.

Only workspace setup, model execution and external GitHub effects are substituted.
Report delivery uses RemoteConversation, the server endpoint and LocalConversation.
"""

import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from openhands.agent_server.event_router import get_event_service, send_message
from openhands.agent_server.event_service import EventService
from openhands.agent_server.models import StoredConversation
from openhands.sdk import Agent, Conversation, LLM, RemoteConversation
from openhands.sdk.conversation import ConversationExecutionStatus
from openhands.sdk.event import ActionEvent, MessageEvent, ObservationEvent
from openhands.sdk.llm import Message, MessageToolCall, TextContent
from openhands.sdk.workspace import LocalWorkspace
from openhands.sdk.workspace.models import CommandResult
from openhands.tools.file_editor.definition import FileEditorAction, FileEditorObservation
from openhands.tools.terminal.definition import TerminalAction, TerminalObservation

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/docs-release-to-pr/scripts"
FALSE_REPORT = "No branch, label or repository writes were made. All edits belonged to a prior run."
HEADING = "## Runner activity report"


def action_event(action, tool):
    call_id = str(uuid4())
    return ActionEvent(
        thought=[], action=action, tool_name=tool, tool_call_id=call_id,
        tool_call=MessageToolCall(id=call_id, name=tool,
                                 arguments=action.model_dump_json(), origin="completion"),
        llm_response_id="offline-response",
    )


class WorkspaceBoundary:
    def __init__(self, repo):
        self.repo = repo
        self.fail_snapshot = False
        self.fail_history_at = None
        self.fail_delivery = False
        self.finish_status = ConversationExecutionStatus.FINISHED
        self.run_exception = None
        self.history_pages = []
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def clone_repos(self, config):
        return SimpleNamespace(success_count=1, failed_repos=[], repo_mappings={
            "docs": SimpleNamespace(local_path=str(self.repo))})

    def load_skills_from_agent_server(self, **kwargs):
        return [], None

    def get_repos_context(self, mappings):
        return "## Cloned Repositories\n- docs: " + str(self.repo)

    def get_llm(self, **kwargs):
        return SimpleNamespace(model="offline", api_key=None)

    def get_secrets(self):
        return {}

    def get_mcp_config(self):
        return {}

    def execute_command(self, command, **kwargs):
        self.commands.append(command)
        if self.fail_snapshot:
            raise RuntimeError("private transport error")
        p = subprocess.run(command, shell=True, cwd=self.repo, text=True,
                           capture_output=True, timeout=60)
        return CommandResult(command=command, exit_code=p.returncode, stdout=p.stdout,
                             stderr=p.stderr, timeout_occurred=False)


class ScriptedConversation(RemoteConversation):
    def __init__(self, workspace, callbacks, scenario, local, client, **kwargs):
        self.boundary = workspace
        self.callbacks = callbacks
        self.scenario = scenario
        self.local = local
        self._id = local.id
        self._client = client
        self._state = local.state
        self.runs = 0
        self.closed = False
        self.final_message = FALSE_REPORT

    @property
    def conversation_stats(self):
        return SimpleNamespace(get_combined_metrics=lambda: SimpleNamespace(accumulated_cost=0))

    def emit(self, event, callback=True):
        self.local.state.events.append(event)
        if callback:
            for cb in self.callbacks:
                cb(event)

    def tool(self, action, observation, tool, callback=True):
        event = action_event(action, tool)
        self.emit(event, callback)
        self.emit(ObservationEvent(tool_name=tool, tool_call_id=event.tool_call_id,
                                  action_id=event.id, observation=observation), callback)

    def terminal(self, command, callback=True):
        p = subprocess.run(command, shell=True, cwd=self.boundary.repo,
                           text=True, capture_output=True, timeout=20)
        self.tool(TerminalAction(command=command), TerminalObservation(
            command=command, exit_code=p.returncode,
            content=[TextContent(text=p.stdout + p.stderr)]), "terminal", callback)

    def label_attempt(self, command=None, code=0):
        command = command or "gh issue edit 7 --repo OpenHands/docs --add-label agent-in-progress"
        self.tool(TerminalAction(command=command), TerminalObservation(
            command=command, exit_code=code, content=[TextContent(text="fixture response")]),
            "terminal")

    def edit(self, name, text):
        path = self.boundary.repo / name
        old = path.read_text() if path.exists() else None
        path.write_text(text)
        self.tool(FileEditorAction(command="create", path=str(path), file_text=text),
                  FileEditorObservation(command="create", path=str(path), prev_exist=old is not None,
                                        old_content=old, new_content=text), "file_editor")

    def run(self):
        self.runs += 1
        self.scenario(self)
        if self.boundary.run_exception:
            raise self.boundary.run_exception
        self.emit(MessageEvent(source="agent", llm_message=Message(
            role="assistant", content=[TextContent(text=self.final_message)])))
        with self.local.state:
            self.local.state.execution_status = self.boundary.finish_status

    def close(self):
        self.closed = True


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "docs"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "guide.mdx").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.workspace = WorkspaceBoundary(self.repo)
        self.agent = Agent(llm=LLM(model="openai/gpt-4o", api_key="offline"), tools=[])
        self.persistence = Path(self.temp.name) / "conversations"
        self.local = Conversation(agent=self.agent, workspace=self.repo,
                                  persistence_dir=self.persistence, visualizer=None)
        self.addCleanup(self.local.close)
        stored = StoredConversation(id=self.local.id,
                                    workspace=LocalWorkspace(working_dir=str(self.repo)))
        self.service = EventService(stored=stored, conversations_dir=self.persistence)
        self.service._conversation = self.local
        self.requests = []
        app = FastAPI()
        app.dependency_overrides[get_event_service] = lambda: self.service
        app.add_api_route("/api/conversations/{conversation_id}/events", send_message,
                          methods=["POST"])

        @app.middleware("http")
        async def record_request(request, call_next):
            if request.method == "POST":
                payload = json.loads(await request.body())
                self.requests.append(payload)
                if len(self.requests) > 1 and self.workspace.fail_delivery:
                    return JSONResponse({}, status_code=400)
            return await call_next(request)

        @app.get("/api/conversations/{conversation_id}/events/search")
        def history(conversation_id: str, page_id: int = 0, limit: int = 100):
            self.workspace.history_pages.append(page_id)
            if self.workspace.fail_history_at == page_id:
                return JSONResponse({}, status_code=503)
            events = list(self.local.state.events)
            end = page_id + 2
            return {"items": [e.model_dump(mode="json") for e in events[page_id:end]],
                    "next_page_id": str(end) if end < len(events) else None}

        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.workspace.client = self.client

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def run_runner(self, scenario, expect_error=False, event_context=None):
        conversations = []

        def make_conversation(**kwargs):
            conv = ScriptedConversation(scenario=scenario, local=self.local,
                                        client=self.client, **kwargs)
            conversations.append(conv)
            return conv

        agent = SimpleNamespace(tools=[], condenser=None)
        env = {"AGENT_SERVER_URL": "http://testserver", "WORKSPACE_BASE": self.temp.name,
               "AUTOMATION_EVENT_PAYLOAD": json.dumps(event_context or {}), "AUTOMATION_CONFIG_PATH": ""}
        output = io.StringIO()
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        with patch.dict(os.environ, env), contextlib.redirect_stdout(output), \
                patch("openhands.sdk.workspace.remote.base.RemoteWorkspace", return_value=self.workspace), \
                patch("openhands.sdk.Conversation", side_effect=make_conversation), \
                patch("openhands.tools.preset.default.get_default_agent", return_value=agent):
            error = None
            try:
                runpy.run_path(str(SCRIPTS / "main.py"), run_name="__main__")
            except RuntimeError as exc:
                error = exc
        if expect_error:
            self.assertIsNotNone(error)
        elif error:
            raise error
        self.error = error
        conv = conversations[0]
        self.assertTrue(conv.closed)
        self.assertEqual(conv.runs, 1, "The report must not invoke the model again")
        self.assertTrue(all(r["run"] is False for r in self.requests))
        self.output = output.getvalue()
        return conv

    def report(self):
        messages = [e for e in self.local.state.events if isinstance(e, MessageEvent)]
        self.assertEqual(len(messages), 3,
                         "A runner correction must be persisted after the task and agent answer")
        self.assertEqual(messages[-2].source, "agent")
        self.assertEqual(messages[-1].source, "user")
        self.assertEqual(self.local.state.execution_status, ConversationExecutionStatus.IDLE)
        text = messages[-1].llm_message.content[0].text
        self.assertTrue(text.startswith(HEADING))
        self.assertIn("qualifies the preceding agent answer", text)
        self.assertIn("Remote effects: not independently verified", text)
        self.assertIn("Side-effect-free execution is not verified", text)
        self.assertNotIn("private transport error", text)
        # Reading a fresh conversation proves this is not merely a client cache write.
        restored = Conversation(agent=self.agent, workspace=self.repo,
                                persistence_dir=self.persistence, conversation_id=self.local.id,
                                visualizer=None)
        try:
            self.assertEqual(list(restored.state.events)[-1].llm_message.content[0].text, text)
        finally:
            restored.close()
        return text


class RunnerRegressionTests(RunnerCase):
    def test_correction_is_delivered_after_false_no_write_answer(self):
        def scenario(conv):
            conv.label_attempt()
            conv.terminal("git checkout -qb automation/docs-v2-release")
            conv.edit("guide.mdx", "this run's edit\n")
            conv.edit("release.mdx", "this run's page\n")
        self.run_runner(scenario)
        text = self.report()
        self.assertIn("Changed: branch,", text)
        self.assertIn("worktree", text)
        self.assertIn("dirty before=no; dirty after=yes", text)
        self.assertIn("Possible GitHub label-write commands: 1", text)
        self.assertIn("attempts only", text)
        self.assertEqual((self.repo / "guide.mdx").read_text(), "this run's edit\n")
        self.assertEqual(self.git("branch", "--show-current"), "automation/docs-v2-release")

    def test_reverted_changes_are_not_reported_as_no_writes(self):
        def scenario(conv):
            conv.label_attempt(code=5)
            conv.terminal("git checkout -qb automation/docs-v2-release; false")
            conv.edit("guide.mdx", "temporary\n")
            conv.terminal("git restore guide.mdx; git checkout -q main; git branch -D automation/docs-v2-release")
        self.run_runner(scenario)
        text = self.report()
        self.assertIn("No final Git differences observed", text)
        self.assertIn("does not erase earlier tool attempts", text)
        self.assertIn("File-edit attempts: 1", text)
        self.assertIn("Possible GitHub label-write commands: 1", text)

    def test_persisted_history_recovers_missed_callback(self):
        self.run_runner(lambda c: c.terminal("git checkout -qb automation/docs-v3-release", callback=False))
        text = self.report()
        self.assertIn("Terminal attempts: 1", text)
        self.assertGreater(len(self.workspace.history_pages), 1)

    def test_compound_api_command_is_not_confirmed_by_shell_exit(self):
        def scenario(conv):
            self.git("checkout", "-qb", "automation/docs-v2-release")
            conv.label_attempt(
                "curl -X POST -H 'Authorization: Bearer PRIVATE_SENTINEL' "
                "https://api.github.com/repos/OpenHands/docs/issues/7/labels "
                "-d '{\"labels\":[\"agent-in-progress\"]}' | jq '.labels'; "
                "git checkout -q -b automation/docs-v2-release main")
        self.run_runner(scenario)
        text = self.report()
        self.assertIn("Possible GitHub label-write commands: 1", text)
        self.assertNotIn("PRIVATE_SENTINEL", text)
        self.assertNotIn("Authorization", text)

    def test_incomplete_history_and_snapshot_are_visible(self):
        self.workspace.fail_snapshot = True
        self.workspace.fail_history_at = 0
        self.run_runner(lambda c: c.label_attempt())
        text = self.report()
        self.assertIn("History incomplete", text)
        self.assertIn("Git comparison unavailable", text)

    def test_same_dirty_status_but_changed_contents_are_observed(self):
        (self.repo / "guide.mdx").write_text("pre-existing edit\n")
        self.run_runner(lambda c: c.edit("guide.mdx", "changed again\n"))
        text = self.report()
        self.assertIn("worktree", text)
        self.assertIn("dirty before=yes; dirty after=yes", text)
        self.assertIn("not proof of which run owns", text)

    def test_delivery_failure_fails_run_instead_of_reporting_all_ok(self):
        self.workspace.fail_delivery = True
        self.run_runner(lambda c: c.edit("guide.mdx", "preserve this\n"), expect_error=True)
        self.assertNotIn("ALL_OK", self.output)
        self.assertEqual((self.repo / "guide.mdx").read_text(), "preserve this\n")


    def test_report_includes_noop_without_changing_agent_answer(self):
        conv = self.run_runner(lambda c: None)
        self.assertEqual(conv.final_message, FALSE_REPORT)
        self.assertIn("No final Git differences observed", self.report())

    def test_nullable_action_does_not_discard_git_evidence(self):
        self.workspace.fail_history_at = 2
        def scenario(conv):
            conv.emit(ActionEvent(
                thought=[], action=None, tool_name="terminal", tool_call_id="malformed",
                tool_call=MessageToolCall(id="malformed", name="terminal", arguments="{}",
                                         origin="completion"), llm_response_id="offline"))
            conv.edit("guide.mdx", "keep real evidence\n")
        self.run_runner(scenario)
        text = self.report()
        self.assertIn("History incomplete", text)
        self.assertIn("File-edit attempts: 1", text)
        self.assertIn("worktree", text)

    def test_unfinished_return_does_not_append_new_input(self):
        self.workspace.finish_status = ConversationExecutionStatus.PAUSED
        self.run_runner(lambda c: None, expect_error=True)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("ALL_OK", self.output)

    def test_missing_objects_cannot_launch_configured_transport(self):
        canary = Path(self.temp.name) / "transport-executed"
        helper = self.repo / "transport.sh"
        helper.write_text("touch " + str(canary) + "\nexit 1\n")
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.url", "ext::sh " + str(helper))
        tree = self.git("rev-parse", "HEAD^{tree}")
        (self.repo / ".git/objects" / tree[:2] / tree[2:]).unlink()
        with patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "ext"}):
            self.run_runner(lambda c: None)
        self.assertFalse(canary.exists())
        self.assertIn("comparison unavailable", self.report())

    def test_normalizing_filters_do_not_invent_worktree_changes(self):
        self.git("config", "filter.normalize.clean", "tr A-Z a-z")
        (self.repo / ".gitattributes").write_text("guide.mdx filter=normalize\n")
        (self.repo / "guide.mdx").write_text("CONTENT\n")
        self.git("add", ".")
        self.git("commit", "-qm", "normalizing filter fixture")
        os.utime(self.repo / "guide.mdx", (946684800, 946684800))
        self.run_runner(lambda c: c.terminal("git status --porcelain=v1"))
        self.assertIn("comparison unavailable", self.report())
        self.assertNotIn("Changed: worktree", self.report())
        self.assertEqual((self.repo / "guide.mdx").read_text(), "CONTENT\n")

    def test_parent_filters_and_fsmonitor_are_not_executed(self):
        canary = Path(self.temp.name) / "filter-executed"
        filter_command = "touch " + str(canary)
        self.git("config", "filter.parent.clean", filter_command)
        self.git("config", "filter.parent.process", filter_command)
        self.git("config", "filter.parent.required", "true")
        self.git("config", "core.fsmonitor", filter_command)
        (self.repo / ".gitattributes").write_text("guide.mdx filter=parent\n")
        self.run_runner(lambda c: c.edit("guide.mdx", "dirty parent\n"))
        self.assertIn("comparison unavailable", self.report())
        self.assertFalse(canary.exists())

    def test_submodule_filters_and_fsmonitor_are_not_executed(self):
        canary = Path(self.temp.name) / "filter-executed"
        filter_command = "touch " + str(canary)
        # A dirty submodule has its own filter config, unknown to the parent.
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q",
                 str(self.repo), "nested")
        nested = self.repo / "nested"
        subprocess.run(["git", "-C", str(nested), "config", "filter.child.clean", filter_command],
                       check=True)
        (nested / ".gitattributes").write_text("guide.mdx filter=child\n")
        (nested / "guide.mdx").write_text("dirty submodule\n")
        self.git("commit", "-qam", "add submodule")
        self.git("config", "core.fsmonitor", filter_command)
        (self.repo / "guide.mdx").write_text("dirty parent\n")
        self.run_runner(lambda c: c.edit("guide.mdx", "second dirty parent\n"))
        text = self.report()
        self.assertIn("worktree", text)
        self.assertIn("submodule worktrees", text)
        self.assertFalse(canary.exists())


class RunnerControlTests(RunnerCase):
    def test_read_only_noop_preserves_repository(self):
        before = self.git("status", "--porcelain=v1")
        def scenario(conv):
            conv.terminal("git status -sb")
            conv.final_message = "no action needed: trigger issue already resolved"
        conv = self.run_runner(scenario)
        self.assertEqual(self.git("status", "--porcelain=v1"), before)
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual((self.repo / "guide.mdx").read_text(), "original\n")
        messages = [e for e in self.local.state.events if isinstance(e, MessageEvent)]
        self.assertIn(conv.final_message, [e.llm_message.content[0].text for e in messages])

    def test_successful_docs_work_and_pr_result_are_preserved(self):
        def scenario(conv):
            conv.terminal("git checkout -qb automation/docs-v4-release")
            conv.edit("guide.mdx", "supported docs update\n")
            conv.terminal("git add guide.mdx && git commit -qm 'docs update'")
            conv.final_message = "draft PR created (https://github.com/example/docs/pull/8)"
        conv = self.run_runner(scenario)
        self.assertEqual(self.git("status", "--porcelain=v1"), "")
        self.assertEqual((self.repo / "guide.mdx").read_text(), "supported docs update\n")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "docs update")
        messages = [e for e in self.local.state.events if isinstance(e, MessageEvent)]
        self.assertIn(conv.final_message, [e.llm_message.content[0].text for e in messages])


    def test_execution_timeout_preserves_error_without_sending_report(self):
        failure = RuntimeError("fixture server execution may still be running")
        self.workspace.run_exception = failure
        self.workspace.fail_delivery = True
        self.run_runner(lambda c: c.edit("guide.mdx", "preserve on timeout\n"), expect_error=True)
        self.assertIs(self.error, failure)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("ALL_OK", self.output)
        self.assertEqual((self.repo / "guide.mdx").read_text(), "preserve on timeout\n")


if __name__ == "__main__":
    unittest.main()
