import stat
from pathlib import Path


import triage_workflow
from common import read_json


def test_triage_workflow_stops_at_private_ordered_handoff(prepared, monkeypatch):
    import sys

    config, queue = prepared
    monkeypatch.setitem(sys.modules, "repair_workflow", None)
    monkeypatch.setitem(sys.modules, "parallel", None)
    result = triage_workflow.run(config, seed=42)
    assert result["status"] == "ready_for_repair"
    path = Path(result["repair_request"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    request = read_json(path)
    assert request["queue"] == queue
    assert request["config"] == config
    assert request["context"]["seed"] == 42
    assert request["budget"]["reported_inference_cost_usd"] == 0.1
    state = read_json(Path(config["state_dir"]) / "state.json")
    assert not state["topic_weeks"]
    assert len(state["backlog"]) == 3
    assert not (path.parent / "workers").exists()
    assert not (path.parent / "weekly-report.json").exists()


def test_triage_preview_does_not_publish_a_repair_handoff(prepared):
    config, _ = prepared
    result = triage_workflow.run(config, dry_run=True)
    assert result["status"] == "preview"
    assert "repair_request" not in result
    assert not (Path(result["output"]) / "repair-request.json").exists()
    assert not (Path(config["state_dir"]) / "state.json").exists()
