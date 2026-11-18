import json
import time
from types import SimpleNamespace

import pytest

from budget import RunBudget
from common import topic_id, write_json
from test_coding_agent import install_sdk
from test_topic_issue import GitHub
from test_triage import topic
from test_weekly import inventory, record, SIGNAL
import parallel
import topic_issue
import tracked_work
import triage
import triage_agent
import weekly


def work(number, key="known-problem", kind="issue", repository="Example/source"):
    return {
        "url": f"https://github.com/{repository}/{'pull' if kind == 'pull' else 'issues'}/{number}",
        "repository": repository,
        "title": "Tracked problem",
        "body": f"<!-- automation-improvement-topic: {topic_id('shared', key)} -->\nExisting human explanation.",
        "kind": kind,
        "state": "OPEN",
        "createdAt": "2026-10-25T00:00:00Z",
    }


def test_matching_uses_stable_identity_and_expands_semantic_matches_across_repos():
    existing = [
        work(10),
        work(11, kind="pull", repository="Example/sdk"),
        work(12, "other"),
    ]
    assert tracked_work.matching(
        {"id": topic_id("shared", "known-problem")}, existing
    ) == sorted([existing[0]["url"], existing[1]["url"]])
    assert tracked_work.matching(
        {"id": "new-name", "existing_work_urls": [existing[0]["url"]]}, existing
    ) == sorted([existing[0]["url"], existing[1]["url"]])


@pytest.mark.parametrize(
    "urls", [["https://github.com/Foreign/repo/issues/1"], [None], "not-a-list"]
)
def test_triage_rejects_foreign_or_malformed_work_references(urls):
    rows = [record()]
    _, references, _ = triage.prepare(rows, [work(10)], [])
    candidate = topic("e1") | {"existing_work_urls": urls}
    with pytest.raises(ValueError, match="existing work"):
        triage.validate(
            {"topics": [candidate], "no_action_reason": ""}, rows, references, 3
        )


def test_triage_requires_an_explicit_existing_work_decision():
    rows = [record()]
    _, references, _ = triage.prepare(rows, [work(10)], [])
    candidate = topic("e1")
    del candidate["existing_work_urls"]
    with pytest.raises(ValueError, match="Invalid topic fields"):
        triage.validate(
            {"topics": [candidate], "no_action_reason": ""}, rows, references, 3
        )


@pytest.mark.parametrize("comment_fails", [False, True])
def test_native_triage_reconciles_existing_work_before_parallel_repair_slots(
    tmp_path, monkeypatch, capsys, comment_fails
):
    rows = [record(i) for i in range(1, 5)]
    rows[0]["event"]["payload"] = json.dumps({"summary": "PRIVATE CUSTOMER CONTENT"})
    frame = tmp_path / "inventory.json"
    write_json(frame, inventory(rows))
    existing = [work(10), work(11, kind="pull")]
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
        if comment_fails and "--method" in argv:
            raise RuntimeError("Comment permission denied")
        return github(argv)

    monkeypatch.setattr(tracked_work, "command", request)
    monkeypatch.setattr(weekly, "github_auth", lambda _: None)
    monkeypatch.setattr(weekly, "repository_catalog", lambda _: {"Example/source": {}})
    monkeypatch.setattr(weekly, "existing_work", lambda _: existing)
    monkeypatch.setattr(parallel.time, "sleep", lambda _: None)
    plan = [
        topic("e1", "known-problem", 0),
        topic("e2", "new-two", 1),
        topic("e3", "new-three", 2),
        topic("e4", "new-four", 3),
    ]
    for candidate in plan:
        candidate["existing_work_urls"] = []
    plan[0]["existing_work_urls"] = [existing[0]["url"]]

    def execute(workspace):
        root = workspace.parent
        if not comment_fails:
            triage_agent.evidence(
                root / "context.json", "comment_existing_topic", plan[0]
            )
            posted = json.loads(capsys.readouterr().out)
            assert len(posted) == 2
        write_json(root / "result.json", {"topics": plan, "no_action_reason": ""})

    captured = install_sdk(monkeypatch, execute)
    launched = []

    def launch(config, context, candidate, budget, root):
        recurrence = [
            c
            for c in github.comments
            if "automation-improvement-recurrence:" in c["body"]
        ]
        assert len(recurrence) == (0 if comment_fails else 2)
        assert candidate["key"] != "known-problem"
        launched.append(candidate["key"])
        root.mkdir(parents=True)
        write_json(
            root / "result.json",
            {
                "outcome": {"topic": candidate, "status": "reviewed", "resources": []},
                "budget": {"reported_inference_cost_usd": 0},
            },
        )
        return {
            "root": root,
            "topic": candidate,
            "started": time.monotonic(),
            "process": SimpleNamespace(pid=123, poll=lambda: 0),
        }

    monkeypatch.setattr(parallel, "launch", launch)
    config = {
        "repository": "Example/source",
        "checkout": str(tmp_path),
        "project_id": "00000000-0000-0000-0000-000000000001",
        "signal_id": SIGNAL,
        "input": str(frame),
        "max_topics_per_run": 2,
        "retry_topic_ids": [topic_id("shared", "known-problem")],
    }
    summary = weekly.run_locked(
        config, tmp_path, dry_run=False, limit=3, seed=1, budget=RunBudget({})
    )
    assert launched == ["new-two", "new-three"]
    assert [item["body"] for item in github.issues[:2]] == original
    assert "PRIVATE CUSTOMER CONTENT" not in json.dumps(github.issues + github.comments)
    assert "comment_existing_topic" in captured["prompt"]
    assert "They MUST NOT receive another repair agent" in captured["prompt"]
    state = json.loads((tmp_path / "state.json").read_text())
    identifier = rows[0]["event"]["id"]
    assert (identifier in state["events"]) is not comment_fails
    assert (identifier in state["backlog"]) is comment_fails
    outcome = next(
        item for item in summary["topics"] if item["topic"]["key"] == "known-problem"
    )
    assert outcome["status"] == (
        "recurrence_pending" if comment_fails else "recurrence_recorded"
    )
    assert any(
        item["status"] == "deferred_topic_limit" and item["topic"]["key"] == "new-four"
        for item in summary["topics"]
    )
    recurrence = [
        c for c in github.comments if "automation-improvement-recurrence:" in c["body"]
    ]
    assert len(recurrence) == (0 if comment_fails else 2)
    for item in recurrence:
        assert (
            identifier in item["body"] and "Private evidence reference:" in item["body"]
        )


