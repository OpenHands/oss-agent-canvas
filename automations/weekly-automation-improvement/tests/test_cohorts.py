import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tarball"))
import cohorts


def identifier(number):
    return str(uuid.UUID(int=number))


def fixture(controls=7):
    owner = identifier(1)
    definition = {"id": identifier(2), "version": 1}
    records, owners, runs = [], [], []
    for index in range(3 + controls):
        trace = identifier(index + 100)
        metadata = {
            "automation_id": owner,
            "conversation_id": identifier(index + 200),
            "run_id": identifier(index + 300),
            "trace_id": trace,
            "automation_name": "Triage",
        }
        event = identifier(index + 400) if index < 3 else None
        run = {
            "trace_id": trace,
            "status": "COMPLETED",
            "event_id": event,
            "run_id": identifier(index + 500),
            "signal_version": 1,
            "input_tokens": 10,
            "output_tokens": 1,
        }
        owners.append(metadata)
        runs.append(run)
        if event:
            records.append(
                {
                    "owner": metadata,
                    "event": {"id": event, "trace_id": trace},
                    "run": run,
                    "definition": definition,
                }
            )

    class Client:
        def query(self, sql):
            if sql.startswith("SELECT DISTINCT"):
                return owners
            return [
                {
                    "input": json.dumps(
                        {"message": {"role": "user", "content": "Native task"}}
                    )
                }
            ]

        def load_sample(self, sample):
            sample["trace_ids"] = [sample["trace_id"]]

        def load_flow1(self, sample, definition):
            sample["flow1_events"] = [
                {"id": event} for event in sample["selected_event_ids"]
            ]

    inventory = {
        "project_id": identifier(3),
        "flow1_signal": definition,
        "flow1_definitions": {"1": definition},
        "latest_runs": runs,
        "window": {"since": "2026-10-01", "until": "2026-10-08"},
    }
    return Client(), inventory, owner, records


@pytest.mark.parametrize("size", [1, 2, 5, 7])
def test_user_sample_size_controls_two_distinct_frozen_cohorts(size):
    args = fixture()
    first = cohorts.collect(*args, size=size, seed=42)
    second = cohorts.collect(*args, size=size, seed=42)
    assert first == second
    assert first["complete"]
    assert sum(row["classification"] == "negative" for row in first["samples"]) == min(
        size, 3
    )
    assert (
        sum(row["classification"] == "non_negative" for row in first["samples"]) == size
    )
    identities = [row["conversation_id"] for row in first["samples"]]
    assert len(identities) == len(set(identities))


def test_failed_selected_input_is_not_replaced_and_short_control_frame_is_incomplete():
    client, inventory, owner, records = fixture(controls=2)
    report = cohorts.collect(client, inventory, owner, records, size=5, seed=42)
    assert not report["complete"]
    assert len(report["selection"]["selected_conversation_ids"]["non_negative"]) == 2
    client, inventory, owner, records = fixture()
    expected = cohorts.collect(client, inventory, owner, records, size=2, seed=42)
    client.load_sample = lambda sample: (_ for _ in ()).throw(
        ValueError("Missing input")
    )
    failed = cohorts.collect(client, inventory, owner, records, size=2, seed=42)
    assert not failed["complete"] and failed["errors"]
    assert failed["selection"] == expected["selection"]


@pytest.mark.parametrize("value", [0, -1, True, "5"])
def test_invalid_user_sample_size_is_rejected(value):
    with pytest.raises(ValueError, match="positive integer"):
        cohorts.collect(*fixture(), size=value)


def test_preparation_freezes_inputs_and_repository_without_label_inference(
    tmp_path, monkeypatch
):
    data = cohorts.collect(*fixture(), size=1, seed=42)
    task = {
        "issue": {
            "number": 7,
            "title": "A report",
            "author_body": "Original text",
            "labels": [],
        }
    }
    for sample in data["samples"]:
        sample["initial_instructions"] = [
            {
                "content": "modify only fixture/review issue #7.\nMarker: <!-- marker -->\n\n"
                + json.dumps(task)
            }
        ]
    calls = []
    monkeypatch.setattr(cohorts, "capture_reads", lambda *a: {})
    monkeypatch.setattr(
        cohorts,
        "repository_snapshot",
        lambda repo, output: calls.append(repo) or {"revision": "b" * 40},
    )
    prepared = cohorts.prepare(data, {"revision": "a" * 40}, tmp_path)
    assert prepared["complete"] and calls == ["fixture/review"]
    assert all("expected_outcome" not in sample for sample in prepared["samples"])
    assert prepared["baseline_revision"] == "a" * 40


def test_redacted_native_markers_cannot_be_replayed():
    sample = {
        "initial_instructions": [
            {
                "content": 'modify only fixture/review issue #7.\nMarker: <!-- triage-source:[REDACTED] -->\n\n{"issue":{"number":7}}'
            }
        ]
    }
    with pytest.raises(ValueError, match="marker"):
        cohorts.native_payload(sample)


def test_validation_expands_exact_signatures_without_absorbing_a_mixed_cluster():
    def record(identifier, signature, cause="agent"):
        return {
            "event": {
                "id": identifier,
                "leaf_clusters": ["mixed"],
                "payload": {"signature": signature, "cause": cause},
            }
        }

    records = [
        record("a", "Wrong priority"),
        record("b", " wrong PRIORITY "),
        record("c", "Wrong readiness"),
        record("d", "Wrong priority", "provider"),
    ]
    assert [item["event"]["id"] for item in cohorts.topic_records(records, ["a"])] == [
        "a",
        "b",
    ]
    missing = [record("e", ""), record("f", "")]
    assert cohorts.topic_records(missing, ["e"]) == missing[:1]


def test_shared_topic_uses_one_n_sized_cohort_across_automations():
    client, inventory, owner, records = fixture()
    second = identifier(999)
    owners = client.query("SELECT DISTINCT")
    for index, row in enumerate(owners):
        if index % 2:
            row["automation_id"] = second
    result = cohorts.collect(
        client, inventory, [owner, second], records, size=2, seed=42
    )
    assert result["automation_ids"] == sorted([owner, second])
    assert result["selection"]["population_sizes"] == {"negative": 3, "non_negative": 7}
    assert len([r for r in result["samples"] if r["classification"] == "negative"]) == 2
    assert (
        len([r for r in result["samples"] if r["classification"] == "non_negative"])
        == 2
    )
