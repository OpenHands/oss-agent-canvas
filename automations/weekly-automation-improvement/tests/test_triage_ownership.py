import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[3]
WORKERS = [
    ROOT / "plugins/issue-triage/scripts/worker.py",
    ROOT
    / "automations/issue-triage-multi-repo-openhands-software-agent-sdk/tarball/worker.py",
]


@pytest.mark.parametrize("path", WORKERS, ids=["plugin", "automation"])
@pytest.mark.parametrize(
    "label,body,skip",
    [
        ("automation-improvement", "Human issue", True),
        (None, "<!-- automation-improvement-topic: 123 -->\n## Goal", True),
        (None, "<!-- automation-improvement-triage: 2026-W44 -->\n## Goal", True),
        (
            None,
            "Human text mentioning <!-- automation-improvement-topic: 123 -->",
            False,
        ),
        (None, "Ordinary issue", False),
    ],
)
def test_managed_issue_is_skipped_before_comments_or_dispatch(
    path, label, body, skip, monkeypatch
):
    conversation = ModuleType("agent_conversation")
    conversation.AgentConversationDispatcher = object
    github = ModuleType("github_client")
    github.GitHubRepository = object
    github.run_repositories = lambda *a: None
    monkeypatch.setitem(sys.modules, "agent_conversation", conversation)
    monkeypatch.setitem(sys.modules, "github_client", github)
    spec = importlib.util.spec_from_file_location("triage_ownership_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    worker = module.IssueTriage()

    class ReadStarted(Exception):
        pass

    def read(*a):
        raise ReadStarted()

    worker.gh_pages = read
    issue = {"number": 1, "body": body, "labels": [{"name": label}] if label else []}
    if skip:
        assert worker._submit("Example/repo", issue, []) is None
    else:
        with pytest.raises(ReadStarted):
            worker._submit("Example/repo", issue, [])
