import json

import pytest

from budget import RunBudget
from common import write_json
from test_support import install_sdk
from test_topic_issue import GitHub
from test_triage import topic
from test_weekly import inventory, record, SIGNAL
from test_tracked_work import work
import topic_issue
import tracked_work
import triage_workflow as weekly


@pytest.mark.parametrize("comment_fails", [False, True])
def test_native_triage_reconciles_existing_work_before_publication(
    tmp_path, monkeypatch, comment_fails
):
    rows = [record(i) for i in range(1, 5)]
    rows[0]["event"]["payload"] = json.dumps({"summary": "PRIVATE CUSTOMER CONTENT"})
    frame = tmp_path / "inventory.json"
    write_json(frame, inventory(rows))
    existing = [work(10), work(11, kind="pull")]
    identifier = rows[0]["event"]["id"]
    for item in existing:
        item["body"] = (
            f"<!-- automation-improvement-event: {identifier} -->\nPrior reviewer work"
        )
    github = GitHub(private=False)
    original = [item["body"] for item in existing]
    github.issues = [
        {
            "number": i + 10,
            "html_url": item["url"],
            "state": "open",
            "body": item["body"],
        }
        for i, item in enumerate(existing)
    ]
    monkeypatch.setattr(topic_issue, "command", github)

    def request(argv):
        if (
            comment_fails
            and "--method" in argv
            and argv[-3].endswith("/issues/11/comments")
        ):
            raise RuntimeError("Comment permission denied")
        return github(argv)

    monkeypatch.setattr(tracked_work, "command", request)
    monkeypatch.setattr(weekly, "github_auth", lambda _: None)
    monkeypatch.setattr(weekly, "repository_catalog", lambda _: {"Example/source": {}})
    monkeypatch.setattr(weekly, "existing_work", lambda _: existing)
    monkeypatch.setattr(weekly, "refresh_epic", lambda *a: {"status": "refreshed"})
    monkeypatch.setattr(
        weekly,
        "publish",
        lambda config,
        context,
        candidate,
        existing,
        output: f"https://github.com/Example/source/issues/{candidate['priority'] + 20}",
    )
    plan = [
        topic("e1", "different-key-for-prior-work", 0),
        topic("e2", "new-two", 1),
        topic("e3", "new-three", 2),
        topic("e4", "new-four", 3),
    ]
    for candidate in plan:
        candidate["existing_work_urls"] = []
        candidate["brief"] = {
            "problem": "A repairable problem affects automation runs.",
            "example": "An automation reported success despite a missing result.",
            "prevention": "Verify that the result exists before reporting success.",
            "validation": "Compare a successful run and a run with a missing result.",
        }

    def execute(workspace):
        root = workspace.parent
        write_json(root / "result.json", {"topics": plan, "no_action_reason": ""})

    captured = install_sdk(monkeypatch, execute)
    config = {
        "repository": "Example/source",
        "checkout": str(tmp_path),
        "project_id": "00000000-0000-0000-0000-000000000001",
        "signal_id": SIGNAL,
        "input": str(frame),
        "max_topics_per_run": 2,
    }
    summary = weekly.run_locked(
        config, tmp_path, dry_run=False, limit=3, seed=1, budget=RunBudget({})
    )
    assert sum(t["status"] == "topic_created" for t in summary["topics"]) == 3
    assert [item["body"] for item in github.issues[:2]] == original
    assert "PRIVATE CUSTOMER CONTENT" not in json.dumps(github.issues + github.comments)
    assert "without losing their readiness or human priority" in captured["prompt"]
    state = json.loads((tmp_path / "state.json").read_text())
    assert (identifier in state["events"]) is not comment_fails
    assert (identifier in state["backlog"]) is comment_fails
    outcome = next(
        item
        for item in summary["topics"]
        if item["topic"]["key"] == "different-key-for-prior-work"
    )
    assert outcome["status"] == (
        "recurrence_pending" if comment_fails else "recurrence_recorded"
    )
    recurrence = [
        c for c in github.comments if "automation-improvement-recurrence:" in c["body"]
    ]
    assert len(recurrence) == (1 if comment_fails else 2)
    for item in recurrence:
        assert identifier not in item["body"]
        assert rows[0]["event"]["trace_id"] not in item["body"]
        assert "Private evidence reference:" in item["body"]
    public_plan = github.issues[2]["body"]
    assert "https://github.com/Example/source/issues/21" in public_plan
    assert recurrence[0]["html_url"] in public_plan
    if comment_fails:
        assert "One or more recurrence updates are pending." in public_plan
        comment_fails = False
        monkeypatch.setattr(
            weekly, "triage", lambda *args: pytest.fail("Re-ranked an existing plan")
        )
        recovered = weekly.run_locked(
            config, tmp_path, dry_run=False, limit=3, seed=1, budget=RunBudget({})
        )
        assert recovered["publication"] == {"status": "verified"}
        recurrence = [
            c
            for c in github.comments
            if "automation-improvement-recurrence:" in c["body"]
        ]
        assert len(recurrence) == 2
        assert all(c["html_url"] in github.issues[2]["body"] for c in recurrence)
        assert (
            "One or more recurrence updates are pending."
            not in github.issues[2]["body"]
        )
