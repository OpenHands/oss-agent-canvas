import json
import uuid
from types import SimpleNamespace

import pytest

from events import collect_events
from test_support import arguments, frozen
from test_weekly import record
from trace_filter import normalize_trace_filter


def test_normalization_accepts_only_bounded_typed_selectors():
    identifier = "aaaaaaaa-bbaa-4321-8abc-000000000002"
    assert normalize_trace_filter(
        {
            "trace_ids": [identifier.upper(), identifier],
            "causes": ["workflow", "provider"],
            "time_basis": "signal_updated",
        }
    ) == {"trace_ids": [identifier], "causes": ["provider", "workflow"]}
    assert normalize_trace_filter({}) == {}


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"where": "1=1"},
        {"time_basis": "start_time"},
        {"automation_ids": []},
        {"automation_run_ids": ["NULL'"]},
        {"trace_ids": [str(uuid.UUID(int=1))] * 101},
        {"conversation_ids": [None]},
        {"causes": ["test"]},
        {"outcomes": "unknown"},
    ],
)
def test_invalid_filter_fails_before_any_request(value):
    with pytest.raises(ValueError, match="trace_filter"):
        collect_events(
            SimpleNamespace(args=SimpleNamespace(trace_filter=value)),
            set(),
            aggregate=True,
        )


class InventoryClient:
    def __init__(self, tmp_path, filters, rows):
        self.base = "https://example.test"
        self.frame = frozen()
        self.args = arguments(
            tmp_path,
            signal_id=self.frame["flow1_signal"]["id"],
            trace_filter=filters,
            since="2026-10-22T02:00:00.000001+02:00",
            until="2026-10-22T01:00:00.000002Z",
        )
        self.rows = rows
        self.events = [item["event"] for item in rows]
        self.owners = [item["owner"] for item in rows]
        self.measurements = []
        self.sql = []

    def request(self, url):
        if url.endswith("/versions"):
            return {"versions": []}
        return (
            self.frame["flow1_signal"]
            if "/signals/" in url
            else self.frame["flow1_profile"]
        )

    def query(self, sql):
        self.sql.append(sql)
        if "FROM signal_runs" in sql:
            return [item["run"] for item in self.rows]
        if "FROM signal_events" in sql:
            return [item for item in self.events if item["id"] in sql]
        return [item for item in self.owners if item["trace_id"] in sql]


@pytest.mark.parametrize("limit", [0, -1, "10", 1.5, True, None])
def test_invalid_population_limit_fails_before_any_request(limit):
    with pytest.raises(ValueError, match="population_limit must be a positive integer"):
        collect_events(
            SimpleNamespace(args=SimpleNamespace(population_limit=limit)),
            set(),
            aggregate=True,
        )


@pytest.mark.parametrize("time_basis", ["trace_started", "signal_updated"])
def test_live_query_scopes_traces_before_cap_and_keeps_all_origin_candidates(
    tmp_path, time_basis
):
    row = record()
    owner = row["owner"]
    filters = {
        "trace_ids": [row["event"]["trace_id"]],
        "automation_ids": [owner["automation_id"]],
        "automation_run_ids": [owner["run_id"]],
        "conversation_ids": [owner["conversation_id"]],
        "time_basis": time_basis,
    }
    client = InventoryClient(tmp_path, filters, [row])
    client.owners.append(owner | {"automation_id": str(uuid.UUID(int=999))})
    result = collect_events(client, set(), aggregate=True)
    sql = client.sql[0].split("ORDER BY")[0]
    assert "trace_id IN ('" + row["event"]["trace_id"] + "')" in sql
    assert sql.count("SELECT trace_id FROM spans") == 1
    origin = sql.split("SELECT trace_id FROM spans")[1]
    assert "name='automation.conversation'" in origin
    assert "trace_id IN ('" + row["event"]["trace_id"] + "')" in origin
    for key in ("automation_id", "conversation_id", "run_id"):
        assert owner[key] in origin
    if time_basis == "trace_started":
        assert "SELECT id FROM traces WHERE start_time >=" in origin
        assert "AND id IN ('" + row["event"]["trace_id"] + "')" in origin
        assert "AND start_time < " in sql and "updated_at >=" not in sql
    else:
        assert "updated_at >=" in sql and "AND updated_at < " in sql
    assert "2026-10-22 00:00:00.000001" in sql
    assert "2026-10-22 01:00:00.000002" in sql
    assert result["trace_filter"] == normalize_trace_filter(filters)
    assert result["events"][0]["attribution_status"] == "ambiguous"
    assert len(result["events"][0]["owner_candidates"]) == 2


@pytest.mark.parametrize("basis", ["trace_started", "signal_updated"])
def test_owner_only_scope_reads_spans_from_the_selected_window(tmp_path, basis):
    row = record()
    filters = {"automation_ids": [row["owner"]["automation_id"]], "time_basis": basis}
    client = InventoryClient(tmp_path, filters, [row])
    collect_events(client, set(), aggregate=True)
    sql = client.sql[0]
    owner_query = sql.split("SELECT trace_id FROM spans WHERE ")[1]
    assert sql.count("LIMIT") == 1
    if basis == "trace_started":
        assert "trace_id IN (SELECT id FROM traces WHERE start_time >=" in owner_query
        assert " AND start_time <" in owner_query
        assert "updated_at >=" not in sql
    else:
        signal = row["event"]["signal_id"]
        assert (
            f"trace_id IN (SELECT trace_id FROM signal_runs WHERE signal_id='{signal}' "
            "AND signal_version IN (1) AND updated_at >=" in owner_query
        )
        assert " AND updated_at < " in owner_query
        assert "start_time" not in sql


def test_payload_filters_only_emitted_findings_and_keeps_attempts_and_controls(
    tmp_path,
):
    first, excluded, superseded, control, unreadable = [record(i) for i in range(1, 6)]
    excluded["event"]["payload"] = json.loads(excluded["event"]["payload"]) | {
        "cause": "provider"
    }
    failed = {
        "owner": superseded["owner"],
        "event": superseded["event"],
        "run": superseded["run"]
        | {
            "run_id": str(uuid.UUID(int=911)),
            "status": "FAILED",
            "updated_at": "2026-10-22T00:30:00Z",
        },
    }
    control["run"]["event_id"] = None
    unreadable["event"]["payload"] = {}
    rows = [first, excluded, superseded, failed, control, unreadable]
    client = InventoryClient(
        tmp_path, {"causes": ["workflow"], "outcomes": ["unresolved"]}, rows
    )
    client.owners.remove(first["owner"])
    result = collect_events(client, set(), aggregate=True)
    selection = result["selection"]
    assert selection["candidate_event_count"] == 3
    assert selection["payload_excluded_count"] == 1
    assert selection["unreadable_event_count"] == 1
    assert selection["population_size"] == selection["sample_size"] == 1
    assert selection["selected_event_ids"] == [first["event"]["id"]]
    assert result["events"][0]["attribution_status"] == "missing"
    assert result["signal_run_summary"]["attempts"] == 6
    assert result["signal_run_summary"]["trace_count"] == 5
    assert result["signal_run_summary"]["completed_no_event"] == 1
    assert result["signal_run_summary"]["statuses"] == {"COMPLETED": 5, "FAILED": 1}
    assert {r["run_id"] for r in result["latest_runs"]} == {
        item["run"]["run_id"] for item in [first, excluded, failed, control, unreadable]
    }
    assert not any(superseded["event"]["id"] in sql for sql in client.sql)
