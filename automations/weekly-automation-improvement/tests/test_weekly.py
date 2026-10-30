import copy
import json
import uuid
from pathlib import Path
import pytest
from test_automation import arguments, command
from test_flow1 import EVENT, SIGNAL, frozen
import sources
import weekly
from events import collect_events
from flow1 import findings


class EventClient:
    def __init__(self, tmp_path, rows, owners=1):
        self.args = arguments(tmp_path, signal_id=SIGNAL)
        self.value = frozen()
        self.rows = rows
        self.owners = owners
        self.measurements = []

    def request(self, url):
        return (
            self.value["flow1_signal"]
            if "/signals/" in url
            else self.value["flow1_profile"]
        )

    base = "https://example.test"

    def query(self, sql):
        if "FROM signal_runs" in sql:
            return self.rows
        case = self.value["samples"][0]
        return [
            {
                key: case[key]
                for key in (
                    "conversation_id",
                    "automation_id",
                    "run_id",
                    "automation_name",
                )
            }
        ] * self.owners

    def load_sample(self, case):
        case.update(copy.deepcopy(self.value["samples"][0]))

    def load_flow1(self, case, definition):
        pass


def test_events_select_findings_and_report_failed_and_clean_runs(tmp_path):
    run = frozen()["samples"][0]["flow1_runs"][0]
    clean = run | {
        "run_id": str(uuid.UUID(int=90)),
        "trace_id": str(uuid.UUID(int=91)),
        "event_id": str(uuid.UUID(int=0)),
    }
    failed = clean | {
        "run_id": str(uuid.UUID(int=92)),
        "trace_id": str(uuid.UUID(int=93)),
        "status": "FAILED",
    }
    client = EventClient(tmp_path, [run, clean, failed])
    value = collect_events(client, set())
    assert value["selection"]["selected_event_ids"] == [EVENT]
    assert value["selection"]["unit"] == "unhandled_signal_event"
    assert value["signal_run_summary"]["completed_no_event"] == 1
    assert value["signal_run_summary"]["statuses"]["FAILED"] == 1


def test_handled_or_superseded_events_are_not_selected(tmp_path):
    run = frozen()["samples"][0]["flow1_runs"][0]
    newer_failure = run | {
        "run_id": str(uuid.UUID(int=90)),
        "status": "FAILED",
        "updated_at": "2026-10-23T00:00:00Z",
    }
    value = collect_events(EventClient(tmp_path, [run, newer_failure]), set())
    assert value["samples"] == []
    second = tmp_path / "second"
    second.mkdir()
    value = collect_events(EventClient(second, [run]), {EVENT})
    assert value["selection"]["population_size"] == 0


def test_ambiguous_ownership_is_recorded_without_replacement(tmp_path):
    run = frozen()["samples"][0]["flow1_runs"][0]
    value = collect_events(EventClient(tmp_path, [run], owners=2), set())
    assert value["selection"]["selected_event_ids"] == [EVENT]
    assert value["samples"] == []
    assert value["collection_errors"][0]["event_id"] == EVENT


def test_selected_event_does_not_require_analysis_of_unselected_context_trace():
    value = frozen()
    case = value["samples"][0]
    original = case["trace_ids"][0]
    other = str(uuid.UUID(int=80))
    case["flow1_required_trace_ids"] = [original]
    case["selected_event_ids"] = [EVENT]
    case["trace_ids"].append(other)
    case["spans"].append(
        case["spans"][0]
        | {
            "ref": "s2",
            "span_id": str(uuid.UUID(int=81)),
            "trace_id": other,
            "end_time": "2026-10-24T00:00:00Z",
        }
    )
    case["flow1_trace_sessions"].append(
        {"trace_id": other, "conversation_ids": [case["conversation_id"]]}
    )
    result, _ = findings(value, case)
    assert result["finding_event_ids"] == [EVENT]


def record(number=1, automation=1, conversation=None):
    value = frozen()
    case = value["samples"][0]
    event = copy.deepcopy(case["flow1_events"][0])
    event["id"] = str(uuid.UUID(int=100 + number))
    event["trace_id"] = str(uuid.UUID(int=200 + number))
    event["run_id"] = str(uuid.UUID(int=300 + number))
    event["severity"] = 1
    event["leaf_clusters"] = []
    payload = json.loads(event["payload"])
    payload["signature"] = "same problem"
    event["payload"] = json.dumps(payload)
    owner = {
        "automation_id": str(uuid.UUID(int=400 + automation)),
        "automation_name": "Automation " + str(automation),
        "conversation_id": str(uuid.UUID(int=500 + (conversation or number))),
        "run_id": str(uuid.UUID(int=600 + number)),
        "trace_id": event["trace_id"],
    }
    run = copy.deepcopy(case["flow1_runs"][0]) | {
        "event_id": event["id"],
        "trace_id": event["trace_id"],
        "run_id": event["run_id"],
    }
    return {
        "event": event,
        "owner": owner,
        "run": run,
        "definition": value["flow1_signal"],
    }


