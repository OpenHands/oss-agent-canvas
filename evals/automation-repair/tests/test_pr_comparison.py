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
    value["changed_files"] = ["plugins/slack-monitor/scripts/main.py"]
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


@pytest.mark.parametrize("changed_budget", [False, True])
def test_recorded_native_executions_are_rescored_without_model_calls(
    tmp_path, monkeypatch, changed_budget
):
    from reviewer_world import World, REPO, DISCLOSURE, SHA
    from scenarios import scenario
    from compare import write

    value = request()
    value["topic"]["key"] = "ground-review-claims"
    value["plan"].update(automatic_contracts=True, llm_timeout=120)
    value["plan"].pop("topics")
    recorded = tmp_path / "original"
    cases = [
        scenario(name)
        for name in ("review_body_contradiction", "review_body_absence_control")
    ]
    write(
        recorded / "manifest.json",
        {
            "baseline": value["baseline"],
            "candidate": value["candidate"],
            "model": "test-model",
            "llm_base_url": value["plan"]["base_url"],
            "effort": "low",
            "repeats": 1,
            "max_steps": 30,
            "max_run_seconds": 180,
            "llm_timeout": 120,
            "cases": cases,
        },
    )
    for case in cases:
        for variant in ("baseline", "candidate"):
            world = World(case)
            text = "Multiplication breaks addition."
            if case["kind"] == "regression" and variant == "baseline":
                text += (
                    " The PR's How to Test does not mention test_acp_conformance.py."
                )
            world.request(
                "POST",
                f"/repos/{REPO}/pulls/7/reviews",
                {
                    "body": DISCLOSURE + "\n\n" + text + "\n\n🔄 CHANGES REQUESTED",
                    "event": "COMMENT",
                    "commit_id": SHA,
                },
            )
            directory = recorded / (case["id"] + "-1-" + variant)
            write(directory / "world.json", world.snapshot())
            write(
                directory / "result.json",
                {
                    "case_id": case["id"],
                    "repeat": 1,
                    "variant": variant,
                    "score": {"error": None, "finished": True},
                    "final_response": "GITHUB_REVIEW_POSTED",
                    "measurements": {"seconds": 1},
                },
            )
    value["plan"]["recorded_runs"] = str(recorded)
    if changed_budget:
        value["plan"]["max_run_seconds"] = 420
    path = tmp_path / "request.json"
    write(path, value)
    monkeypatch.setattr(
        pr_comparison.compare, "main", lambda *args: pytest.fail("No new inference")
    )
    if changed_budget:
        with pytest.raises(ValueError, match="max_run_seconds"):
            pr_comparison.run(path, tmp_path / "comparison")
        return
    result = pr_comparison.run(path, tmp_path / "comparison")
    assert result["status"] == "improvement_on_development_cases"
    assert result["reused_executions"]
    assert "no agent run was repeated" in result["markdown"]
