from pathlib import Path

import pytest

import triage_workflow
from common import read_json, replace_json
from test_weekly import inventory, record

WINDOW = {"since": "2026-10-20T12:00:00Z", "until": "2026-10-21T12:00:00Z"}


def test_scoped_replay_checks_source_filter_and_window(prepared):
    config, _ = prepared
    capture = inventory([])
    filters = {"outcomes": ["unresolved"]}
    capture.update(trace_filter=filters, window=WINDOW)
    config.update(project_id=capture["project_id"], trace_filter=filters)
    args = triage_workflow.arguments(config, None, 1)
    scope = triage_workflow.collection_scope(config, args, capture)
    assert scope == {
        "trace_filter": filters,
        "since": "2026-10-20T12:00:00+00:00",
        "until": "2026-10-21T12:00:00+00:00",
    }
    assert (
        triage_workflow.collection_scope(
            config | {"since": "2026-10-20T14:00:00+02:00"}, args, capture
        )
        == scope
    )
    for changed, error in (
        ({"trace_filter": {}}, "different trace filter"),
        ({"project_id": args.signal_id}, "another project or Signal"),
        ({"flow1_signal": {"id": args.project_id}}, "another project or Signal"),
    ):
        with pytest.raises(ValueError, match=error):
            triage_workflow.collection_scope(config, args, capture | changed)
    with pytest.raises(ValueError, match="different collection window"):
        triage_workflow.collection_scope(
            config | {"until": "2026-10-22T12:00:00Z"}, args, capture
        )


@pytest.mark.parametrize("use_trace_filter", [True, False])
def test_targeted_discovery_keeps_unrelated_backlog(
    prepared, monkeypatch, use_trace_filter
):
    config, _ = prepared
    current, unrelated = record(1), record(2)
    capture = inventory([current])
    capture["window"] = WINDOW
    config.update(project_id=capture["project_id"], **WINDOW)
    if use_trace_filter:
        capture["trace_filter"] = {"trace_ids": [current["run"]["trace_id"]]}
        config.update(trace_filter=capture["trace_filter"])
    replace_json(Path(config["input"]), capture)
    state_path = Path(config["state_dir"]) / "state.json"
    unrelated_id = unrelated["event"]["id"]
    replace_json(state_path, {"events": {}, "backlog": {unrelated_id: unrelated}})
    monkeypatch.setattr(triage_workflow, "existing_work", lambda _: [])
    ranked = []

    def rank(args, records, prs, previous):
        ranked.append([r["event"]["id"] for r in records])
        return [], {}

    monkeypatch.setattr(triage_workflow, "triage", rank)
    assert triage_workflow.run(config, dry_run=True)["status"] == "preview"
    assert ranked == [[current["event"]["id"]]]
    assert set(read_json(state_path)["backlog"]) == {unrelated_id}
    capture["trace_filter"] = {}
    capture["explicit_window"] = True
    replace_json(Path(config["input"]), capture)
    for field in ("since", "until", "trace_filter"):
        config.pop(field, None)
    triage_workflow.run(config, dry_run=True)
    assert ranked[1] == [current["event"]["id"]]
    capture.pop("explicit_window")
    replace_json(Path(config["input"]), capture)
    triage_workflow.run(config, dry_run=True)
    assert set(ranked[2]) == {current["event"]["id"], unrelated_id}


def test_filtered_retry_defers_another_window_then_resumes(prepared, monkeypatch):
    config, _ = prepared
    current = record()
    capture = inventory([current])
    capture.update(
        window=WINDOW, trace_filter={"trace_ids": [current["run"]["trace_id"]]}
    )
    config["project_id"] = capture["project_id"]
    replace_json(Path(config["input"]), capture)
    monkeypatch.setattr(triage_workflow, "existing_work", lambda _: [])
    calls = []

    def reconcile(*args):
        calls.append("publish")
        if len(calls) == 1:
            raise RuntimeError("Temporary outage")

    monkeypatch.setattr(triage_workflow, "reconcile_publication", reconcile)
    state_path = Path(config["state_dir"]) / "state.json"
    assert triage_workflow.run(config)["status"] == "triage_incomplete"
    history = state_path.with_name("signal-history.sqlite3")
    before = (state_path.read_bytes(), history.read_bytes())
    capture["window"] = WINDOW | {"since": "2026-10-20T15:00:00Z"}
    capture["latest_runs"] = [current["run"] | {"status": "FAILED"}]
    replace_json(Path(config["input"]), capture)

    def unexpected(*args):
        pytest.fail("Unexpected side effect while deferring a saved publication")

    monkeypatch.setattr(triage_workflow, "triage", unexpected)
    with monkeypatch.context() as patch:
        patch.setattr(triage_workflow, "refresh_epic", unexpected)
        deferred = triage_workflow.run(config)
    assert deferred["status"] == "discovery_incomplete"
    assert deferred["publication"] == {"status": "deferred"}
    assert (state_path.read_bytes(), history.read_bytes()) == before
    assert calls == ["publish"]
    capture["window"] = WINDOW
    capture["latest_runs"] = [current["run"]]
    replace_json(Path(config["input"]), capture)
    assert triage_workflow.run(config)["status"] == "published"
    assert not read_json(state_path)["pending_discovery"]
    assert calls == ["publish", "publish"]


