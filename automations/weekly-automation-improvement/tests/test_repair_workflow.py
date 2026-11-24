import uuid
from pathlib import Path

from test_weekly import inventory, record
from test_support import SIGNAL


def test_topic_preparation_retains_cross_automation_inputs_without_source_gate(
    tmp_path, monkeypatch
):
    from common import write_json
    import repair_workflow as weekly

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
