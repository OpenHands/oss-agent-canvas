import copy
import json
import uuid

import pytest

from test_automation import arguments
from test_weekly import record, inventory
from events import collect_events
import triage


def test_grouping_counts_unique_conversations_and_uses_native_clusters():
    records = [record(1), record(2, conversation=1), record(3)]
    cards, members = triage.group_findings(records)
    assert len(cards) == 1 and cards[0]["events"] == 3
    assert cards[0]["affected_conversations"] == 2
    assert len(cards[0]["examples"]) == 2
    records[0]["event"]["leaf_clusters"] = [str(uuid.UUID(int=1))]
    cards, members = triage.group_findings(records)
    assert len(cards) == 2
    assert {card["grouping"] for card in cards} == {
        "laminar_leaf_cluster",
        "exact_signature",
    }
    assert sum(len(items) for items in members.values()) == 3


def test_luna_sees_aggregates_and_returns_only_representative_ids(
    tmp_path, monkeypatch
):
    records = [record(1), record(2)]
    args = arguments(tmp_path)
    calls = []

    def model(args, model, instruction, context, name, schema):
        calls.append(context)
        group = context["groups"][0]
        assert group["affected_conversations"] == 2
        assert "spans" not in json.dumps(context)
        return {
            "priorities": [
                {
                    "group_ids": [group["group_id"]],
                    "reason": "Repeated supported workflow defect",
                }
            ],
            "representative_event_ids": [records[0]["event"]["id"]],
            "no_action_reason": "",
        }, {"model": model}

    monkeypatch.setattr(triage, "call_model", model)
    chosen, aggregation = triage.triage(args, records, [])
    assert chosen == records[:1]
    assert aggregation["total_events"] == 2
    assert "examples" not in aggregation["groups"][0]
    assert len(calls) == 1
    assert (tmp_path / "triage-input.json").exists()


@pytest.mark.parametrize(
    "bad",
    ["foreign_event", "foreign_group", "duplicate_conversation", "missing_evidence"],
)
def test_luna_selection_rejects_invalid_references(tmp_path, monkeypatch, bad):
    records = (
        [record(1), record(2, conversation=1)]
        if bad == "duplicate_conversation"
        else [record(1), record(2)]
    )

    def model(args, model, instruction, context, name, schema):
        group = context["groups"][0]
        result = {
            "priorities": [{"group_ids": [group["group_id"]], "reason": "Investigate"}],
            "representative_event_ids": [records[0]["event"]["id"]],
            "no_action_reason": "",
        }
        if bad == "foreign_event":
            result["representative_event_ids"] = [str(uuid.UUID(int=900))]
        elif bad == "foreign_group":
            result["priorities"][0]["group_ids"] = ["not-a-group"]
        elif bad == "duplicate_conversation":
            context["groups"][0]["examples"].append(
                {"event_id": records[1]["event"]["id"]}
            )
            result["representative_event_ids"].append(records[1]["event"]["id"])
        else:
            result["representative_event_ids"] = []
        return result, {}

    monkeypatch.setattr(triage, "call_model", model)
    with pytest.raises(ValueError):
        triage.triage(arguments(tmp_path), records, [])


def test_summary_collection_covers_all_events_without_loading_trajectories(tmp_path):
    records = [record(1), record(2), record(3, automation=2), record(4)]
    frame = inventory(records)
    frame["flow1_signal"].update(
        version=2, prompt="same detector", structuredOutput={"type": "object"}
    )
    history = [
        {
            "version": 1,
            "definition": {
                "llmModel": "flow-1",
                "structuredOutputSchema": {"type": "object"},
            },
        },
        {"version": 3, "definition": {"llmModel": "other-model"}},
    ]

    class Client:
        args = arguments(tmp_path, signal_id=frame["flow1_signal"]["id"], limit=1)
        base = "https://example.test"
        measurements = []

        def request(self, url):
            if url.endswith("/versions"):
                return {"versions": history}
            return (
                frame["flow1_signal"] if "/signals/" in url else frame["flow1_profile"]
            )

        def query(self, sql):
            if "FROM signal_runs" in sql:
                return [item["run"] for item in records]
            if "FROM signal_events" in sql:
                return [item["event"] for item in records]
            return [item["owner"] for item in records]

        def load_sample(self, sample):
            pytest.fail("Inventory must not hydrate trajectories")

    result = collect_events(Client(), set(), aggregate=True)
    assert len(result["events"]) == 4
    assert result["samples"] == []
    assert result["selection"]["method"] == "all_unhandled_events"
    assert result["selection"]["sample_size"] == 4
    assert not result["collection_errors"]
    assert set(result["flow1_definitions"]) == {"1", "2"}
    assert all(item["definition"]["version"] == 1 for item in result["events"])


def test_replay_can_use_persisted_definition_after_sampling_change():
    from test_flow1 import frozen
    from flow1 import findings

    value = frozen()
    case = value["samples"][0]
    case["flow1_signal"] = copy.deepcopy(value["flow1_signal"])
    value["flow1_signal"]["version"] += 1
    result, _ = findings(value, case)
    assert len(result["findings"]) == 1


def test_empty_signatures_do_not_merge_unrelated_findings():
    records = [record(1), record(2)]
    for item in records:
        payload = json.loads(item["event"]["payload"])
        payload["signature"] = ""
        item["event"]["payload"] = json.dumps(payload)
    cards, _ = triage.group_findings(records)
    assert len(cards) == 2