def test_discovery_includes_open_human_work_and_closed_improvements_not_weekly_briefs(
    monkeypatch,
):
    calls = []

    def pages(endpoint):
        calls.append(endpoint)
        return (
            [
                {
                    "html_url": "https://github.com/Example/source/issues/1",
                    "title": "Human issue",
                    "body": "Problem",
                    "state": "open",
                    "created_at": "2026-10-25",
                },
                {
                    "html_url": "https://github.com/Example/source/issues/2",
                    "title": "Weekly",
                    "body": "<!-- automation-improvement-triage: week/run -->",
                    "state": "open",
                    "created_at": "2026-10-25",
                },
                {
                    "html_url": "https://github.com/Example/source/issues/4",
                    "title": "Human report quoting a weekly marker",
                    "body": "Bug in weekly reconciliation:\n`<!-- automation-improvement-triage: week/run -->`",
                    "state": "open",
                    "created_at": "2026-10-25",
                },
            ]
            if "state=open" in endpoint
            else [
                {
                    "html_url": "https://github.com/Example/source/pull/3",
                    "title": "Old repair",
                    "state": "closed",
                    "created_at": "2026-10-25",
                    "pull_request": {},
                },
            ]
        )

    monkeypatch.setattr(weekly, "github_pages", pages)
    found = weekly.existing_work("Example/source")
    assert [item["title"] for item in found] == [
        "Human issue",
        "Human report quoting a weekly marker",
        "Old repair",
    ]
    assert "labels=automation-improvement" in calls[1]


def test_discovery_failure_does_not_dispatch_repairs(tmp_path, monkeypatch):
    frame = tmp_path / "inventory.json"
    write_json(frame, inventory([record()]))
    monkeypatch.setattr(weekly, "github_auth", lambda _: None)
    monkeypatch.setattr(weekly, "repository_catalog", lambda _: {"Example/source": {}})
    monkeypatch.setattr(
        weekly,
        "existing_work",
        lambda _: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        weekly,
        "triage",
        lambda *a: pytest.fail("Must not triage without existing-work discovery"),
    )
    config = {
        "repository": "Example/source",
        "checkout": str(tmp_path),
        "project_id": "00000000-0000-0000-0000-000000000001",
        "signal_id": SIGNAL,
        "input": str(frame),
    }
    summary = weekly.run_locked(
        config, tmp_path, dry_run=False, limit=3, seed=1, budget=RunBudget({})
    )
    assert (
        summary["status"] == "discovery_incomplete" and summary["pending_events"] == 1
    )
