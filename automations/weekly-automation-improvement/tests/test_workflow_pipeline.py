import json
import time
from types import SimpleNamespace

import pytest

from budget import RunBudget
from common import topic_id, write_json
from test_support import install_sdk
from test_topic_issue import GitHub
from test_triage import topic
from test_weekly import inventory, record, SIGNAL
from test_tracked_work import work
import parallel
import topic_issue
import tracked_work
import triage_agent
import triage_workflow as weekly
import weekly as pipeline


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
    summary = pipeline.run_locked(
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


@pytest.mark.parametrize("separate", [False, True])
def test_workflows_keep_queue_limits_accounting_and_replay_safety(
    prepared, monkeypatch, separate
):
    from pathlib import Path
    import repair_workflow
    from common import read_json

    config, queue = prepared
    monkeypatch.setattr(repair_workflow, "github_auth", lambda _: None)
    launches = []

    def launch(cfg, context, candidate, budget, root):
        assert cfg == config
        assert budget.cost == pytest.approx(0.1)
        assert context["seed"] == 42
        launches.append(candidate["id"])
        root.mkdir(parents=True)
        write_json(
            root / "result.json",
            {
                "outcome": {"topic": candidate, "status": "reviewed", "resources": []},
                "budget": {"reported_inference_cost_usd": 0.2},
            },
        )
        return {
            "root": root,
            "topic": candidate,
            "started": time.monotonic(),
            "process": SimpleNamespace(pid=123, poll=lambda: 0),
        }

    monkeypatch.setattr(parallel, "launch", launch)
    if separate:
        triaged = weekly.run(config, seed=42)
        result = repair_workflow.run(triaged["repair_request"])
    else:
        result = pipeline.run(config, seed=42)
    assert launches == ["0", "1"]
    assert result["status"] == "completed"
    assert result["budget"]["reported_inference_cost_usd"] == pytest.approx(0.5)
    assert result["budget"]["requests"] == 3
    assert [
        item["topic"]["id"]
        for item in result["topics"]
        if item["status"] == "deferred_topic_limit"
    ] == ["2"]
    request = result["repair_request"]
    assert repair_workflow.run(request) == result
    assert launches == ["0", "1"]
    state = read_json(Path(config["state_dir"]) / "state.json")
    assert len(state["topic_weeks"]) == 2


def test_repair_handoff_rechecks_dispatch_ledger(prepared, monkeypatch):
    from pathlib import Path
    import repair_workflow
    from common import read_json, replace_json

    config, queue = prepared
    monkeypatch.setattr(repair_workflow, "github_auth", lambda _: None)
    triaged = weekly.run(config)
    path = Path(config["state_dir"]) / "state.json"
    state = read_json(path)
    for item in queue:
        state["topic_weeks"][triaged["week"] + "/" + item["id"]] = {
            "topic": item,
            "status": "dispatched",
        }
    replace_json(path, state)
    monkeypatch.setattr(
        parallel, "launch", lambda *a: pytest.fail("duplicate dispatch")
    )
    result = repair_workflow.run(triaged["repair_request"])
    assert all(item["status"] == "already_attempted" for item in result["topics"])


def test_separate_repair_preserves_exhausted_triage_budget(prepared, monkeypatch):
    import repair_workflow

    config, _ = prepared
    config["max_inference_cost_usd"] = 0.1
    monkeypatch.setattr(repair_workflow, "github_auth", lambda _: None)
    triaged = weekly.run(config)
    monkeypatch.setattr(parallel, "launch", lambda *a: pytest.fail("budget reset"))
    result = repair_workflow.run(triaged["repair_request"])
    assert result["status"] == "budget_exhausted"
    assert result["budget"]["reported_inference_cost_usd"] == 0.1


def test_repair_uses_same_lock_as_triage(prepared):
    import fcntl
    import os
    from pathlib import Path
    import repair_workflow

    config, _ = prepared
    triaged = weekly.run(config)
    descriptor = os.open(Path(config["state_dir"]) / "weekly.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert repair_workflow.run(triaged["repair_request"]) == {
            "status": "skipped_overlap"
        }
    finally:
        os.close(descriptor)
