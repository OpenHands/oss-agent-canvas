import hashlib
import importlib.util
import json
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
