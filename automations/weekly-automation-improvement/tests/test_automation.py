import copy
import importlib.util
import io
import json
import random
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

TARBALL = Path(__file__).parents[1] / "tarball"
sys.path.insert(0, str(TARBALL))
spec = importlib.util.spec_from_file_location("improvement", TARBALL / "main.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
encode = app.encode
Laminar = app.Laminar
message_projection = importlib.import_module("laminar").message_projection
patch_module = importlib.import_module("patches")
command = patch_module.command
load_source = patch_module.load_source
propose_fix = patch_module.propose_fix


def arguments(tmp_path, **overrides):
    values = dict(
        output=tmp_path,
        project_id=str(uuid.UUID(int=1)),
        app_url="https://example.test",
        since="2026-10-10T00:00:00Z",
        until="2026-10-17T00:00:00Z",
        population_limit=100,
        seed=42,
        limit=3,
        max_spans=1000,
        max_sample_bytes=2000000,
        max_response_bytes=8000000,
        input=None,
        source_config=None,
        collect_only=False,
        model="small",
        coordinator="large",
        api="responses",
        effort="low",
        max_input_bytes=750000,
        max_output_tokens=4096,
        evaluate_fix=False,
        publish_fix=False,
    )
    return SimpleNamespace(**(values | overrides))


@pytest.mark.parametrize("proxy_key", ["dedicated-proxy-key", None])
def test_model_credentials_with_inherited_host_key(tmp_path, monkeypatch, proxy_key):
    common = importlib.import_module("common")
    monkeypatch.setenv("LLM_API_KEY", "inherited-host-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://proxy.example.test")
    if proxy_key:
        monkeypatch.setenv("LITELLM_API_KEY", proxy_key)
    else:
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    expected = proxy_key or "inherited-host-key"

    def respond(request, timeout):
        assert request.full_url == "https://proxy.example.test/v1/responses"
        assert request.get_header("Authorization") == "Bearer " + expected
        response = io.BytesIO(
            json.dumps(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "report",
                            "arguments": '{"findings":[]}',
                        }
                    ]
                }
            ).encode()
        )
        response.headers = {}
        return response

    monkeypatch.setattr(common.urllib.request, "urlopen", respond)
    result, _ = common.call_model(
        arguments(tmp_path), "small", "Inspect", {}, "credentials", app.ANALYSIS
    )
    assert result == {"findings": []}
    artifacts = "".join(path.read_text() for path in tmp_path.rglob("*.json"))
    assert expected not in artifacts


def sample():
    return {
        "case_id": "c1",
        "conversation_id": str(uuid.UUID(int=2)),
        "automation_id": "automation",
        "run_id": "run",
        "automation_name": "Example",
        "trace_ids": [str(uuid.UUID(int=3))],
        "initial_instructions": [
            {"role": "user", "content": "Report test failures honestly"}
        ],
        "spans": [
            {
                "ref": "s1",
                "span_id": "00000000-0000-0000-8000-000000123456",
                "parent_span_id": None,
                "trace_id": str(uuid.UUID(int=3)),
                "name": "terminal",
                "span_type": "TOOL",
                "status": "success",
                "input": "pytest -q",
                "output": "FAILED: expected 4, got 3; exit code 1",
                "content_clipped": False,
            }
        ],
        "signals": [],
    }


def snapshot():
    return {
        "schema_version": 1,
        "project_id": str(uuid.UUID(int=1)),
        "app_url": "https://example.test",
        "selection": {"population_sha256": "a" * 64},
        "samples": [sample()],
    }


def candidate():
    return {
        "summary": "Failure hidden",
        "signature": "test-result-reporting",
        "cause": "workflow",
        "outcome": "unresolved",
        "evidence": ["s1"],
        "recommendation": "Report the failure",
    }


