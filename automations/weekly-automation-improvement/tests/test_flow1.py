import copy
import json
import uuid

import pytest

from test_automation import app, arguments, candidate, snapshot
from flow1 import estimated_cost, findings


SIGNAL = str(uuid.UUID(int=10))
RUN = str(uuid.UUID(int=11))
EVENT = str(uuid.UUID(int=12))


def frozen(with_finding=True):
    value = snapshot()
    value["analysis_backend"] = "flow1"
    value["flow1_signal"] = {
        "id": SIGNAL,
        "projectId": value["project_id"],
        "model": "flow-1",
        "llmProfileId": str(uuid.UUID(int=13)),
        "version": 1,
    }
    value["flow1_profile"] = {
        "id": value["flow1_signal"]["llmProfileId"],
        "provider": "custom",
        "config": {"baseUrl": "https://api.lmnr.ai/v1"},
        "models": ["flow-1"],
    }
    case = value["samples"][0]
    case["flow1_trace_sessions"] = [
        {
            "trace_id": case["trace_ids"][0],
            "conversation_ids": [case["conversation_id"]],
        }
    ]
    case["flow1_runs"] = [
        {
            "signal_id": SIGNAL,
            "signal_version": 1,
            "run_id": RUN,
            "trace_id": case["trace_ids"][0],
            "status": "COMPLETED",
            "updated_at": "2026-10-22T00:00:00Z",
            "event_id": EVENT if with_finding else str(uuid.UUID(int=0)),
            "input_tokens": 100,
            "cache_read_tokens": 0,
            "output_tokens": 20,
        }
    ]
    payload = candidate() | {
        "evidence": f"[failure](https://laminar.app.all-hands.dev/project/{value['project_id']}/traces/{case['trace_ids'][0]}?spanId={case['spans'][0]['span_id']}&chat=true)"
    }
    case["flow1_events"] = (
        [
            {
                "id": EVENT,
                "signal_id": SIGNAL,
                "signal_version": 1,
                "run_id": RUN,
                "trace_id": case["trace_ids"][0],
                "payload": json.dumps(payload),
            }
        ]
        if with_finding
        else []
    )
    return value


def test_completed_no_event_skips_all_model_calls_and_laminar(tmp_path, monkeypatch):
    value = frozen(False)
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(
        app, "Laminar", lambda *a: pytest.fail("Replay uses no Laminar API")
    )
    monkeypatch.setattr(
        app, "call_model", lambda *a: pytest.fail("No finding needs no model")
    )
    args = arguments(tmp_path / "review", input=path)
    app.run(args)
    report = json.loads((args.output / "report.json").read_text())
    assert report["analysis_backend"] == "flow1"
    assert report["findings"] == report["errors"] == []
    assert report["measurements"][0]["reported_cost_usd"] is None


@pytest.mark.parametrize("summary_length", [2000, 3500, 20000])
def test_flow1_finding_goes_directly_to_sol_with_evidence(
    tmp_path, monkeypatch, summary_length
):
    value = frozen()
    event = value["samples"][0]["flow1_events"][0]
    payload = json.loads(event["payload"])
    payload["summary"] = "x" * summary_length
    event["payload"] = json.dumps(payload)
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(value))
    calls = []

    def model(args, model, policy, context, name, schema):
        calls.append((model, name, context))
        return {
            "proposals": [],
            "rejections": [
                {
                    "finding_id": context["findings"][0]["id"],
                    "reason": "The observed failure does not establish a source defect.",
                }
            ],
        }, {"model": model}

    monkeypatch.setattr(app, "call_model", model)
    args = arguments(tmp_path / "review", input=path)
    app.run(args)
    assert [(m, n) for m, n, _ in calls] == [("large", "coordinator")]
    assert calls[0][2]["findings"][0]["evidence"] == ["s1"]
    assert calls[0][2]["findings"][0]["summary"] == payload["summary"]
    assert "FAILED" in json.dumps(calls[0][2]["cases"])
    assert "payload" not in calls[0][2]["cases"][0]["prior_signals_unverified"][0]
    assert json.loads((args.output / "report.json").read_text())["rejected_findings"][
        0
    ]["reason"]


def test_finding_text_limits_preserve_native_and_generated_boundaries():
    sample = snapshot()["samples"][0]
    result = {"findings": [candidate() | {"summary": "x" * 2001}]}
    assert (
        app.validated_findings(result, sample)[1][0]["reason"] == "Invalid finding text"
    )
    result["findings"][0]["summary"] = "x" * 20001
    valid, rejected = app.validated_findings(result, sample, max_text_chars=20000)
    assert valid == []
    assert rejected[0]["reason"] == "Invalid finding text"


