import copy
import importlib.util
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
    command(["git", "add", "."], checkout)
    command(["git", "commit", "-qm", "baseline"], checkout)
    config = {
        "repository": "Example/automation",
        "automation_ids": ["automation"],
        "checkout": str(checkout),
        "revision": command(["git", "rev-parse", "HEAD"], checkout),
        "base_branch": "main",
        "files": ["value.py"],
        "checks": [
            {
                "name": "regression",
                "argv": [sys.executable, "check.py"],
                "baseline": "fail",
            }
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
    assert (checkout / "value.py").read_text() == "VALUE = 3\n"
    assert "pr_url" not in result
    assert (
        json.loads((args.output / "validation.json").read_text())["status"]
        == result["status"]
    )


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
    with pytest.raises(ValueError, match="allowlist"):
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
    with pytest.raises(ValueError, match="allowlist"):
        propose_fix(args, fix_report(PATCH + extra), {"source": load_source(path)})
