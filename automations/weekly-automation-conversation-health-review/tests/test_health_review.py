import hashlib
import importlib.util
import json
import random
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "tarball" / "health_review.py"
spec = importlib.util.spec_from_file_location("health_review", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)
CONVERSATION = "00000000-0000-4000-8000-000000000001"


def episode(index, output="ok", code=0):
    return [
        {
            "id": f"action-{index}",
            "timestamp": f"2026-10-10T00:{index:02}:00Z",
            "kind": "ActionEvent",
            "action": {"command": f"check {index}"},
            "tool_name": "terminal",
        },
        {
            "id": f"result-{index}",
            "timestamp": f"2026-10-10T00:{index:02}:01Z",
            "kind": "ObservationEvent",
            "action_id": f"action-{index}",
            "observation": {
                "content": [{"text": output}],
                "exit_code": code,
                "is_error": False,
            },
        },
    ]


def finding(ref="e1"):
    return {
        "lens": "Errors",
        "case_id": "c1",
        "support": "observed_error",
        "summary": "Observed failure",
        "outcome": "unknown",
        "cause": "unknown",
        "evidence": [ref],
        "recommendation": "Check the cause",
    }


@pytest.fixture
def native_inputs(tmp_path):
    db_path = tmp_path / "automations.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE automations (id TEXT, name TEXT, enabled INTEGER, trigger TEXT, model TEXT, timeout INTEGER, prompt TEXT)"
        )
        db.execute(
            "INSERT INTO automations VALUES ('automation-1','Example',1,'{}','example',600,'Review a change')"
        )
        db.execute(
            "CREATE TABLE automation_runs (id TEXT,automation_id TEXT,status TEXT,error_detail TEXT,created_at TEXT,started_at TEXT,completed_at TEXT,conversation_id TEXT,cost REAL)"
        )
        db.execute(
            "INSERT INTO automation_runs VALUES ('run-1','automation-1','COMPLETED',NULL,'2026-10-10 00:00:00','2026-10-10 00:00:00','2026-10-10 00:05:00',?,0.01)",
            (CONVERSATION,),
        )
    directory = tmp_path / "dev_conversations" / CONVERSATION.replace("-", "")
    (directory / "events").mkdir(parents=True)
    (directory / "base_state.json").write_text(
        json.dumps(
            {
                "execution_status": "running",
                "agent": {
                    "agent_context": {
                        "skills": [{"name": "example", "content": "catalog"}]
                    }
                },
                "stats": {},
            }
        )
    )
    for index, event in enumerate(
        episode(1, "authentication required", 4) + episode(2, "retry succeeded", 0)
    ):
        (directory / "events" / f"event-{index}.json").write_text(json.dumps(event))
    (tmp_path / "monitor_state_api.json").write_text(
        json.dumps(
            {
                "key": "state",
                "value": {
                    "value": {
                        "last_reviewed_through": "2026-10-09T00:00:00Z",
                        "fingerprints": ["existing"],
                    },
                    "if_version": 4,
                },
            }
        )
    )
    return tmp_path


def test_middle_failure_and_retry_survive_compaction():
    events = [
        event
        for index in range(50)
        for event in episode(
            index, "failed" if index == 25 else "ok", 4 if index == 25 else 0
        )
    ]
    run = {
        "id": "run",
        "automation_id": "automation",
        "conversation_id": CONVERSATION,
        "status": "COMPLETED",
    }
    context, registry = review.summarize_conversation(
        run, {"execution_status": "running"}, events, "compact"
    )
    details = {record["ref"]: record for record in context["timeline"]["detail"]}
    assert details["e26"]["has_failed_result"]
    assert "e27" in details
    assert len(context["timeline"]["rows"]) == 50
    assert context["conversation_status"] == "running"
    assert context["outcome"].startswith("unknown")
    assert registry["e26"]["event_ids"] == ["action-25", "result-25"]


def test_embedded_exit_code_is_distinct_from_tool_exit_code():
    run = {
        "id": "run",
        "automation_id": "automation",
        "conversation_id": CONVERSATION,
        "status": "COMPLETED",
    }
    _, registry = review.summarize_conversation(
        run, {}, episode(1, "TSC_EXIT=137\nKilled", 0), "compact"
    )
    record = registry["e1"]["record"]
    assert record["results"][0]["exit_code"] == 0
    assert record["reported_exit_codes"] == [137]


