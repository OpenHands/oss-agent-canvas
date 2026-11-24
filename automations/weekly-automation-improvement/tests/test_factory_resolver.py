import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RESOLVERS = [
    ROOT / "plugins/github-issue-resolver/scripts/main.py",
    next((ROOT / "automations").glob("github-issue-resolver-multi-*/tarball/main.py")),
]


@pytest.mark.parametrize("path", RESOLVERS, ids=["plugin", "tarball"])
def test_factory_resolver_admission_and_ambiguous_start(path, tmp_path, monkeypatch):
    sdk = types.ModuleType("openhands.sdk.automation")
    sdk.automation_conversation_kwargs = lambda **kwargs: {}
    monkeypatch.setitem(sys.modules, sdk.__name__, sdk)
    spec = importlib.util.spec_from_file_location("fixture_resolver", path)
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    config = tmp_path / "install.json"
    repo, assignee = "Example/another-project", "factory-assignee"
    config.write_text(
        json.dumps({"factory_repositories": [repo], "factory_assignee": assignee})
    )
    monkeypatch.setenv("AUTOMATION_CONFIG_PATH", str(config))
    monkeypatch.setenv(
        "AUTOMATION_EVENT_PAYLOAD",
        json.dumps({"event": {"payload": {"repository": {"full_name": repo}}}}),
    )
    monkeypatch.setenv("WORKSPACE_BASE", str(tmp_path))
    entries = resolver._configured_repositories()
    assert entries == [{"repo": repo, "factory_only": True, "bot_username": assignee}]
    monkeypatch.setattr(resolver, "_require_kv", lambda: None)
    monkeypatch.setattr(
        resolver,
        "get_secret",
        lambda key: {
            "GITHUB_TOKEN": "fixture",
            "FACTORY_DISPATCHER_GITHUB_LOGIN": "dispatcher",
        }[key],
    )
    issue = {
        "number": 42,
        "state": "open",
        "title": "Visible title",
        "body": "Existing brief.",
        "labels": [{"name": resolver.EXTERNAL_LABEL}, {"name": resolver.FACTORY_LABEL}],
        "html_url": f"https://github.com/{repo}/issues/42",
    }
    dispatcher_status = {
        "body": resolver.FACTORY_STATUS
        + "\n\n**Owner:** External Factory (assignment).\n\nAssigning this issue to the configured resolver.",
        "user": {"login": "reporter"},
    }
    assigner = {"login": "dispatcher"}
    timeline = [
        {"event": "labeled", "label": {"name": resolver.EXTERNAL_LABEL}},
        {"event": "labeled", "label": {"name": resolver.FACTORY_LABEL}},
        {"event": "assigned", "assignee": {"login": assignee}, "actor": assigner},
    ]

    def pages(token, endpoint, params):
        if endpoint.endswith("/pulls"):
            assert prs and params["state"] == "all"
            return [
                {
                    "user": {"login": "resolver-writer"},
                    "head": {"ref": prs[0][3]},
                    "body": prs[0][5],
                }
            ]
        if endpoint.endswith("/issues"):
            assert params["assignee"] == assignee
            return [issue]
        if endpoint.endswith("/timeline"):
            return timeline
        if endpoint.endswith("/comments"):
            return [dispatcher_status]
        raise AssertionError(endpoint)

    monkeypatch.setattr(resolver, "_github_paginate", pages)
    monkeypatch.setattr(
        resolver,
        "_verify_token_and_repo",
        lambda *_: {"visibility": "public", "default_branch": "main"},
    )
    monkeypatch.setattr(
        resolver, "_is_trusted_actor", lambda *args: args[-1] == "dispatcher"
    )
    state = resolver._compact_state(None)

    def save(value, version):
        state.clear()
        state.update(copy.deepcopy(value))
        return True

    monkeypatch.setattr(resolver, "_save_state", save)
    monkeypatch.setattr(resolver, "_load_state", lambda: (copy.deepcopy(state), 1))
    monkeypatch.setattr(
        resolver, "_prepare_repository", lambda *args: tmp_path / "checkout"
    )
    monkeypatch.setattr(resolver, "_get_agent_dict", lambda: {})
    conversations, prs = [], []

    def agent(method, endpoint, payload=None):
        if endpoint.endswith("/settings/secrets"):
            return {"secrets": [{"name": "GITHUB_TOKEN"}, {"name": "LINEAR_API_KEY"}]}
        if method == "POST":
            assert set(payload["secrets"]) == {"GITHUB_TOKEN"}
            conversations.append(payload["conversation_id"])
            raise OSError("conversation created, response lost")
        assert endpoint.endswith(conversations[0])
        return {"id": conversations[0], "execution_status": "finished"}

    monkeypatch.setattr(resolver, "_oh_request", agent)
    monkeypatch.setattr(resolver, "_get_issue", lambda *_: issue)
    monkeypatch.setattr(resolver, "_branch_exists", lambda *_: {"name": "issue-42"})
    monkeypatch.setattr(resolver, "_commits_ahead", lambda *_: 1)

    def open_pr(*args):
        prs.append(args)
        raise OSError("pull request created, response lost")

    monkeypatch.setattr(
        resolver, "_github_request", lambda *args: ({"login": "resolver-writer"}, {})
    )
    monkeypatch.setattr(resolver, "_open_pr", open_pr)
    monkeypatch.setattr(resolver, "DONE_DEBOUNCE", 0)
    resolver.main()
    assert not conversations
    dispatcher_status["user"]["login"] = "dispatcher"
    issue["labels"].append({"name": "improvement-customer"})
    resolver.main()
    assert not conversations
    issue["labels"].pop()
    assigner["login"] = "reporter"
    resolver.main()
    assert not conversations
    assigner["login"] = "dispatcher"
    resolver.main()
    assert len(conversations) == 1
    assert next(iter(state["active"].values()))["status"] == "starting"
    resolver.main()
    resolver.main()
    resolver.main()
    assert len(conversations) == 1 and len(prs) == 1
    assert prs[0][-1] is True and prs[0][3] == "openhands/issue-42"
    assert "Existing brief" not in prs[0][5]
    assert not state["active"]
