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


def topic(reference, key="publication-payload"):
    return {
        "key": key,
        "title": "Validate publication payload",
        "reason": "Repeated supported workflow defect",
        "representative_event_refs": [reference],
    }


def test_luna_sees_compact_refs_and_returns_scoped_topics(tmp_path, monkeypatch):
    records = [record(1), record(2)]
    calls = []

    def model(args, model, instruction, context, name, schema):
        calls.append(context)
        assert context["groups"][0]["affected_conversations"] == 2
        assert "spans" not in json.dumps(context)
        assert records[0]["event"]["id"] not in json.dumps(context)
        assert context["groups"][0]["examples"][0]["conversation_ref"] == "c1"
        allowed = schema["properties"]["topics"]["items"]["properties"][
            "representative_event_refs"
        ]["items"]["enum"]
        assert allowed == ["e1", "e2"]
        return {"topics": [topic("e1")], "no_action_reason": ""}, {"model": model}

    monkeypatch.setattr(triage, "call_model", model)
    plans, aggregation = triage.triage(arguments(tmp_path), records, [])
    assert plans[0]["representative_event_ids"] == [records[0]["event"]["id"]]
    assert plans[0]["id"] == triage.topic_id(
        records[0]["owner"]["automation_id"], plans[0]["key"]
    )
    assert (
        aggregation["total_events"] == 2 and "examples" not in aggregation["groups"][0]
    )
    assert len(calls) == 1 and (tmp_path / "triage-references.json").exists()


def test_retryable_conclusions_and_explicit_requests_reach_real_triage(
    tmp_path, monkeypatch
):
    previous = [
        {
            "id": "prior",
            "key": "provider-recovery",
            "title": "Recover provider failures",
            "status": "retryable",
            "reason": "Previous experiment did not establish improvement",
            "assessed_event_ids": ["old-event"],
        }
    ]

    def model(args, model, policy, context, name, schema):
        assert context["completed_or_reserved_topics_this_week"] == []
        assert context["previous_attempts"][0]["status"] == "retryable"
        assert context["previous_attempts"][0]["reason"] == previous[0]["reason"]
        assert context["explicit_retry_topics"] == ["provider-recovery"]
        assert "Do not rename or rephrase an exhausted repair mechanism" in policy
        return {"topics": [], "no_action_reason": "No new repair approach"}, {}

    monkeypatch.setattr(triage, "call_model", model)
    args = arguments(tmp_path, retry_topic_ids=["prior"])
    plans, _ = triage.triage(args, [record()], [], previous)
    assert not plans


@pytest.mark.parametrize(
    "bad",
    [
        "foreign_event",
        "duplicate_conversation",
        "missing_evidence",
        "reused_event",
        "duplicate_topic",
        "invalid_key",
        "budget",
    ],
)
def test_luna_selection_rejects_invalid_references(tmp_path, monkeypatch, bad):
    records = [
        record(1),
        record(2, conversation=1 if bad == "duplicate_conversation" else 2),
    ]
    if bad == "duplicate_conversation":
        records[1]["event"]["leaf_clusters"] = [str(uuid.UUID(int=42))]

    def model(args, model, instruction, context, name, schema):
        plan = topic("e1")
        result = {"topics": [plan], "no_action_reason": ""}
        if bad == "foreign_event":
            plan["representative_event_refs"] = ["e999"]
        elif bad == "duplicate_conversation":
            plan["representative_event_refs"] = ["e1", "e2"]
        elif bad == "missing_evidence":
            plan["representative_event_refs"] = []
        elif bad == "reused_event":
            result["topics"].append(topic("e1", "other-issue"))
        elif bad == "duplicate_topic":
            result["topics"].append(topic("e2"))
        elif bad == "invalid_key":
            plan["key"] = "not a stable key"
        else:
            result["topics"].append(topic("e2", "other-issue"))
        return result, {}

    monkeypatch.setattr(triage, "call_model", model)
    args = arguments(tmp_path, max_topics=1 if bad == "budget" else 3)
    with pytest.raises(ValueError):
        triage.triage(args, records, [])


def test_mixed_cluster_can_split_and_different_clusters_can_merge(
    tmp_path, monkeypatch
):
    records = [record(1), record(2), record(3)]

    def split(*args):
        return {
            "topics": [topic("e1", "head-check"), topic("e2", "payload-validation")],
            "no_action_reason": "",
        }, {}

    monkeypatch.setattr(triage, "call_model", split)
    plans, _ = triage.triage(arguments(tmp_path / "split"), records, [])
    assert len(plans) == 2 and plans[0]["group_ids"] == plans[1]["group_ids"]
    records[1]["event"]["leaf_clusters"] = [str(uuid.UUID(int=42))]

    def merge(args, model, instruction, context, name, schema):
        plan = topic(context["groups"][0]["examples"][0]["event_ref"])
        plan["representative_event_refs"] = [
            g["examples"][0]["event_ref"] for g in context["groups"]
        ]
        return {"topics": [plan], "no_action_reason": ""}, {}

    monkeypatch.setattr(triage, "call_model", merge)
    plans, _ = triage.triage(arguments(tmp_path / "merge"), records, [])
    assert len(plans) == 1 and len(plans[0]["group_ids"]) == 2


def test_different_topics_can_use_different_events_from_one_conversation(
    tmp_path, monkeypatch
):
    records = [record(1), record(2, conversation=1)]
    records[1]["event"]["leaf_clusters"] = [str(uuid.UUID(int=42))]

    def model(*args):
        return {
            "topics": [topic("e1", "issue-1"), topic("e2", "issue-2")],
            "no_action_reason": "",
        }, {}

    monkeypatch.setattr(triage, "call_model", model)
    plans, _ = triage.triage(arguments(tmp_path), records, [])
    assert len(plans) == 2


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