def test_native_entrypoint_does_not_import_sdk_or_modify_inputs(native_inputs):
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in native_inputs.rglob("*")
        if path.is_file()
    }
    output = native_inputs / "review"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT.with_name("main.py")),
            "--review-host",
            "--db",
            str(native_inputs / "automations.db"),
            "--conversations",
            str(native_inputs / "dev_conversations"),
            "--state-file",
            str(native_inputs / "monitor_state_api.json"),
            "--until",
            "2026-10-11T00:00:00Z",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((output / "review.json").read_text())
    assert report["analysis_status"] == "collection_only"
    assert not report["state_update"]["advance_permitted"]
    assert report["state_update"]["fingerprints"] == ["existing"]
    assert not list((output / "requests").glob("*.json"))
    assert (output / "review.json").stat().st_mode & 0o777 == 0o600
    for path, digest in before.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_rejects_unknown_references_without_losing_valid_candidates():
    valid, rejected = review.separate_findings(
        {"findings": [finding(), finding("invented")]},
        {
            "c1": {
                "e1": {
                    "record": {"text": "failure", "has_failed_result": True},
                    "event_ids": ["event-1"],
                }
            }
        },
    )
    assert len(valid) == 1
    assert len(rejected) == 1
    assert "unknown evidence" in rejected[0]["reason"]


def test_case_identity_prevents_cross_conversation_citations():
    with pytest.raises(ValueError, match="unknown evidence"):
        review.validate_findings(
            {"findings": [finding()]}, {"other-conversation": {"e1": {}}}
        )


def test_redaction_preserves_variable_bindings_and_paths(monkeypatch):
    credential = "example-sensitive-credential"
    monkeypatch.setenv("LLM_API_KEY", credential)
    value = review.redact(
        {
            "command": f"GH_TOKEN=${{GITHUB_TOKEN_REVIEW}} gh api; key={credential}",
            "path": "/workspace/a-very-long-path-with-important-source-context-and-file-names.py",
            "api_key": credential,
        }
    )
    assert credential not in str(value)
    assert "${GITHUB_TOKEN_REVIEW}" in value["command"]
    assert value["path"].endswith("file-names.py")


def test_model_context_keeps_only_relevant_automation_rollups():
    context = {
        "conversations": [{"automation_id": "one"}],
        "automation_overview": [
            {"automation_id": "one", "name": "First"},
            {"automation_id": "two", "name": "Second"},
        ],
        "coverage": {},
    }
    result = review.model_context(context)
    assert result["automation_overview"] == [{"name": "First"}]
    assert len(context["automation_overview"]) == 2


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        review.read_database(
            path, review.timestamp("2026-10-09"), review.timestamp("2026-10-10"), 100
        )
    assert not path.exists()