@pytest.fixture
def discover_snapshots(prepared, monkeypatch):
    config, _ = prepared
    monkeypatch.setattr(triage_workflow, "existing_work", lambda _: [])
    monkeypatch.setattr(triage_workflow, "triage", lambda *a: ([], {}))
    monkeypatch.setattr(triage_workflow, "reconcile_publication", lambda *a: None)

    def discover(rows, latest, *, dry_run=False):
        capture = inventory(rows)
        capture.update(latest_runs=latest, window=WINDOW, explicit_window=True)
        replace_json(Path(config["input"]), capture)
        return triage_workflow.run(config, dry_run=dry_run)

    return Path(config["state_dir"]), discover


@pytest.mark.parametrize("newer", ["finding", "failed", "empty", "filtered"])
def test_older_window_cannot_restore_superseded_findings(discover_snapshots, newer):
    state_dir, discover = discover_snapshots
    old, updated = record(1), record(2)
    trace = old["run"]["trace_id"]
    updated["run"]["trace_id"] = updated["event"]["trace_id"] = trace
    updated["run"]["updated_at"] = "2026-10-22T00:10:00Z"
    state_path = state_dir / "state.json"
    other = record(3)
    other["definition"]["prompt"] = "Independent Signal definition"
    other["run"]["trace_id"] = other["event"]["trace_id"] = trace
    unaffected = {
        other["event"]["id"]: other,
        "portable": {"occurrence_id": "portable"},
    }
    replace_json(
        state_path, {"events": {}, "backlog": unaffected | {old["event"]["id"]: old}}
    )
    run = updated["run"].copy()
    if newer == "failed":
        run["status"] = "FAILED"
    elif newer == "empty":
        run["event_id"] = None
    rows = [updated] if newer == "finding" else []
    discover(rows, [run])
    expected = unaffected | {r["event"]["id"]: r for r in rows}
    assert read_json(state_path)["backlog"] == expected
    assert discover([old], [old["run"]])["status"] == "no_unhandled_findings"
    assert read_json(state_path)["backlog"] == expected
    if newer == "filtered":
        assert discover([updated], [run])["status"] == "published"
        assert read_json(state_path)["backlog"][updated["event"]["id"]] == updated


def test_legacy_pending_run_wins_timestamp_tie_and_dry_runs_leave_history_unchanged(
    discover_snapshots,
):
    state_dir, discover = discover_snapshots
    old, winning = record(1), record(2)
    trace = old["run"]["trace_id"]
    winning["run"]["trace_id"] = winning["event"]["trace_id"] = trace
    state_path = state_dir / "state.json"
    history = state_dir / "signal-history.sqlite3"
    pending = {winning["event"]["id"]: winning}
    replace_json(state_path, {"events": {}, "backlog": pending})
    assert (
        discover([old], [old["run"]], dry_run=True)["status"] == "no_unhandled_findings"
    )
    assert not history.exists()
    assert discover([old], [old["run"]])["status"] == "no_unhandled_findings"
    assert read_json(state_path)["backlog"] == pending
    assert history.stat().st_mode & 0o077 == 0
    before = (state_path.read_bytes(), history.read_bytes())
    revoked = winning["run"] | {"event_id": None}
    assert discover([], [revoked], dry_run=True)["status"] == "no_unhandled_findings"
    assert (state_path.read_bytes(), history.read_bytes()) == before
    assert discover([winning], [winning["run"]], dry_run=True)["status"] == "preview"
    discover([], [revoked])
    assert discover([winning], [winning["run"]])["status"] == "no_unhandled_findings"
    assert not read_json(state_path)["backlog"]


def test_retry_after_state_save_failure_preserves_latest_result(
    discover_snapshots, monkeypatch
):
    state_dir, discover = discover_snapshots
    old, replacement = record(1), record(2)
    trace = old["run"]["trace_id"]
    replacement["run"]["trace_id"] = replacement["event"]["trace_id"] = trace
    discover([old], [old["run"]])
    state_path = state_dir / "state.json"
    original = state_path.read_bytes()

    def fail(*args):
        raise OSError("Disk full")

    with monkeypatch.context() as patch:
        patch.setattr(triage_workflow, "save_state", fail)
        with pytest.raises(OSError, match="Disk full"):
            discover([replacement], [replacement["run"]])
    assert state_path.read_bytes() == original
    assert discover([old], [old["run"]])["status"] == "no_unhandled_findings"
    assert discover([replacement], [replacement["run"]])["status"] == "published"
    assert read_json(state_path)["backlog"] == {replacement["event"]["id"]: replacement}


def test_same_run_completes_then_conflicting_terminal_records_are_withheld(
    discover_snapshots,
):
    state_dir, discover = discover_snapshots
    row = record()
    run = row["run"]
    pending = run | {"status": "PENDING", "event_id": None}
    discover([], [pending])
    assert discover([row], [run])["status"] == "published"
    discover([], [pending])
    state_path = state_dir / "state.json"
    assert read_json(state_path)["backlog"] == {row["event"]["id"]: row}
    conflicting = run | {"event_id": record(2)["event"]["id"]}
    discover([], [conflicting])
    assert discover([row], [run])["status"] == "no_unhandled_findings"
    assert not read_json(state_path)["backlog"]
