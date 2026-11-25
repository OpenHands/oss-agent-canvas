import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

import collect_signals
from common import write_json
from test_support import frozen
from test_weekly import record


def test_collect_cli_reports_latest_attempts_and_writes_private_inventory(
    tmp_path, monkeypatch, capsys
):
    frame = frozen()
    matched = record()
    wrong_cause = record(2)
    payload = json.loads(wrong_cause["event"]["payload"])
    wrong_cause["event"]["payload"] = json.dumps(payload | {"cause": "provider"})
    finding = matched["run"]
    other = wrong_cause["run"]
    private_text = "private finding title"
    matched["event"]["payload"] = json.dumps(payload | {"summary": private_text})
    clean = finding | {
        "trace_id": str(UUID(int=901)),
        "run_id": str(UUID(int=902)),
        "event_id": None,
    }
    failed = clean | {
        "run_id": str(UUID(int=903)),
        "status": "FAILED",
        "updated_at": "2026-10-23T00:00:00Z",
    }
    empty = clean | {
        "trace_id": str(UUID(int=904)),
        "run_id": str(UUID(int=905)),
        "input_tokens": 0,
        "output_tokens": 0,
    }
    queries = []

    def request(client, url, body=None):
        assert client.key == "private-laminar-key"
        if url.endswith("/versions"):
            return {"versions": []}
        if "/signals/" in url:
            return frame["flow1_signal"]
        if "/llm-profiles/" in url:
            return frame["flow1_profile"]
        assert url.endswith("/sql/query")
        sql = body["query"]
        queries.append(sql)
        if "FROM signal_runs" in sql:
            return {"data": [finding, other, clean, failed, empty]}
        if "FROM signal_events" in sql:
            return {"data": [matched["event"], wrong_cause["event"]]}
        assert "FROM spans" in sql and "automation.conversation" in sql
        return {"data": []}

    monkeypatch.setattr(collect_signals.Laminar, "request", request)
    config_path = tmp_path / "config.json"
    credentials = tmp_path / "credentials.json"
    write_json(
        credentials,
        {"LMNR_PROJECT_API_KEY": "private-laminar-key", "LITELLM_API_KEY": "model-key"},
    )
    for key in ("LMNR_PROJECT_API_KEY", "LMNR_BASE_URL", "LMNR_FORCE_HTTP"):
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    config = {
        "project_id": frame["project_id"],
        "signal_id": frame["flow1_signal"]["id"],
        "laminar_base_url": "https://example.test",
        "credentials_file": str(credentials),
        "since": "2026-10-22T00:00:00Z",
        "until": "2026-10-25T00:00:00Z",
        "trace_filter": {"time_basis": "trace_started", "causes": ["workflow"]},
    }
    write_json(config_path, config)
    output = tmp_path / "debug"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect_signals.py",
            "--config",
            str(config_path),
            "--output",
            str(output),
            "--since",
            "2026-10-21T00:00:00Z",
        ],
    )
    assert collect_signals.main() == 0
    assert "LITELLM_API_KEY" not in os.environ
    receipt = capsys.readouterr().out
    summary = json.loads(receipt)
    assert summary["window"]["field"] == "traces.start_time"
    assert summary["window"]["since"] == "2026-10-21T00:00:00+00:00"
    assert summary["filter_value_counts"] == {"causes": 1}
    assert summary["signal_attempts"] == 5
    assert summary["latest_by_trace"] == {
        "traces": 4,
        "statuses": {"COMPLETED": 3, "FAILED": 1},
        "completed_without_event": 1,
        "completed_without_event_or_recorded_usage": 1,
    }
    assert summary["candidate_events"] == 2 and summary["excluded_by_payload"] == 1
    assert summary["matched_findings"] == 1 and summary["attribution"] == {"missing": 1}
    assert summary["collection_errors"] == 0 and summary["status"] == "collected"
    inventory = Path(summary["inventory_path"])
    assert json.loads(inventory.read_text())["explicit_window"] is True
    assert list(output.iterdir()) == [inventory] and inventory.name == "input.json"
    assert output.stat().st_mode & 0o077 == inventory.stat().st_mode & 0o077 == 0
    assert "private-laminar-key" not in receipt + inventory.read_text()
    assert private_text not in receipt and private_text in inventory.read_text()
    assert "FROM traces WHERE" in queries[0] and "2026-10-21" in queries[0]


def test_collector_rejects_nonempty_directory_and_portable_source(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        collect_signals, "Laminar", lambda _: pytest.fail("Laminar read")
    )
    monkeypatch.setenv("LMNR_BASE_URL", "")
    monkeypatch.setenv("LMNR_FORCE_HTTP", "")
    frame = frozen()
    config = {
        "project_id": frame["project_id"],
        "signal_id": frame["flow1_signal"]["id"],
        "laminar_base_url": "https://example.test",
    }
    write_json(tmp_path / "existing.json", {})
    with pytest.raises(FileExistsError, match="empty output directory"):
        collect_signals.collect(config, tmp_path)
    assert list(tmp_path.iterdir()) == [tmp_path / "existing.json"]
    with pytest.raises(ValueError, match="Laminar finding source"):
        collect_signals.collect({"finding_source": "portable"}, tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


def test_debug_cli_is_only_in_discovery_packages(tmp_path):
    root = Path(__file__).resolve().parents[1]
    package = runpy.run_path(str(root / "package_workflow.py"))["package"]
    tarball = package("discovery", tmp_path, without_sdk=True) / "tarball"
    result = subprocess.run(
        [sys.executable, "-E", "-S", "-B", "collect_signals.py", "--help"],
        cwd=tarball,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--output" in result.stdout
    for stage in ("native-repair", "factory-handoff"):
        assert not (package(stage, tmp_path) / "tarball/collect_signals.py").exists()