def test_every_model_request_is_recorded_without_credentials(tmp_path, monkeypatch):
    key = "sk-test-sensitive-example-credential"
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid")
    requests = []

    class Response:
        def __init__(self):
            self.headers = {"X-Litellm-Response-Cost": "0.001"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return json.dumps(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "report",
                            "arguments": '{"findings":[]}',
                        }
                    ],
                    "usage": {"input_tokens": 42},
                }
            ).encode()

    def send(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(review.urllib.request, "urlopen", send)
    args = SimpleNamespace(
        api="responses",
        effort="low",
        max_output_tokens=1000,
        max_input_bytes=100000,
        output=tmp_path,
    )
    result, measurement = review.call_model(
        args, "example-model", "Review only", {"evidence": key}, "analysis"
    )
    saved = json.loads((tmp_path / "requests/analysis.json").read_text())
    assert saved == json.loads(requests[0].data)
    assert key not in json.dumps(saved)
    assert result == {"findings": []}
    assert measurement["usage"]["input_tokens"] == 42
    assert measurement["reported_cost_usd"] == "0.001"


def test_input_budget_prevents_inference(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Oversized inputs must not invoke a provider")

    monkeypatch.setattr(review.urllib.request, "urlopen", fail)
    args = SimpleNamespace(
        api="responses",
        effort="low",
        max_output_tokens=1000,
        max_input_bytes=1,
        output=tmp_path,
    )
    with pytest.raises(ValueError, match="byte budget"):
        review.call_model(args, "example-model", "Review", {}, "analysis")


def test_unfinished_work_and_token_counts_are_not_observed_errors():
    registries = {
        "c1": {
            "e1": {"record": {"missing_result": True, "has_failed_result": False}},
            "stats": {"record": {"prompt_tokens": 1000000}},
        }
    }
    with pytest.raises(ValueError, match="lacks a failed"):
        review.validate_findings({"findings": [finding()]}, registries)
    candidate = finding("stats")
    candidate["support"] = "measured_regression"
    with pytest.raises(ValueError, match="baseline"):
        review.validate_findings({"findings": [candidate]}, registries)


def test_focused_verification_keeps_middle_error_lines():
    text = (
        "ordinary output\n" * 100
        + "FATAL ERROR: JavaScript heap out of memory\n"
        + "ordinary output\n" * 100
    )
    focused = review.focused_excerpt(text)
    assert "FATAL ERROR: JavaScript heap out of memory" in focused
    assert len(focused) < len(text)


def test_replay_prefers_the_captured_monitor_policy(native_inputs):
    policy = SCRIPT.with_name("prompt.txt").read_text() + "\nCaptured policy marker\n"
    (native_inputs / "monitor_prompt.txt").write_text(policy)
    output = native_inputs / "replay"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT.with_name("main.py")),
            "--replay",
            str(native_inputs),
            "--until",
            "2026-10-11T00:00:00Z",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((output / "review.json").read_text())
    assert report["policy_sha256"] == hashlib.sha256(policy.encode()).hexdigest()


def collection_args(root, **overrides):
    return SimpleNamespace(
        **(
            {
                "replay": root,
                "review_host": False,
                "db": None,
                "conversations": None,
                "state_file": None,
                "since": None,
                "until": "2026-10-11T00:00:00Z",
                "row_limit": 100,
                "conversation_limit": 3,
                "conversation_id": [],
                "layout": "compact",
                "seed": 42,
            }
            | overrides
        )
    )


def test_uniform_sample_is_over_unique_conversations_not_runs(native_inputs):
    original = native_inputs / "dev_conversations" / CONVERSATION.replace("-", "")
    ids = [CONVERSATION]
    with sqlite3.connect(native_inputs / "automations.db") as db:
        for index in range(2, 7):
            identifier = f"00000000-0000-4000-8000-{index:012}"
            ids.append(identifier)
            shutil.copytree(original, original.parent / identifier.replace("-", ""))
            db.execute(
                "INSERT INTO automation_runs SELECT ?,automation_id,status,error_detail,created_at,started_at,completed_at,?,cost FROM automation_runs WHERE id='run-1'",
                (f"run-{index}", identifier),
            )
        for index in range(20):
            db.execute(
                "INSERT INTO automation_runs SELECT ?,automation_id,'FAILED','bad','2026-10-10 23:00:00',started_at,completed_at,conversation_id,999 FROM automation_runs WHERE id='run-1'",
                (f"duplicate-{index}",),
            )
    first, _, _ = review.collect(collection_args(native_inputs))
    expected = random.Random(42).sample(sorted(ids), 3)
    assert first["selection"]["selected_conversation_ids"] == expected
    assert first["selection"]["population_size"] == 6
    assert first["selection"]["inclusion_probability"] == 0.5
    with sqlite3.connect(native_inputs / "automations.db") as db:
        db.execute(
            "UPDATE automation_runs SET status='COMPLETED', cost=0, error_detail=NULL"
        )
    second, _, _ = review.collect(collection_args(native_inputs))
    assert second["selection"] == first["selection"]
    third, _, _ = review.collect(collection_args(native_inputs, seed=43))
    assert third["selection"]["selected_conversation_ids"] != expected


def test_random_sample_reports_missing_frame_and_records_generated_seed(native_inputs):
    with sqlite3.connect(native_inputs / "automations.db") as db:
        db.execute(
            "INSERT INTO automation_runs SELECT 'missing',automation_id,status,error_detail,created_at,started_at,completed_at,'00000000-0000-4000-8000-000000000009',cost FROM automation_runs WHERE id='run-1'"
        )
    context, _, _ = review.collect(collection_args(native_inputs, seed=None))
    assert isinstance(context["selection"]["seed"], int)
    assert context["selection"]["population_size"] == 1
    assert context["coverage"]["missing_conversation_run_records"] == 1
    assert context["window"]["since"] == "2026-10-04T00:00:00+00:00"
    assert not context["coverage"]["full_conversation_coverage"]


@pytest.fixture
def flow1(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import flow1_review

    return flow1_review


def test_flow1_native_export_retains_full_results_and_native_links(
    native_inputs, flow1
):
    directory = native_inputs / "dev_conversations" / CONVERSATION.replace("-", "")
    state, events = review.read_conversation(directory)
    events += [
        {
            "id": "task",
            "kind": "MessageEvent",
            "timestamp": "2026-10-10T00:00:00Z",
            "llm_message": {
                "role": "user",
                "content": [{"text": "Report test failures honestly."}],
            },
        },
        {
            "id": "answer",
            "kind": "MessageEvent",
            "timestamp": "2026-10-10T00:03:00Z",
            "llm_message": {
                "role": "assistant",
                "content": [{"text": "All tests passed."}],
            },
        },
        {"id": "stats", "kind": "StatsUpdate", "timestamp": "2026-10-10T00:03:01Z"},
    ]
    text = "ordinary output\n" * 1000 + "MIDDLE_FAILURE" + "ordinary output\n" * 1000
    events[1]["observation"]["content"] = [{"text": text}]
    context, registry = review.summarize_conversation(
        {
            "id": "run-1",
            "automation_id": "automation-1",
            "conversation_id": CONVERSATION,
            "status": "COMPLETED",
        },
        state,
        events,
        "compact",
    )
    spans, manifest = flow1.native_spans(context, state, events, registry)
    assert "MIDDLE_FAILURE" in json.dumps(spans)
    assert text in json.dumps(spans).replace("\\n", "\n")
    assert "action-1" in manifest["references"]["e1"]["native_event_ids"]
    assert manifest["omitted_event_kinds"] == {"StatsUpdate": 1}
    assert any(
        item["spanType"] == "LLM" and item["input"]["evidence_ref"].startswith("m")
        for item in spans
    )
    assert spans[-1]["output"]["task_outcome"] == "unknown"


def test_flow1_cost_splits_cached_input_and_preserves_unknown_usage(flow1):
    cost = flow1.priced_usage(
        {
            "totalInputTokens": 1_000_000,
            "totalCachedTokens": 200_000,
            "totalOutputTokens": 100_000,
        }
    )
    assert float(cost["usd"]) == 0.072
    assert flow1.priced_usage({}) is None
    assert (
        flow1.priced_usage(
            {"totalInputTokens": 1, "totalCachedTokens": 5, "totalOutputTokens": 0}
        )["fresh_input_tokens"]
        == 0
    )


def test_compact_context_avoids_duplicate_messages_but_verifies_earlier_citations(
    flow1,
):
    events = [
        {
            "id": "task",
            "kind": "MessageEvent",
            "timestamp": "2026-10-10T00:00:00Z",
            "llm_message": {"role": "user", "content": [{"text": "Report honestly"}]},
        }
    ]
    events.extend(
        {
            "id": f"message-{index}",
            "kind": "MessageEvent",
            "timestamp": "2026-10-10T00:00:01Z",
            "llm_message": {
                "role": "assistant",
                "content": [{"text": f"Update {index}"}],
            },
        }
        for index in range(7)
    )
    case, registry = review.summarize_conversation(
        {
            "id": "run",
            "automation_id": "a1",
            "conversation_id": CONVERSATION,
            "status": "COMPLETED",
        },
        {},
        events,
        "compact",
    )
    case["case_id"] = "c1"
    context = {"conversations": [case], "automation_overview": []}
    compact = review.model_context(context)["conversations"][0]
    assert "assistant_final_messages_unverified" not in compact
    assert [item["ref"] for item in compact["message_evidence"]] == ["m7", "m8"]
    candidate = finding("m2") | {
        "support": "policy_violation",
        "evidence": ["task", "m2"],
    }
    verified = review.verification_context(context, [candidate], {"c1": registry})
    assert [
        item["ref"] for item in verified["conversations"][0]["message_evidence"]
    ] == ["m2", "m7", "m8"]
    spans, _ = flow1.native_spans(case, {}, events, registry)
    assert sum(span["spanType"] == "LLM" for span in spans) == 7


def test_flow1_citations_resolve_only_within_the_imported_trace(flow1):
    manifest = {
        "references": {
            "e1": {
                "span_id": "00000000-0000-0000-8000-000000123456",
                "span_type": "TOOL",
            },
            "m2": {
                "span_id": "00000000-0000-0000-8000-000000234567",
                "span_type": "LLM",
            },
        }
    }
    registry = {"task": {}, "e1": {}, "m2": {}}
    assert flow1.resolve_references("task, 123456, 234568, e1", manifest, registry) == [
        "task",
        "e1",
        "m2",
    ]
    assert flow1.resolve_references(
        "00000000-0000-0000-8000-000000123456", manifest, registry
    ) == ["e1"]
    for value in ("abcdef", "123457"):
        with pytest.raises(ValueError, match="unknown or ambiguous"):
            flow1.resolve_references(value, manifest, registry)
    manifest["references"]["e2"] = {
        "span_id": "00000000-0000-0000-9000-000000123456",
        "span_type": "TOOL",
    }
    with pytest.raises(ValueError, match="ambiguous"):
        flow1.resolve_references("123456", manifest, registry | {"e2": {}})


def flow1_args(root, **overrides):
    config = root / "local-project.json"
    config.write_text(
        json.dumps(
            {
                "projectId": CONVERSATION,
                "profileId": CONVERSATION,
                "ingestKey": "test-ingest",
            }
        )
    )
    return SimpleNamespace(
        **(
            {
                "replay": root,
                "conversations": None,
                "flow1_config": config,
                "flow1_profile_id": None,
                "flow1_api_url": "http://127.0.0.1:18000",
                "flow1_frontend_url": "http://127.0.0.1:15667",
                "max_trace_bytes": 2000000,
                "output": root / "analysis",
                "flow1_max_steps": 4,
                "flow1_output_tokens": 4096,
            }
            | overrides
        )
    )


@pytest.mark.parametrize("changed", [False, True])
def test_flow1_rejects_changed_or_oversized_selected_trace_without_resampling(
    native_inputs, flow1, monkeypatch, changed
):
    context, registries, _ = review.collect(collection_args(native_inputs))
    args = flow1_args(native_inputs, max_trace_bytes=1 if not changed else 2000000)
    if changed:
        path = (
            native_inputs
            / "dev_conversations"
            / CONVERSATION.replace("-", "")
            / "base_state.json"
        )
        state = json.loads(path.read_text())
        state["execution_status"] = "finished"
        path.write_text(json.dumps(state))
    analyzer = flow1.Flow1Analyzer(args)
    monkeypatch.setattr(
        analyzer, "request", lambda *a, **k: pytest.fail("Must not upload or infer")
    )
    result, measurement = analyzer.analyze(
        context["conversations"][0], registries["c1"], "Review"
    )
    assert result == {"findings": []}
    assert measurement["status"] == "error"
    assert "not resampled" in measurement["error"]


@pytest.mark.parametrize("signal_result", ["noEvent", "skipped", "other"])
def test_flow1_distinguishes_no_event_from_incomplete_analysis_and_retains_cost(
    native_inputs, flow1, monkeypatch, signal_result
):
    context, registries, _ = review.collect(collection_args(native_inputs))
    analyzer = flow1.Flow1Analyzer(flow1_args(native_inputs))
    calls = []

    def request(origin, path, *args, **kwargs):
        calls.append(path)
        if path.endswith("signal-test"):
            return {
                "result": signal_result,
                "stats": {
                    "totalInputTokens": 100,
                    "totalCachedTokens": 20,
                    "totalOutputTokens": 10,
                    "totalCostUsd": 0.0000072,
                },
            }
        return {"endTime": "2026-10-10T00:05:00Z"}

    monkeypatch.setattr(analyzer, "request", request)
    result, measurement = analyzer.analyze(
        context["conversations"][0], registries["c1"], "Review"
    )
    assert result == {"findings": []}
    assert measurement["status"] == (
        "noEvent" if signal_result == "noEvent" else "error"
    )
    assert float(measurement["estimated_cost"]["usd"]) == 0.0000072
    assert measurement["reported_cost_usd"] == "7.2e-06"
    assert sum(path.endswith("signal-test") for path in calls) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "https://api.lmnr.ai",
        "http://example.com",
        "http://localhost/path",
        "http://user:password@localhost",
        "http:///",
    ],
)
def test_flow1_internal_routes_are_confined_to_local_instance(flow1, origin):
    with pytest.raises(ValueError, match="loopback"):
        flow1.local_origin(origin)
