import json
from types import SimpleNamespace

import pytest

from test_automation import arguments
import validate_repairs

AUTOMATION = "00000000-0000-0000-0000-000000000001"
TRACE = "00000000-0000-0000-0000-000000000002"


def test_backfill_captures_input_from_the_owned_laminar_trace(tmp_path, monkeypatch):
    queries = []

    def query(sql):
        queries.append(sql)
        if "AS automation_id" in sql:
            return [{"automation_id": AUTOMATION}]
        return [
            {
                "input": json.dumps(
                    {"message": {"role": "user", "content": "Original task"}}
                )
            }
        ]

    monkeypatch.setattr(validate_repairs, "arguments", lambda *a: arguments(tmp_path))
    monkeypatch.setattr(
        validate_repairs, "Laminar", lambda *a: SimpleNamespace(query=query)
    )
    body = f"<!-- automation-improvement-automation: {AUTOMATION} -->\nhttps://example.test/traces/{TRACE}"
    value = validate_repairs.collect_snapshot({}, body, tmp_path)
    assert value["samples"][0]["initial_instructions"][0]["content"] == "Original task"
    assert len(queries) == 2
    assert all(TRACE in sql for sql in queries)


def test_backfill_rejects_a_trace_owned_by_another_automation(tmp_path, monkeypatch):
    monkeypatch.setattr(validate_repairs, "arguments", lambda *a: arguments(tmp_path))
    monkeypatch.setattr(
        validate_repairs,
        "Laminar",
        lambda *a: SimpleNamespace(query=lambda sql: [{"automation_id": "another"}]),
    )
    body = f"<!-- automation-improvement-automation: {AUTOMATION} -->\nhttps://example.test/traces/{TRACE}"
    with pytest.raises(ValueError, match="does not belong"):
        validate_repairs.collect_snapshot({}, body, tmp_path)