def test_cost_estimate_counts_fresh_cached_output_and_distinct_attempts():
    pricing = {
        "input_usd_per_million": 0.05,
        "cached_input_usd_per_million": 0.01,
        "output_usd_per_million": 0.3,
    }
    run = {
        "run_id": "a",
        "updated_at": "2026-10-22T00:00:00Z",
        "input_tokens": 1000000,
        "cache_read_tokens": 200000,
        "output_tokens": 100000,
    }
    older = run | {"input_tokens": 500000, "updated_at": "2026-10-21T00:00:00Z"}
    retry = run | {"run_id": "b"}
    assert estimated_cost([run, older, retry], pricing) == pytest.approx(0.144)
    assert estimated_cost([run], None) is None
    assert estimated_cost([run | {"cache_read_tokens": 2000000}], pricing) is None
    assert (
        estimated_cost(
            [run | {"input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0}],
            pricing,
        )
        is None
    )


@pytest.mark.parametrize(
    "rejections",
    [
        [],
        [{"finding_id": "foreign", "reason": "No defect"}],
        [{"finding_id": "c1f1", "reason": ""}],
    ],
)
def test_coordinator_cannot_silently_drop_a_finding(tmp_path, monkeypatch, rejections):
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(frozen()))
    monkeypatch.setattr(
        app,
        "call_model",
        lambda *args: ({"proposals": [], "rejections": rejections}, {}),
    )
    args = arguments(tmp_path / "review", input=path)
    with pytest.raises(ValueError, match="Partial analysis"):
        app.run(args)
    report = json.loads((args.output / "report.json").read_text())
    assert report["errors"][0]["stage"] == "coordinator"


@pytest.mark.parametrize("status", ["FAILED", "PENDING", "PROCESSING"])
def test_unfinished_or_failed_signal_is_not_clean(status):
    value = frozen(False)
    value["samples"][0]["flow1_runs"][0]["status"] = status
    with pytest.raises(ValueError, match="not a clean case"):
        findings(value, value["samples"][0])


def test_multitrace_conversation_requires_every_trace_to_be_analyzed():
    value = frozen(False)
    value["samples"][0]["trace_ids"].append(str(uuid.UUID(int=15)))
    with pytest.raises(ValueError, match="scope|Missing|ownership"):
        findings(value, value["samples"][0])


@pytest.mark.parametrize(
    "mismatch", ["project", "trace", "span", "version", "missing_event", "later_span"]
)
def test_foreign_stale_or_missing_evidence_fails_closed(mismatch):
    value = frozen()
    case = value["samples"][0]
    event = case["flow1_events"][0]
    payload = json.loads(event["payload"])
    if mismatch == "project":
        payload["evidence"] = payload["evidence"].replace(
            value["project_id"], str(uuid.UUID(int=90))
        )
    elif mismatch == "trace":
        payload["evidence"] = payload["evidence"].replace(
            case["trace_ids"][0], str(uuid.UUID(int=90))
        )
    elif mismatch == "span":
        payload["evidence"] = payload["evidence"].replace(
            case["spans"][0]["span_id"], str(uuid.UUID(int=90))
        )
    elif mismatch == "version":
        event["signal_version"] = 2
    elif mismatch == "missing_event":
        case["flow1_events"] = []
    else:
        case["spans"][0]["end_time"] = "2026-10-22T01:00:00Z"
    event["payload"] = json.dumps(payload)
    with pytest.raises(ValueError):
        findings(value, case)


def test_latest_failed_run_does_not_fall_back_to_an_older_success():
    value = frozen(False)
    case = value["samples"][0]
    case["flow1_runs"].append(
        case["flow1_runs"][0]
        | {
            "run_id": str(uuid.UUID(int=16)),
            "status": "FAILED",
            "updated_at": "2026-10-22T01:00:00Z",
        }
    )
    with pytest.raises(ValueError, match="failed"):
        findings(value, case)


def test_collector_scopes_runs_and_preserves_random_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test")
    monkeypatch.setenv("LMNR_BASE_URL", "https://example.test")
    value = frozen(False)
    client = app.Laminar(arguments(tmp_path, analyzer="flow1", signal_id=SIGNAL))
    monkeypatch.setattr(
        client,
        "request",
        lambda url: value["flow1_profile"]
        if "/llm-profiles/" in url
        else value["flow1_signal"],
    )
    queries = []

    def query(sql):
        queries.append(sql)
        if "GROUP BY conversation_id" in sql:
            return [copy.deepcopy(value["samples"][0])]
        if "FROM signal_runs" in sql:
            return value["samples"][0]["flow1_runs"]
        return []

    monkeypatch.setattr(client, "query", query)
    monkeypatch.setattr(
        client,
        "load_sample",
        lambda case: case.update(spans=value["samples"][0]["spans"]),
    )
    result = client.collect()
    assert result["analysis_backend"] == "flow1"
    assert result["selection"]["method"] == "uniform_random_without_replacement"
    assert "including pending/failed" in result["selection"]["frame"]
    assert SIGNAL in queries[0] and "signal_version=1" in queries[0]
    assert result["samples"][0]["flow1_runs"][0]["run_id"] == RUN


def test_lifecycle_only_trace_does_not_become_a_clean_analysis():
    value = frozen(False)
    case = value["samples"][0]
    case["spans"][0].update(name="automation.conversation", input="", output="")
    with pytest.raises(ValueError, match="Insufficient"):
        findings(value, case)


def test_zero_usage_completed_run_does_not_prove_model_analysis():
    value = frozen(False)
    value["samples"][0]["flow1_runs"][0].update(input_tokens=0, output_tokens=0)
    with pytest.raises(ValueError, match="no recorded model usage"):
        findings(value, value["samples"][0])


def test_shared_webhook_trace_cannot_be_attributed_to_one_automation():
    value = frozen()
    case = value["samples"][0]
    case["flow1_trace_sessions"][0]["conversation_ids"].append(str(uuid.UUID(int=99)))
    with pytest.raises(ValueError, match="shared across conversations"):
        findings(value, case)


@pytest.mark.parametrize(
    "disposition", ["unsupported", "other_topic", "already_tracked", "invalid", None]
)
def test_topic_rejections_are_typed_and_preserve_separate_issue_dispositions(
    tmp_path, monkeypatch, disposition
):
    value = frozen()
    value["topic"] = {
        "id": "1" * 20,
        "key": "one-issue",
        "title": "One issue",
        "representative_event_ids": [EVENT],
    }
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(value))

    def model(args, model, policy, context, name, schema):
        assert context["repair_topic"] == value["topic"]
        assert schema["properties"]["rejections"]["items"]["properties"]["disposition"][
            "enum"
        ] == ["unsupported", "other_topic", "already_tracked"]
        rejection = {
            "finding_id": context["findings"][0]["id"],
            "reason": "Evidence belongs to another independently reviewable issue",
        }
        if disposition is not None:
            rejection["disposition"] = disposition
        return {"proposals": [], "rejections": [rejection]}, {}

    monkeypatch.setattr(app, "call_model", model)
    args = arguments(tmp_path / "review", input=path)
    if disposition in ("invalid", None):
        with pytest.raises(ValueError, match="Partial analysis"):
            app.run(args)
    else:
        app.run(args)
        report = json.loads((args.output / "report.json").read_text())
        assert not report["errors"]
        assert report["rejected_findings"][0]["disposition"] == disposition


