import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import pr_comparison


def request():
    return {
        "baseline": "a" * 40,
        "candidate": "b" * 40,
        "checkout": "/private/existing-repo",
        "changed_files": [pr_comparison.SOURCE + "main.py"],
        "topic": {"key": "wrong-head"},
        "plan": {
            "model": "test-model",
            "base_url": "https://proxy.example.test",
            "topics": {"wrong-head": {"cases": ["healthy", "head_moved"]}},
        },
    }


@pytest.mark.parametrize("targeted", [True, False])
def test_topic_coverage_is_explicit_and_only_targeted_comparison_can_show_improvement(
    tmp_path, monkeypatch, targeted
):
    value = request()
    if not targeted:
        value["topic"]["key"] = "uncovered-body-claim"

    def run(args):
        args.output.mkdir()
        (args.output / "comparison.json").write_text(
            json.dumps({"status": "improvement_on_development_cases"})
        )
        (args.output / "manifest.json").write_text("{}")

    monkeypatch.setattr(pr_comparison.compare, "main", run)
    monkeypatch.setattr(
        pr_comparison, "summarize", lambda root: "side-by-side outcomes"
    )
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value))
    receipt = pr_comparison.run(path, tmp_path / "comparison")
    assert receipt["status"] == (
        "improvement_on_development_cases" if targeted else "inconclusive"
    )
    assert receipt["target_topic_exercised"] == targeted
    assert not receipt["historical_replay"]
    assert "side-by-side outcomes" in receipt["markdown"]


def test_unsupported_automation_never_executes_reviewer_adapter(tmp_path, monkeypatch):
    value = request()
    value["changed_files"] = ["plugins/issue-triage/scripts/main.py"]
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(
        pr_comparison.compare, "main", lambda *a: pytest.fail("Wrong execution adapter")
    )
    result = pr_comparison.run(path, tmp_path / "comparison")
    assert result["status"] == "inconclusive"
    assert "Not evaluated" in result["markdown"]


@pytest.mark.parametrize(
    "cases",
    [["head_moved"], ["healthy"], ["healthy", "healthy"], ["healthy", "not-a-case"]],
)
def test_topic_needs_known_frozen_regression_and_control_cases(cases, tmp_path):
    value = request()
    value["plan"]["topics"]["wrong-head"]["cases"] = cases
    with pytest.raises((ValueError, KeyError)):
        pr_comparison.settings(value, tmp_path)
