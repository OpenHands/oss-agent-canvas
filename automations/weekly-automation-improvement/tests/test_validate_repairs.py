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


@pytest.mark.parametrize("size,baseline", [(2, "a" * 40), (5, "b" * 40)])
def test_cached_cohort_cannot_change_n_or_baseline(tmp_path, size, baseline):
    (tmp_path / "cohort.json").write_text(
        json.dumps({"sample_size": size, "baseline_revision": baseline})
    )
    body = f"<!-- automation-improvement-automation: {AUTOMATION} -->"
    with pytest.raises(ValueError, match="Cached cohort"):
        validate_repairs.historical_cohort(
            {"validation_sample_size": 5}, body, "a" * 40, tmp_path
        )


def test_revalidation_preserves_add_delete_rename_manifest_without_loading_new_files(
    tmp_path, monkeypatch
):
    import cohorts
    import events
    from budget import RunBudget

    revision = "a" * 40
    manifest = [
        {"filename": "plugins/example/main.py", "status": "modified"},
        {"filename": "plugins/example/helper.py", "status": "added"},
        {"filename": "tests/test_helper.py", "status": "added"},
        {
            "filename": "plugins/example/renamed.py",
            "previous_filename": "plugins/example/old.py",
            "status": "renamed",
        },
        {"filename": "plugins/example/deleted.py", "status": "removed"},
    ]
    pr = {
        "number": 66,
        "head": {"ref": "automation/improve-" + "b" * 20, "sha": "c" * 40},
        "labels": [{"name": "automation-improvement"}],
        "body": f"<!-- automation-improvement-automation: {AUTOMATION} -->\nBaseline revision: {revision}.",
        "base": {"sha": revision},
        "title": "A repair with new files",
        "html_url": "https://github.com/example/repo/pull/66",
    }

    def command(argv, cwd=None):
        if argv[:2] == ["git", "ls-tree"]:
            pytest.fail(
                "Cohort preparation must not load candidate paths from baseline"
            )
        if argv[:2] == ["gh", "api"]:
            if "/files?" in argv[-1]:
                assert "--paginate" in argv
                return json.dumps(manifest[:2]) + "\n" + json.dumps(manifest[2:])
            return json.dumps([pr] if "?state=open" in argv[-1] else pr)
        return ""

    monkeypatch.setattr(validate_repairs, "command", command)
    monkeypatch.setattr(validate_repairs, "load_environment", lambda c: None)
    monkeypatch.setattr(validate_repairs, "github_auth", lambda c: None)
    monkeypatch.setattr(
        validate_repairs, "arguments", lambda *a: arguments(tmp_path / "collection")
    )
    monkeypatch.setattr(validate_repairs, "Laminar", lambda *a: object())
    monkeypatch.setattr(events, "collect_events", lambda *a, **kw: {"events": []})
    monkeypatch.setattr(
        cohorts,
        "collect",
        lambda *a, **kw: {
            "samples": [],
            "errors": [],
            "complete": False,
            "sample_size": 5,
        },
    )
    captured = []
    budget = RunBudget({})

    def compare(config, source, snapshot, results, output, command, budget=None):
        captured.append((source, snapshot, results, budget))
        return {"status": "inconclusive"}

    monkeypatch.setattr(validate_repairs, "after_publication", compare)
    config = {
        "repository": "example/repo",
        "checkout": "/existing",
        "comparative_validation": {"command": [str(tmp_path / "runner.py")]},
    }
    result = validate_repairs.validate(config, tmp_path / "validation", budget=budget)
    assert result[0]["status"] == "inconclusive" and len(captured) == 1
    source, snapshot, results, actual_budget = captured[0]
    assert source["revision"] == revision and actual_budget is budget
    assert results["changed_files"] == [item["filename"] for item in manifest]
    assert results["changed_file_manifest"] == manifest
    assert snapshot["validation_cohort"]["sha256"]


def test_expired_budget_defers_existing_pr_without_fetch_or_inference(
    tmp_path, monkeypatch
):
    from budget import RunBudget

    budget = RunBudget({})
    budget.record(None)
    monkeypatch.setattr(validate_repairs, "load_environment", lambda c: None)
    monkeypatch.setattr(validate_repairs, "github_auth", lambda c: None)
    pr = {
        "number": 1,
        "head": {"ref": "automation/improve-x"},
        "labels": [{"name": "automation-improvement"}],
    }

    def command(argv, *a):
        assert "?state=open" in argv[-1]
        return json.dumps([pr])

    monkeypatch.setattr(validate_repairs, "command", command)
    result = validate_repairs.validate(
        {
            "repository": "example/repo",
            "comparative_validation": {"command": ["runner"]},
        },
        tmp_path / "validation",
        budget=budget,
    )
    assert result[0]["status"] == "deferred_budget"