def test_random_frame_is_uniform_over_unique_conversations_and_never_replaced(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test-key")
    monkeypatch.setenv("LMNR_BASE_URL", "https://example.test")
    client = Laminar(arguments(tmp_path))
    population = [
        {
            "conversation_id": str(uuid.UUID(int=index)),
            "cost": index**4,
            "status": "FAILED" if index == 1 else "COMPLETED",
        }
        for index in range(1, 7)
    ]
    queries, loaded = [], []

    def query(sql):
        queries.append(sql)
        return list(reversed(population))

    def load(case):
        loaded.append(case["conversation_id"])
        raise ValueError("unavailable selected trace; not resampled")

    monkeypatch.setattr(client, "query", query)
    monkeypatch.setattr(client, "load_sample", load)
    result = client.collect()
    assert loaded == [
        row["conversation_id"] for row in random.Random(42).sample(population, 3)
    ]
    assert len(result["samples"]) == 3
    assert all(case.get("collection_error") for case in result["samples"])
    assert result["selection"]["inclusion_probability"] == 0.5
    assert len(result["selection"]["population_sha256"]) == 64
    assert "GROUP BY conversation_id" in queries[0]
    assert "status" not in queries[0] and "cost" not in queries[0]


def test_message_projection_keeps_behavior_and_removes_reasoning_duplicates():
    original = [
        {
            "role": "assistant",
            "content": "Tests failed",
            "reasoning_content": "large thought" * 100,
            "provider_specific_fields": {"reasoning": "large thought" * 100},
            "tool_calls": [{"function": {"name": "terminal"}}],
        }
    ]
    clean = message_projection(encode(original))
    assert clean == [
        {
            "role": "assistant",
            "content": "Tests failed",
            "tool_calls": [{"function": {"name": "terminal"}}],
        }
    ]
    assert "reasoning" not in encode(clean)


def test_citations_are_trace_scoped_and_ambiguous_ids_fail():
    case = sample()
    assert app.references(["123456", "s1"], case) == ["s1"]
    for ref in ("123457", "abcdef", "s2"):
        with pytest.raises(ValueError, match="Unknown"):
            app.references([ref], case)
    case["spans"].append(
        case["spans"][0]
        | {"ref": "s2", "span_id": "00000000-0000-0000-9000-000000123456"}
    )
    with pytest.raises(ValueError, match="ambiguous"):
        app.references(["123456"], case)


def test_virtual_llm_output_span_resolves_to_original_evidence():
    case = sample()
    case["spans"][0]["span_type"] = "LLM"
    assert app.references(["123457"], case) == ["s1"]


def test_coordinator_sees_cited_evidence_and_recovery():
    case = sample()
    case["spans"].append(
        case["spans"][0]
        | {
            "ref": "s2",
            "span_id": str(uuid.UUID(int=4)),
            "output": "Retry: 12 passed; exit code 0",
        }
    )
    context = app.evidence_context(case, [candidate()])
    assert "FAILED" in encode(context)
    assert "Retry: 12 passed" in encode(context)
    assert case["initial_instructions"] == context["initial_instructions"]
    for index in range(3, 10):
        case["spans"].append(
            case["spans"][0]
            | {
                "ref": f"s{index}",
                "span_id": str(uuid.UUID(int=10 + index)),
                "output": "ordinary later work",
            }
        )
    context = app.evidence_context(case, [candidate()])
    assert "Retry: 12 passed" in encode(context["timeline"])


def test_snapshot_rejects_path_like_case_ids_before_model_calls():
    value = snapshot()
    value["samples"][0]["case_id"] = "../../elsewhere"
    with pytest.raises(ValueError, match="identity"):
        app.validate_snapshot(value, 3)


def test_replay_does_not_read_laminar_and_audits_rejected_candidates(
    tmp_path, monkeypatch
):
    value = snapshot()
    path = tmp_path / "snapshot.json"
    path.write_text(encode(value))
    args = arguments(tmp_path / "review", input=path)
    monkeypatch.setattr(
        app, "Laminar", lambda *args: pytest.fail("Replay must not use Laminar")
    )
    calls = []

    def model(args, model, policy, context, name, schema):
        calls.append(name)
        return {"findings": [candidate() | {"evidence": ["not-present"]}]}, {
            "model": model
        }

    monkeypatch.setattr(app, "call_model", model)
    with pytest.raises(ValueError, match="Partial"):
        app.run(args)
    report = json.loads((args.output / "report.json").read_text())
    assert calls == ["analysis-c1"]
    assert report["status"] == "partial_analysis"
    assert len(report["rejected_candidates"]) == 1
    assert report["findings"] == []


def test_failed_analysis_is_reported_and_does_not_become_a_healthy_case(
    tmp_path, monkeypatch
):
    path = tmp_path / "snapshot.json"
    path.write_text(encode(snapshot()))
    args = arguments(tmp_path / "review", input=path)
    monkeypatch.setattr(
        app,
        "call_model",
        lambda *args: (_ for _ in ()).throw(ValueError("budget exhausted")),
    )
    with pytest.raises(ValueError, match="Partial"):
        app.run(args)
    report = json.loads((args.output / "report.json").read_text())
    assert report["errors"][0]["case_id"] == "c1"
    assert report["publication"] == "disabled"


@pytest.fixture
def source(tmp_path):
    checkout = tmp_path / "repository"
    checkout.mkdir()
    command(["git", "init", "-q"], checkout)
    command(["git", "config", "user.email", "test@example.test"], checkout)
    command(["git", "config", "user.name", "Test"], checkout)
    (checkout / "value.py").write_text("VALUE = 3\n")
    (checkout / "check.py").write_text("from value import VALUE\nassert VALUE == 4\n")
    (checkout / "healthy.py").write_text(
        "from value import VALUE\nassert isinstance(VALUE, int) and 0 < VALUE < 5\n"
    )
    command(["git", "add", "."], checkout)
    command(["git", "commit", "-qm", "baseline"], checkout)
    config = {
        "repository": "Example/automation",
        "automation_ids": ["automation"],
        "checkout": str(checkout),
        "revision": command(["git", "rev-parse", "HEAD"], checkout),
        "base_branch": "main",
        "require_improvement": False,
        "files": ["value.py"],
        "checks": [
            {
                "name": "regression",
                "argv": [sys.executable, "check.py"],
                "baseline": "fail",
            },
            {
                "name": "healthy control",
                "argv": [sys.executable, "healthy.py"],
                "baseline": "pass",
            },
        ],
    }
    path = tmp_path / "source.json"
    path.write_text(encode(config))
    return checkout, path, config


def fix_report(patch):
    return {
        "findings": [
            {
                "id": "c1f1",
                "fingerprint": "stable",
                "cause": "workflow",
                "automation_id": "automation",
            }
        ],
        "proposals": [
            {
                "finding_ids": ["c1f1"],
                "patch": patch,
                "title": "Correct the value",
                "hypothesis": "Off by one",
                "evaluation_plan": "Check expected value",
            }
        ],
    }


PATCH = "diff --git a/value.py b/value.py\n--- a/value.py\n+++ b/value.py\n@@ -1 +1 @@\n-VALUE = 3\n+VALUE = 4\n"


def test_coding_agent_diff_reaches_regression_checks(source, tmp_path, monkeypatch):
    checkout, config_path, _ = source
    value = snapshot()
    value["source"] = load_source(config_path)
    path = tmp_path / "snapshot.json"
    path.write_text(encode(value))
    args = arguments(
        tmp_path / "review", input=path, source_config=config_path, evaluate_fix=True
    )

    monkeypatch.setattr(app, "call_model", lambda *a: ({"findings": [candidate()]}, {}))
    proposal = fix_report(PATCH)["proposals"][0].copy()
    proposal.pop("patch")
    proposal["checks"] = json.loads(config_path.read_text())["checks"]
    monkeypatch.setattr(
        app,
        "coding_coordinate",
        lambda *a: (
            {"proposals": [proposal], "rejections": []},
            {"backend": "openhands-sdk"},
            PATCH,
        ),
    )
    app.run(args)
    report = json.loads((args.output / "report.json").read_text())
    assert report["errors"] == []
    assert report["fix"]["baseline"][0]["exit_code"] != 0
    assert report["fix"]["candidate"][0]["exit_code"] == 0
    assert report["publication"] == "not_published"
    assert (checkout / "value.py").read_text() == "VALUE = 3\n"


def test_fix_validation_uses_isolated_worktrees_and_keeps_baseline_unchanged(
    source, tmp_path
):
    checkout, path, config = source
    args = arguments(tmp_path / "review", source_config=path)
    args.output.mkdir()
    result = propose_fix(args, fix_report(PATCH), {"source": load_source(path)})
    assert result["status"] == "checks_passed_not_task_quality_proven"
    assert result["baseline"][0]["exit_code"] != 0
    assert result["candidate"][0]["exit_code"] == 0
    assert result["baseline"][1]["exit_code"] == 0
    assert result["candidate"][1]["exit_code"] == 0
    assert (checkout / "value.py").read_text() == "VALUE = 3\n"
    assert "pr_url" not in result
    assert (
        json.loads((args.output / "validation.json").read_text())["status"]
        == result["status"]
    )


@pytest.mark.parametrize("keep", ["pass", "fail"])
def test_publication_requires_regression_and_healthy_control(source, tmp_path, keep):
    _, path, config = source
    config["checks"] = [
        check for check in config["checks"] if check["baseline"] == keep
    ]
    path.write_text(encode(config))
    args = arguments(tmp_path / "review", source_config=path, publish_fix=True)
    with pytest.raises(
        ValueError, match="baseline-failing regression and a passing control"
    ):
        propose_fix(args, fix_report(PATCH), {"source": load_source(path)})
    assert not args.output.exists()


def test_patch_outside_configured_allowlist_never_runs_checks(
    source, tmp_path, monkeypatch
):
    _, path, _ = source
    args = arguments(tmp_path / "review", source_config=path)
    args.output.mkdir()
    patch = "diff --git a/check.py b/check.py\n--- a/check.py\n+++ b/check.py\n@@ -1,2 +1,2 @@\n from value import VALUE\n-assert VALUE == 4\n+assert VALUE == 3\n"
    import patches

    monkeypatch.setattr(
        patches, "run_checks", lambda *args: pytest.fail("Must not execute")
    )
    with pytest.raises(ValueError, match="automation scope"):
        propose_fix(args, fix_report(patch), {"source": load_source(path)})


def test_no_improvement_rejects_patch(source, tmp_path):
    _, path, _ = source
    args = arguments(tmp_path / "review", source_config=path)
    args.output.mkdir()
    result = propose_fix(
        args,
        fix_report(PATCH.replace("+VALUE = 4", "+VALUE = 5")),
        {"source": load_source(path)},
    )
    assert result["status"] == "rejected"
    assert result["candidate"][0]["exit_code"] != 0


def test_source_snapshot_retains_exact_file_bytes(source):
    _, path, _ = source
    assert load_source(path)["files"]["value.py"] == "VALUE = 3\n"


def test_laminar_excerpt_aliases_do_not_shadow_original_lengths(tmp_path, monkeypatch):
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test-key")
    monkeypatch.setenv("LMNR_BASE_URL", "https://example.test")
    client = Laminar(arguments(tmp_path))
    queries = []
    row = {
        "span_id": str(uuid.UUID(int=2)),
        "trace_id": str(uuid.UUID(int=3)),
        "parent_span_id": None,
        "name": "terminal",
        "span_type": "TOOL",
        "status": "success",
        "input_excerpt": "pytest",
        "output_excerpt": "x" * 6000,
        "original_input_bytes": 6,
        "original_output_bytes": 12000,
    }

    def query(sql):
        queries.append(sql)
        if "SELECT DISTINCT trace_id" in sql:
            return [{"trace_id": row["trace_id"]}]
        return [copy.deepcopy(row)] if "original_output_bytes" in sql else []

    monkeypatch.setattr(client, "query", query)
    case = sample()
    client.load_sample(case | {"spans": []})
    assert "length(output) AS original_output_bytes" in queries[1]
    assert " AS output," not in queries[1] and " AS input," not in queries[1]


def test_failed_candidate_never_pushes_or_opens_pr(source, tmp_path, monkeypatch):
    _, path, config = source
    args = arguments(tmp_path / "review", source_config=path, publish_fix=True)
    args.output.mkdir()
    import patches

    def run(argv, *a, **k):
        if argv[:2] == ["gh", "api"]:
            return config["revision"]
        if argv[:3] == ["gh", "pr", "list"]:
            return "[]"
        if argv[0] == "gh" or argv[:2] == ["git", "push"]:
            pytest.fail("Rejected fix must not be published")
        return command(argv, *a, **k)

    monkeypatch.setattr(patches, "command", run)
    result = propose_fix(
        args,
        fix_report(PATCH.replace("+VALUE = 4", "+VALUE = 5")),
        {"source": load_source(path)},
    )
    assert result["status"] == "rejected"


def test_source_scope_prevents_patching_another_automation(source, tmp_path):
    _, path, _ = source
    args = arguments(tmp_path / "review", source_config=path)
    args.output.mkdir()
    report = fix_report(PATCH)
    report["findings"][0]["automation_id"] = "another-automation"
    with pytest.raises(ValueError, match="scope"):
        propose_fix(args, report, {"source": load_source(path)})


def test_new_untracked_files_in_patch_are_rejected_before_checks(
    source, tmp_path, monkeypatch
):
    _, path, _ = source
    args = arguments(tmp_path / "review", source_config=path)
    args.output.mkdir()
    import patches

    monkeypatch.setattr(
        patches, "run_checks", lambda *a: pytest.fail("Must not execute")
    )
    extra = "diff --git a/injected.py b/injected.py\nnew file mode 100644\n--- /dev/null\n+++ b/injected.py\n@@ -0,0 +1 @@\n+print('unexpected helper')\n"
    with pytest.raises(ValueError, match="automation scope"):
        propose_fix(args, fix_report(PATCH + extra), {"source": load_source(path)})


def test_model_payload_redaction_preserves_json_and_sanitizes_structured_audits(
    tmp_path, monkeypatch
):
    common = importlib.import_module("common")
    monkeypatch.setenv("LITELLM_API_KEY", "transport-only-credential")
    monkeypatch.setenv("LLM_BASE_URL", "https://proxy.example.test")
    context = {
        "credentials": "private-context-value",
        "body": 'api_key=os.getenv("LLM_API_KEY")\nUse the task.',
    }
    result = {
        "credentials": "private-result-value",
        "quote": 'api_key="example-credential-long"',
    }

    def respond(request, timeout):
        sent = json.loads(request.data)
        assert json.loads(sent["input"][-1]["content"]) == common.redact(context)
        assert "private-context-value" not in request.data.decode()
        response = io.BytesIO(
            json.dumps(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "report",
                            "arguments": json.dumps(result),
                        }
                    ]
                }
            ).encode()
        )
        response.headers = {}
        return response

    monkeypatch.setattr(common.urllib.request, "urlopen", respond)
    actual, _ = common.call_model(
        arguments(tmp_path), "small", "Inspect", context, "roundtrip", app.ANALYSIS
    )
    assert actual == common.redact(result)
    audit = json.loads((tmp_path / "responses/roundtrip.json").read_text())
    assert json.loads(audit["output"][0]["arguments"]) == common.redact(result)
    assert "private-result-value" not in json.dumps(audit)


