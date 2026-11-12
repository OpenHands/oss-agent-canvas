import copy
import json
import uuid
from types import SimpleNamespace

import pytest

import cohorts
import topic_agent
import triage
import triage_agent
import weekly
from attribution import ownership
from common import write_json
from events import collect_cards, hydrate_events
from raw_evidence import EvidenceReader, population
from test_cohorts import fixture
from test_triage import topic
from test_weekly import inventory, record


def identifier(number):
    return str(uuid.UUID(int=number))


def uncertain(row, owners):
    return row | ownership(owners)


@pytest.mark.parametrize(
    "kind",
    ["missing", "ambiguous", "multiple_automations", "invalid", "duplicate_name"],
)
def test_collector_preserves_findings_despite_uncertain_ownership(kind):
    row = record()
    owners = [row["owner"]]
    if kind == "missing":
        owners = []
    elif kind == "invalid":
        owners = [row["owner"] | {"conversation_id": "invalid"}]
    elif kind in ("ambiguous", "multiple_automations"):
        owners.append(row["owner"] | {"conversation_id": identifier(999)})
        if kind == "multiple_automations":
            owners[-1]["automation_id"] = identifier(998)
    else:
        owners.append(row["owner"] | {"automation_name": "Renamed"})
    frame = inventory([row])
    frame["flow1_definitions"] = {
        str(row["event"]["signal_version"]): row["definition"]
    }
    client = SimpleNamespace(
        query=lambda sql: owners if "FROM spans" in sql else [row["event"]]
    )
    retained = collect_cards(client, frame, {row["event"]["id"]: row["run"]})
    assert len(retained) == 1 and not frame["collection_errors"]
    assert retained[0]["event"] == row["event"]
    assert retained[0]["run"] == row["run"]
    if kind == "duplicate_name":
        assert retained[0]["owner"] and retained[0]["attribution_status"] == "resolved"
        assert not frame.get("attribution_warnings")
    else:
        assert retained[0]["owner"] is None
        assert len(retained[0]["owner_candidates"]) == len(owners)
        assert frame["attribution_warnings"][0]["event_id"] == row["event"]["id"]
    state = {"events": {}, "backlog": {}}
    frame["events"] = retained
    assert weekly.update_backlog(state, frame, []) == retained
    cards, refs, prepared = triage.prepare(retained, [], [])
    assert len(prepared) == 1 and cards["total_events"] == 1
    plan = triage.validate(
        {"topics": [topic("e1")], "no_action_reason": ""}, retained, refs, 3
    )
    expected = [] if kind == "missing" else sorted({o["automation_id"] for o in owners})
    assert plan[0]["automation_ids"] == expected
    assert cards["affected_conversations"] == (1 if kind == "duplicate_name" else 0)
    assert cards["unresolved_attribution_events"] == (
        0 if kind == "duplicate_name" else 1
    )


def test_owner_lookup_overflow_retains_signal_as_incomplete_but_mismatched_event_does_not():
    row = record()
    owners = [row["owner"] | {"conversation_id": identifier(900 + i)} for i in range(4)]
    frame = inventory([row])
    frame["flow1_definitions"] = {
        str(row["event"]["signal_version"]): row["definition"]
    }
    client = SimpleNamespace(
        query=lambda sql: owners if "FROM spans" in sql else [row["event"]]
    )
    retained = collect_cards(client, frame, {row["event"]["id"]: row["run"]})
    assert len(retained) == 1 and retained[0]["attribution_status"] == "incomplete"
    changed = copy.deepcopy(row["event"]) | {"run_id": identifier(999)}
    client.query = lambda sql: owners if "FROM spans" in sql else [changed]
    assert not collect_cards(client, frame, {row["event"]["id"]: row["run"]})
    assert "differs" in frame["collection_errors"][0]["error"]


def test_multiple_possible_owners_are_not_independent_topic_examples():
    first, second = record(1), record(2)
    ambiguous = uncertain(
        first,
        [first["owner"], second["owner"] | {"trace_id": first["event"]["trace_id"]}],
    )
    context, refs, _ = triage.prepare([ambiguous, second], [], [])
    assert context["candidate_conversations"] == 2
    assert context["affected_conversations"] == 1
    assert context["event_catalog"][0]["conversation_ref"] is None
    assert len(context["event_catalog"][0]["conversation_refs"]) == 2
    plan = topic("e1") | {"representative_event_refs": ["e1", "e2"]}
    with pytest.raises(ValueError, match="repeat a conversation or trace"):
        triage.validate(
            {"topics": [plan], "no_action_reason": ""}, [ambiguous, second], refs, 3
        )