def inventory(records):
    value = frozen()
    value.update(
        events=copy.deepcopy(records),
        samples=[],
        latest_runs=[item["run"] for item in records],
        signal_run_summary={"attempts": len(records)},
        collection_errors=[],
    )
    value["selection"]["selected_event_ids"] = [item["event"]["id"] for item in records]
    return value


def test_backlog_survives_window_and_sampling_only_version_changes():
    old = record()
    old["definition"].update(prompt="same instruction", sampleRate=5)
    state = {"events": {}, "backlog": {old["event"]["id"]: old}}
    current = inventory([])
    current["flow1_signal"].update(prompt="same instruction", sampleRate=50, version=2)
    assert weekly.update_backlog(state, current, []) == [old]
    current["flow1_signal"]["prompt"] = "a different detector"
    assert weekly.update_backlog(state, current, []) == []
    assert len(state["backlog"]) == 1


def test_handled_and_superseded_findings_leave_backlog():
    old = record()
    identifier = old["event"]["id"]
    state = {
        "events": {identifier: {"status": "rejected"}},
        "backlog": {identifier: old},
    }
    assert weekly.update_backlog(state, inventory([old]), []) == []
    state["events"] = {}
    state["backlog"][identifier] = old
    current = inventory([])
    current["latest_runs"] = [old["run"] | {"event_id": str(uuid.UUID(int=0))}]
    assert weekly.update_backlog(state, current, []) == []
    assert not state["backlog"]


def test_repository_index_includes_plugins_without_symlinks(tmp_path):
    command(["git", "init", "-q"], tmp_path)
    command(["git", "config", "user.name", "Test"], tmp_path)
    command(["git", "config", "user.email", "test@example.test"], tmp_path)
    directory = tmp_path / "plugins/reviewer/scripts"
    directory.mkdir(parents=True)
    (directory / "worker.py").write_text("print(42)\n")
    (directory / "linked.py").symlink_to("worker.py")
    command(["git", "add", "."], tmp_path)
    command(["git", "commit", "-qm", "plugin"], tmp_path)
    index = sources.repository_index(tmp_path, "HEAD")
    assert set(index["plugins/reviewer"]) == {"scripts/worker.py"}


@pytest.mark.parametrize(
    "previous,rate,patch_count",
    [(5, 50, 1), (50, 50, 0), (50, None, 1), (None, None, 0)],
)
def test_configure_signal_rate_only_and_idempotency(previous, rate, patch_count):
    from configure_signal import configure_rate

    definition = frozen()["flow1_signal"] | {"sampleRate": previous, "disabled": False}

    class Client:
        base = "https://example.test"
        patches = []

        def request(self, url, body=None, method=None):
            assert url == self.base + "/v1/signals/" + SIGNAL
            if body is not None:
                assert method == "PATCH" and body == {"sampleRate": rate}
                self.patches.append(body)
                definition.update(body)
                definition["version"] += 1
            return copy.deepcopy(definition)

    client = Client()
    result = configure_rate(client, definition["projectId"], SIGNAL, rate)
    assert result["sample_rate"] == rate and result["disabled"] is False
    assert len(client.patches) == patch_count


def test_configure_signal_rejects_invalid_percentage_without_network():
    from configure_signal import configure_rate

    for rate in (-1, 0, 96, 100, 101, 0.5, True):
        with pytest.raises(ValueError, match="integer percentage"):
            configure_rate(None, "", SIGNAL, rate)


def test_failed_reanalysis_does_not_reuse_older_finding():
    old = record()
    identifier = old["event"]["id"]
    state = {"events": {}, "backlog": {identifier: old}}
    current = inventory([])
    current["latest_runs"] = [old["run"] | {"status": "FAILED"}]
    assert weekly.update_backlog(state, current, []) == []
    assert not state["backlog"]


def test_laminar_sdk_uses_reachable_http_transport(monkeypatch):
    monkeypatch.delenv("LMNR_FORCE_HTTP", raising=False)
    weekly.load_environment(
        {
            "laminar_base_url": "https://example.test",
            "llm_base_url": "https://proxy.test",
        }
    )
    import os

    assert os.environ["LMNR_FORCE_HTTP"] == "true"


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_topics_per_run", 0),
        ("max_topics_per_run", True),
        ("max_topics_per_run", "10"),
        ("repair_concurrency", -1),
        ("validation_sample_size", False),
        ("review_agent_profile", "not/a/profile"),
        ("max_prs_per_run", 10),
    ],
)
def test_invalid_top_level_config_fails_before_access(name, value, monkeypatch):
    monkeypatch.setattr(
        weekly, "load_environment", lambda c: pytest.fail("access before validation")
    )
    with pytest.raises(ValueError):
        weekly.run({name: value})


