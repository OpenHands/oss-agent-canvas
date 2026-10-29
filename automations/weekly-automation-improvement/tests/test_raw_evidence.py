import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tarball"))
import evidence_agent
from raw_evidence import EvidenceReader, population


OWNER = str(uuid.UUID(int=1))
TRACE = str(uuid.UUID(int=2))
SPAN = str(uuid.UUID(int=3))
EVENT = str(uuid.UUID(int=4))


def test_raw_evidence_requires_ownership_and_retains_paged_originals(tmp_path):
    queries = []

    class Client:
        def query(self, sql):
            queries.append(sql)
            if "SELECT DISTINCT" in sql:
                return [{"automation_id": OWNER}]
            return [
                {
                    "span_id": SPAN,
                    "total_chars": 25000,
                    "content": "Original omitted detail",
                }
            ]

    reader = EvidenceReader(Client(), OWNER, {}, tmp_path)
    result = reader.execute(
        "read_trace",
        {"trace_id": TRACE, "span_id": SPAN, "field": "output", "offset": 12000},
    )
    assert result["next_offset"] == 24000
    assert result["content"] == "Original omitted detail"
    assert "substringUTF8(output,12001,12000)" in queries[-1]
    assert json.loads((tmp_path / "read-001.json").read_text())["result"] == result
    reader.client.query = lambda sql: [{"automation_id": str(uuid.UUID(int=99))}]
    with pytest.raises(ValueError, match="outside this automation"):
        reader.execute("list_spans", {"trace_id": TRACE, "offset": 0})
    with pytest.raises(ValueError):
        reader.execute(
            "read_trace",
            {"trace_id": TRACE, "span_id": SPAN, "field": "api_key", "offset": 0},
        )


def test_non_negative_frame_excludes_unscored_and_mixed_conversations():
    owners, runs = [], []
    for number, status, event, tokens, conversation in [
        (1, "COMPLETED", None, 10, 1),
        (2, "COMPLETED", EVENT, 10, 1),
        (3, "FAILED", None, 10, 2),
        (4, "COMPLETED", None, 0, 3),
        (5, "COMPLETED", None, 10, 4),
    ]:
        trace = str(uuid.UUID(int=100 + number))
        owners.append(
            {
                "trace_id": trace,
                "automation_id": OWNER,
                "conversation_id": str(uuid.UUID(int=conversation)),
                "run_id": str(uuid.UUID(int=200 + number)),
                "automation_name": "Triage",
            }
        )
        runs.append(
            {
                "trace_id": trace,
                "status": status,
                "event_id": event,
                "input_tokens": tokens,
                "output_tokens": 0,
            }
        )
    client = SimpleNamespace(query=lambda sql: owners)
    rows = population(client, {"latest_runs": runs}, OWNER)
    controls = [row for row in rows if row["classification"] == "non_negative"]
    assert len(controls) == 1
    assert controls[0]["conversation_id"] == str(uuid.UUID(int=4))
    assert len([row for row in rows if row["classification"] == "negative"]) == 1


def test_readonly_validation_can_request_detail_then_report(tmp_path, monkeypatch):
    requests, reads = [], []
    reader = SimpleNamespace(
        schemas=[{"name": "list_spans", "parameters": {"type": "object"}}],
        execute=lambda name, arguments: reads.append((name, arguments))
        or {"detail": "Raw evidence"},
    )

    def respond(args, model, body, endpoint, name, **kwargs):
        requests.append(json.loads(json.dumps(body)))
        call = (
            {
                "type": "function_call",
                "call_id": "one",
                "name": "list_spans",
                "arguments": '{"offset":0}',
            }
            if len(requests) == 1
            else {
                "type": "function_call",
                "call_id": "two",
                "name": "report",
                "arguments": '{"proposals":[]}',
            }
        )
        output = (
            [{"type": "reasoning", "encrypted_content": "opaque-" + "A" * 128}, call]
            if len(requests) == 1
            else [call]
        )
        return {"output": output}, {
            "seconds": 1,
            "input_bytes": 100,
            "reported_cost_usd": "0.01",
        }

    monkeypatch.setattr(evidence_agent, "request_model", respond)
    args = SimpleNamespace(
        api="responses",
        max_output_tokens=4096,
        effort="low",
        evidence_reader=reader,
        max_evidence_reads=1,
    )
    value, measurement = evidence_agent.investigate(
        args, "sol", "Investigate", {}, "validation", {}, reader
    )
    assert value == {"proposals": []}
    assert reads == [("list_spans", {"offset": 0})]
    assert requests[1]["input"][-1]["type"] == "function_call_output"
    assert requests[1]["input"][2]["encrypted_content"] == "opaque-" + "A" * 128
    assert requests[1]["tool_choice"] == {"type": "function", "name": "report"}
    assert measurement["evidence_reads"] == 1
    assert measurement["reported_cost_usd"] == "0.02"


def test_agent_cannot_exceed_its_evidence_read_budget(monkeypatch):
    reader = SimpleNamespace(schemas=[], execute=lambda *a: {})
    monkeypatch.setattr(
        evidence_agent,
        "request_model",
        lambda *a, **k: (
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "list_spans",
                        "call_id": "one",
                        "arguments": "{}",
                    }
                ]
            },
            {"seconds": 1, "input_bytes": 1, "reported_cost_usd": "0"},
        ),
    )
    args = SimpleNamespace(
        api="responses", max_output_tokens=4096, effort="low", max_evidence_reads=1
    )
    with pytest.raises(ValueError, match="budget exceeded"):
        evidence_agent.investigate(
            args, "sol", "Investigate", {}, "coordinator", {}, reader
        )
