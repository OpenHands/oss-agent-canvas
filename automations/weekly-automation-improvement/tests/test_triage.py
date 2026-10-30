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


def topic(reference, key="publication-payload", priority=2):
    return {
        "key": key,
        "title": "Validate publication payload",
        "priority": priority,
        "reason": "Repeated supported workflow defect",
        "representative_event_refs": [reference],
    }


def test_global_preparation_retains_examples_full_records_and_prior_outcomes():
    records = [record(1), record(2), record(3, automation=2)]
    previous = [
        {
            "id": "prior",
            "key": "provider-recovery",
            "status": "unrepaired",
            "reason": "No supported fix",
        }
    ]
    context, references, prepared = triage.prepare(records, [], previous, ["prior"])
    assert len(context["automations"]) == 2
    assert context["groups"][0]["affected_conversations"] == 2
    assert len(context["event_catalog"]) == 3 and len(prepared) == 3
    assert prepared[0]["event_ref"] == "e1"
    assert context["previous_attempts"] == previous
    assert context["explicit_retry_topics"] == previous
    assert references["events"]["e3"] == records[2]["event"]["id"]


def test_luna_agent_returns_a_globally_sorted_scoped_plan(tmp_path, monkeypatch):
    from types import SimpleNamespace

    records = [record(1), record(2, automation=2)]
    calls = []
    issue = SimpleNamespace(
        record=lambda *a: None, refresh=lambda: {"url": "https://example.test/triage"}
    )

    def agent(args, context, prepared, schema):
        calls.append(context)
        assert len(context["automations"]) == 2 and len(prepared) == 2
        return (
            {
                "topics": [topic("e1", "minor", 3), topic("e2", "urgent", 0)],
                "no_action_reason": "",
            },
            {"backend": "openhands-sdk"},
            issue,
        )

    monkeypatch.setattr(triage, "rank", agent)
    plans, aggregation = triage.triage(arguments(tmp_path), records, [])
    assert [p["priority"] for p in plans] == [0, 3]
    assert plans[0]["automation_id"] == records[1]["owner"]["automation_id"]
    assert len(calls) == 1
    assert aggregation["triage_issue"]["url"] == "https://example.test/triage"


@pytest.mark.parametrize(
    "bad",
    [
        "foreign_event",
        "duplicate_conversation",
        "missing_evidence",
        "reused_event",
        "duplicate_topic",
        "invalid_key",
        "invalid_priority",
    ],
)
def test_selection_rejects_invalid_references_and_priority(bad):
    records = [
        record(1),
        record(
            2,
            conversation=1 if bad == "duplicate_conversation" else 2,
        ),
    ]
    _, references, _ = triage.prepare(records, [], [])
    plan = topic("e1")
    result = {"topics": [plan], "no_action_reason": ""}
    if bad == "foreign_event":
        plan["representative_event_refs"] = ["e999"]
    elif bad == "duplicate_conversation":
        plan["representative_event_refs"] = ["e1", "e2"]
    elif bad == "missing_evidence":
        plan["representative_event_refs"] = []
    elif bad == "reused_event":
        result["topics"].append(topic("e1", "other"))
    elif bad == "duplicate_topic":
        result["topics"].append(topic("e2"))
    elif bad == "invalid_key":
        plan["key"] = "not a stable key"
    else:
        plan["priority"] = True
    with pytest.raises(ValueError):
        triage.validate(result, records, references, 3)


def test_mixed_groups_split_and_different_groups_merge_without_count_quota():
    records = [record(i) for i in range(1, 6)]
    _, refs, _ = triage.prepare(records, [], [])
    split = triage.validate(
        {
            "topics": [topic("e" + str(i), "issue-" + str(i)) for i in range(1, 6)],
            "no_action_reason": "",
        },
        records,
        refs,
        3,
    )
    assert len(split) == 5 and split[0]["group_ids"] == split[1]["group_ids"]
    records[1]["event"]["leaf_clusters"] = [str(uuid.UUID(int=42))]
    _, refs, _ = triage.prepare(records, [], [])
    merged = topic("e1")
    merged["representative_event_refs"] = ["e1", "e2"]
    plans = triage.validate(
        {"topics": [merged], "no_action_reason": ""}, records, refs, 3
    )
    assert len(plans[0]["group_ids"]) == 2


def test_different_topics_can_use_different_events_from_one_conversation():
    rows = [record(1), record(2, conversation=1)]
    _, refs, _ = triage.prepare(rows, [], [])
    assert (
        len(
            triage.validate(
                {
                    "topics": [topic("e1", "first"), topic("e2", "second")],
                    "no_action_reason": "",
                },
                rows,
                refs,
                3,
            )
        )
        == 2
    )


def test_empty_plan_requires_a_reason():
    with pytest.raises(ValueError, match="explain"):
        triage.validate({"topics": [], "no_action_reason": ""}, [], {"events": {}}, 3)


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


def test_shared_root_cause_can_span_automations_with_one_stable_topic_id():
    rows = [record(1), record(2, automation=2)]
    _, refs, _ = triage.prepare(rows, [], [])
    plan = topic("e1")
    plan["representative_event_refs"] = ["e1", "e2"]
    result = triage.validate({"topics": [plan], "no_action_reason": ""}, rows, refs, 3)[
        0
    ]
    assert result["automation_ids"] == sorted(
        {r["owner"]["automation_id"] for r in rows}
    )
    _, refs, _ = triage.prepare(rows[1:], [], [])
    single = triage.validate(
        {"topics": [topic("e1")], "no_action_reason": ""}, rows[1:], refs, 3
    )[0]
    assert single["id"] == result["id"]