def test_unresolved_snapshot_preserves_signal_and_span_index_without_choosing_an_input():
    row = record()
    row = uncertain(
        row, [row["owner"], row["owner"] | {"conversation_id": identifier(999)}]
    )
    span = {"span_id": identifier(900), "conversation_id": identifier(999)}
    client = SimpleNamespace(
        args=SimpleNamespace(max_spans=1), query=lambda sql: [span, span]
    )
    result = hydrate_events(client, inventory([row]), [row])
    sample = result["samples"][0]
    assert sample["conversation_id"] is None
    assert sample["flow1_events"] == [row["event"]]
    assert sample["owner_candidates"] == row["owner_candidates"]
    assert sample["span_index"] == [span] and sample["span_index_clipped"]
    assert not sample.get("collection_error")


@pytest.mark.parametrize("known_automation", [True, False])
def test_unresolved_negatives_remain_explicit_gaps_and_never_become_controls(
    known_automation,
):
    client, frame, auto, records = fixture()
    possible_control = identifier(203)
    owners = [
        records[0]["owner"],
        records[0]["owner"] | {"conversation_id": possible_control},
    ]
    row = uncertain(records[0], owners if known_automation else [])
    report = cohorts.collect(
        client, frame, [auto] if known_automation else [], [row], seed=42
    )
    assert not report["complete"] and not report["errors"]
    assert report["attribution_gaps"][0]["event_id"] == row["event"]["id"]
    assert not report["selection"]["selected_conversation_ids"]["negative"]
    assert (
        possible_control
        not in report["selection"]["selected_conversation_ids"]["non_negative"]
    )


def test_population_excludes_possible_owners_of_ambiguous_trace_from_controls():
    auto, conv, trace, extra = map(identifier, (1, 2, 3, 4))
    owners = [
        {
            "automation_id": auto,
            "conversation_id": conv,
            "run_id": identifier(5),
            "trace_id": trace,
        },
        {
            "automation_id": auto,
            "conversation_id": conv,
            "run_id": identifier(6),
            "trace_id": extra,
        },
        {
            "automation_id": auto,
            "conversation_id": identifier(7),
            "run_id": identifier(8),
            "trace_id": extra,
        },
    ]
    run = {
        "status": "COMPLETED",
        "event_id": None,
        "input_tokens": 10,
        "output_tokens": 0,
    }
    frame = {
        "latest_runs": [
            run | {"trace_id": trace},
            run | {"trace_id": extra, "event_id": identifier(9)},
        ]
    }
    assert not population(SimpleNamespace(query=lambda sql: owners), frame, auto)


def test_finding_reader_exposes_exact_trace_and_original_signal_without_arbitrary_owner(
    tmp_path,
):
    row = uncertain(record(), [])
    event_id, trace = row["event"]["id"], row["event"]["trace_id"]
    client = SimpleNamespace(
        query=lambda sql: [copy.deepcopy(row["event"])]
        if "signal_events" in sql
        else []
    )
    reader = EvidenceReader(
        client, None, {"events": [row]}, tmp_path, finding_id=event_id
    )
    assert reader.owner(trace) == trace
    assert (
        reader.execute("search_runs", {"classification": "all", "offset": 0})["runs"][
            0
        ]["owner"]
        is None
    )
    result = reader.execute("read_signal", {"event_id": event_id, "offset": 0})
    assert result["payload"] == row["event"]["payload"]
    with pytest.raises(ValueError, match="outside this finding"):
        reader.owner(identifier(999))
    with pytest.raises(ValueError, match="outside this finding"):
        reader.execute("read_signal", {"event_id": identifier(999), "offset": 0})
    with pytest.raises(ValueError, match="either automation scope or finding scope"):
        EvidenceReader(
            client, identifier(1), {"events": [row]}, tmp_path, finding_id=event_id
        )
    with pytest.raises(ValueError, match="outside the prepared inventory"):
        EvidenceReader(
            client, None, {"events": [row]}, tmp_path, finding_id=identifier(999)
        )


@pytest.mark.parametrize("module", [triage_agent, topic_agent])
def test_agents_can_inspect_backlogged_unattributed_findings_not_in_current_window(
    tmp_path, monkeypatch, capsys, module
):
    row = uncertain(record(), [])
    frame, signals, context = (
        tmp_path / name for name in ("inventory.json", "records.json", "context.json")
    )
    write_json(frame, inventory([]))
    write_json(signals, [row])
    write_json(
        context,
        {
            "issue": {"number": 0, "url": None, "body": "preview"},
            "repository": "Example/source",
            "issue_evidence_dir": str(tmp_path / "issue"),
            "automation_ids": [],
            "inventory_path": str(frame),
            "signal_records_path": str(signals),
        },
    )
    monkeypatch.setattr(
        module, "Laminar", lambda args: SimpleNamespace(query=lambda sql: [])
    )
    result = module.evidence(
        context,
        "list_spans",
        {"trace_id": row["event"]["trace_id"], "offset": 0},
        finding_id=row["event"]["id"],
    )
    if module is triage_agent:
        result = json.loads(capsys.readouterr().out)
    assert result["spans"] == []