def test_redaction_preserves_protocol_checksums_but_removes_credentials(monkeypatch):
    from common import redact

    digest = "a1" * 32
    marker = "<!-- triage-source:" + digest + " -->"
    assert redact(marker + " " + digest) == marker + " " + digest
    monkeypatch.setenv("PRIVATE_API_KEY", digest)
    assert digest not in redact(marker)


def test_private_report_preserves_exact_agent_patch(source, tmp_path, monkeypatch):
    _, config_path, _ = source
    value = snapshot()
    value["source"] = load_source(config_path)
    path = tmp_path / "snapshot.json"
    path.write_text(encode(value))
    args = arguments(tmp_path / "review", input=path, source_config=config_path)
    patch = PATCH + "\n" + "a" * 64
    proposal = fix_report(patch)["proposals"][0].copy()
    proposal.pop("patch")
    checks = [
        {
            "name": "selector",
            "argv": ["python", "-k", "arbitrary_long_validation_selector_" * 3],
            "baseline": "fail",
        }
    ]
    proposal["checks"] = checks
    monkeypatch.setattr(app, "call_model", lambda *a: ({"findings": [candidate()]}, {}))
    monkeypatch.setattr(
        app,
        "coding_coordinate",
        lambda *a: ({"proposals": [proposal], "rejections": []}, {}, patch),
    )
    app.run(args)
    report = json.loads((args.output / "report.json").read_text())
    assert report["proposals"][0]["patch"] == patch
    assert report["proposals"][0]["checks"] == checks
    assert (args.output / "report.json").stat().st_mode & 0o077 == 0