@pytest.mark.parametrize("content", ["", "New execution content"])
def test_late_empty_conversation_wrapper_does_not_invalidate_unchanged_execution(
    content,
):
    value = frozen()
    case = value["samples"][0]
    case["spans"].append(
        case["spans"][0]
        | {
            "ref": "s2",
            "span_id": str(uuid.UUID(int=91)),
            "name": "conversation",
            "span_type": "DEFAULT",
            "input": "",
            "output": content,
            "end_time": "2026-10-22T01:00:00Z",
        }
    )
    if content:
        with pytest.raises(ValueError, match="Trace changed"):
            findings(value, case)
    else:
        result, measurement = findings(value, case)
        assert result["findings"] and measurement["late_lifecycle_span_refs"] == ["s2"]


def test_partial_preflight_never_starts_expensive_coding(tmp_path, monkeypatch):
    value = frozen()
    other = copy.deepcopy(value["samples"][0])
    other.update(
        case_id="c2",
        conversation_id=str(uuid.UUID(int=92)),
        collection_error="Unavailable original trace",
    )
    value["samples"].append(other)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(
        app,
        "coding_coordinate",
        lambda *a: pytest.fail("Do not code a repair that cannot be published"),
    )
    monkeypatch.setattr(
        app,
        "call_model",
        lambda *a: pytest.fail("Do not spend inference on failed preflight"),
    )
    with pytest.raises(ValueError, match="Partial analysis"):
        app.run(arguments(tmp_path / "analysis", input=path))
    report = json.loads((tmp_path / "analysis/report.json").read_text())
    assert report["findings"] and report["errors"][0]["case_id"] == "c2"
    assert not report["proposals"]