def test_same_week_attempts_are_not_replaced_and_explicit_retry_is_respected():
    topic = {"id": "one"}
    ledger = {"topic_weeks": {"2026-W44/one": {"topic": topic, "status": "failed"}}}
    queue, skipped = weekly.eligible_topics([topic], ledger, "2026-W44", [])
    assert queue == [] and skipped[0]["status"] == "already_attempted"
    assert weekly.eligible_topics([topic], ledger, "2026-W44", ["one"])[0] == [topic]
    assert weekly.eligible_topics([topic], ledger, "2026-W45", [])[0] == [topic]


def test_missing_sdk_checkout_is_context_not_a_repair_gate(tmp_path, monkeypatch):
    def revision(config):
        if config["repository"].endswith("software-agent-sdk"):
            raise FileNotFoundError("canonical checkout unavailable")
        return "a" * 40

    monkeypatch.setattr(weekly, "source_revision", revision)
    catalog = weekly.repository_catalog(
        {"repository": "Example/automation", "checkout": str(tmp_path / "automation")}
    )
    assert catalog["Example/automation"]["revision"] == "a" * 40
    assert "unavailable" in catalog["OpenHands/software-agent-sdk"]["error"]


def test_frozen_inventory_preview_never_launches_repair_or_changes_ledger(
    tmp_path, monkeypatch
):
    import parallel
    from budget import RunBudget
    from common import write_json

    rows = [record()]
    frame = tmp_path / "input.json"
    write_json(frame, inventory(rows))
    state_path = tmp_path / "state.json"
    state_path.write_text('{"events":{},"topic_weeks":{},"backlog":{}}')
    before = state_path.read_bytes()
    config = {
        "repository": "Example/automation",
        "checkout": str(tmp_path),
        "input": str(frame),
        "project_id": str(uuid.UUID(int=1)),
        "signal_id": SIGNAL,
    }
    monkeypatch.setattr(weekly, "github_auth", lambda c: None)
    monkeypatch.setattr(weekly, "repository_catalog", lambda c: {})
    monkeypatch.setattr(
        weekly,
        "collect_events",
        lambda *a, **k: pytest.fail("must use frozen inventory"),
    )
    monkeypatch.setattr(
        parallel,
        "execute",
        lambda *a, **k: pytest.fail("preview cannot launch repairs"),
    )

    def rank(args, records, prs, previous):
        assert args.dry_run and records == rows
        return [{"id": "one"}], {}

    monkeypatch.setattr(weekly, "triage", rank)
    result = weekly.run_locked(
        config, tmp_path, dry_run=True, limit=3, seed=42, budget=RunBudget({})
    )
    assert result["status"] == "preview"
    assert result["topics"][0]["status"] == "planned"
    assert state_path.read_bytes() == before


def test_topic_preparation_retains_cross_automation_inputs_without_source_gate(
    tmp_path, monkeypatch
):
    from common import write_json

    rows = [record(1), record(2, automation=2)]
    frame = tmp_path / "inventory.json"
    write_json(frame, inventory(rows))
    topic = {"representative_event_ids": [r["event"]["id"] for r in rows]}
    context = {
        "inventory_path": str(frame),
        "records": rows,
        "limit": 3,
        "seed": 42,
        "aggregation": {
            "triage_issue": {"url": "https://github.com/Example/a/issues/1"}
        },
        "state_dir": str(tmp_path),
        "week": "2026-W44",
        "repositories": {},
        "previous": [],
    }
    monkeypatch.setattr(weekly, "Laminar", lambda args: object())
    monkeypatch.setattr(
        weekly, "hydrate_events", lambda client, inv, chosen: {"samples": chosen}
    )

    def cohort(client, inv, owners, negative, size, seed):
        assert owners == sorted({r["owner"]["automation_id"] for r in rows})
        assert negative == rows and size == 7
        return {"samples": [], "complete": False, "errors": ["missing data"]}

    monkeypatch.setattr(weekly, "collect_cohort", cohort)
    result = weekly.prepare_topic(
        {
            "repository": "Example/a",
            "project_id": str(uuid.UUID(int=1)),
            "signal_id": SIGNAL,
            "validation_sample_size": 7,
        },
        context,
        topic,
        tmp_path / "topic",
    )
    assert len(result["automation_ids"]) == 2
    assert result["validation_sample_size"] == 7
    assert Path(result["snapshot_path"]).is_file()


def test_collection_can_write_its_own_immutable_inventory(tmp_path, monkeypatch):
    from budget import RunBudget
    from common import write_json

    config = {
        "repository": "Example/automation",
        "checkout": str(tmp_path),
        "project_id": str(uuid.UUID(int=1)),
        "signal_id": SIGNAL,
    }
    frame = inventory([])
    monkeypatch.setattr(weekly, "github_auth", lambda c: None)
    monkeypatch.setattr(weekly, "repository_catalog", lambda c: {})
    monkeypatch.setattr(weekly, "Laminar", lambda args: args)

    def collect(args, *a, **k):
        write_json(args.output / "input.json", frame)
        return frame

    monkeypatch.setattr(weekly, "collect_events", collect)
    result = weekly.run_locked(
        config, tmp_path, dry_run=True, limit=3, seed=42, budget=RunBudget({})
    )
    assert result["status"] == "no_unhandled_findings"