def test_sdk_runtime_failure_is_preserved_in_private_report(
    source, tmp_path, monkeypatch
):
    _, config_path, _ = source
    value = snapshot()
    value["source"] = load_source(config_path)
    path = tmp_path / "snapshot.json"
    path.write_text(encode(value))
    args = arguments(tmp_path / "review", input=path, source_config=config_path)
    monkeypatch.setattr(app, "call_model", lambda *a: ({"findings": [candidate()]}, {}))

    def fail(*args):
        raise RuntimeError("Native SDK failure")

    monkeypatch.setattr(app, "coding_coordinate", fail)
    with pytest.raises(ValueError, match="Partial analysis"):
        app.run(args)
    report = json.loads((args.output / "report.json").read_text())
    assert report["errors"] == [{"stage": "coordinator", "error": "Native SDK failure"}]


def test_evidence_checksums_survive_without_exposing_hex_credentials(monkeypatch):
    checksum = "a1" * 32
    secret = "b2" * 32
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", secret)
    value = app.redact(
        {
            "source_sha256": checksum,
            "api_key": checksum,
            "apiKey": checksum,
            "text": f"{checksum} {secret}",
        }
    )
    assert value["source_sha256"] == checksum
    assert value["api_key"] == "[REDACTED]"
    assert value["apiKey"] == "[REDACTED]"
    assert value["text"] == f"{checksum} [REDACTED]"


def test_long_test_selectors_survive_without_allowing_worker_credentials(monkeypatch):
    from common import redact

    selector = "test_transient_then_success_reuses_conversation_without_new_prompt"
    assert redact(["python", "-k", selector])[-1] == selector
    assert redact("def " + selector + "(self):") == "def " + selector + "(self):"
    monkeypatch.setenv("PRIVATE_API_KEY", selector)
    assert redact(selector) == "[REDACTED]"
    assert redact({"api_key": selector})["api_key"] == "[REDACTED]"
    assert redact("xY" * 32) == "[REDACTED]"
