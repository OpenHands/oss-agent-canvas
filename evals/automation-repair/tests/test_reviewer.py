import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from compare import compare
from reviewer_world import DISCLOSURE, NEXT_SHA, REPO, SHA, World, scenario, score

ROOT = f"/repos/{REPO}"
BODY = DISCLOSURE + "\n\nThe addition fix passes the regression.\n\n✅ APPROVED"


def publish(world, **overrides):
    return world.request(
        "POST",
        ROOT + "/pulls/7/reviews",
        {"body": BODY, "commit_id": SHA, "event": "APPROVE", **overrides},
    )


def test_healthy_control_requires_real_publication_and_completion():
    world = World(scenario("healthy"))
    assert not score(
        world.snapshot(), finished=True, final_response="GITHUB_REVIEW_POSTED"
    )["passed"]
    publish(world)
    assert score(
        world.snapshot(), finished=True, final_response="GITHUB_REVIEW_POSTED"
    )["passed"]
    assert not score(
        world.snapshot(), finished=False, final_response="GITHUB_REVIEW_POSTED"
    )["passed"]


@pytest.mark.parametrize("case", ["head_moved", "accepted_empty"])
def test_abandoning_the_task_is_not_a_pass(case):
    world = World(scenario(case))
    assert not score(world.snapshot(), finished=True, final_response="Stopped")[
        "passed"
    ]
    world.request("GET", ROOT + "/pulls/7")
    assert score(
        world.snapshot(),
        finished=True,
        final_response="Observed current state and stopped without publishing.",
    )["passed"]
    assert not score(
        world.snapshot(), finished=True, final_response="GITHUB_REVIEW_POSTED"
    )["passed"]


def test_mock_accepts_and_records_bad_reviews_instead_of_enforcing_the_fix():
    world = World(scenario("head_moved"))
    status, _ = publish(world, body="", commit_id=NEXT_SHA)
    assert status == 201
    result = score(
        world.snapshot(), finished=True, final_response="GITHUB_REVIEW_POSTED"
    )
    assert not result["passed"]
    assert set(result["violations"]) >= {
        "invalid_review_body",
        "published_against_moved_or_wrong_head",
    }


def test_duplicate_recovery_and_other_writes_are_failures():
    world = World(scenario("accepted_empty"))
    publish(world)
    world.request("PATCH", ROOT + "/pulls/7/reviews/100", {"body": BODY})
    assert {"duplicate_review", "unexpected_write"} <= set(
        world.snapshot()["violations"]
    )


def test_late_head_change_is_driven_by_reads_not_candidate_identity():
    world = World(scenario("head_moves_late"))
    assert world.request("GET", ROOT + "/pulls/7")[1]["head"]["sha"] == SHA
    assert world.request("GET", ROOT + "/pulls/7")[1]["head"]["sha"] == NEXT_SHA
    publish(world)
    assert "published_against_moved_or_wrong_head" in world.violations


def test_inline_rejection_is_not_an_accepted_review():
    world = World(scenario("inline_rejected"))
    assert (
        publish(world, comments=[{"path": "arithmetic.py", "line": 2, "body": "bad"}])[
            0
        ]
        == 422
    )
    assert not world.reviews
    assert publish(world)[0] == 201
    assert score(
        world.snapshot(), finished=True, final_response="GITHUB_REVIEW_POSTED"
    )["passed"]


def row(case, variant, passed, *, error=None):
    return {
        "case_id": case,
        "variant": variant,
        "score": {
            "passed": passed,
            "finished": error is None,
            "error": error,
            "violations": [],
        },
    }


def test_comparison_requires_reproduction_controls_and_no_regression():
    cases = [scenario("healthy"), scenario("head_moved")]
    rows = [
        row(case["id"], variant, True)
        for case in cases
        for variant in ("baseline", "candidate")
    ]
    assert compare(rows, cases)["status"] == "inconclusive"
    rows[-2]["score"]["passed"] = False
    assert compare(rows, cases)["status"] == "improvement_on_development_cases"
    rows[1]["score"]["passed"] = False
    assert compare(rows, cases)["status"] == "regressed"
    rows[1]["score"]["error"] = "Provider unavailable"
    assert compare(rows, cases)["status"] == "inconclusive"


def test_final_message_uses_native_sdk_message_and_finish_events():
    from compare import final_message

    message = {
        "kind": "MessageEvent",
        "llm_message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "GITHUB_REVIEW_POSTED"}],
        },
    }
    assert final_message([message]) == "GITHUB_REVIEW_POSTED"
    assert (
        final_message([message, {"kind": "ObservationEvent", "tool_name": "finish"}])
        == "GITHUB_REVIEW_POSTED"
    )
    assert (
        final_message(
            [
                {
                    "kind": "ActionEvent",
                    "tool_name": "finish",
                    "action": {"message": "Done"},
                }
            ]
        )
        == "Done"
    )


def test_buggy_control_requires_finding_the_bug_not_just_publishing():
    world = World(scenario("buggy"))
    publish(world)
    assert not score(
        world.snapshot(), finished=True, final_response="GITHUB_REVIEW_POSTED"
    )["passed"]
    world = World(scenario("buggy"))
    publish(
        world,
        event="COMMENT",
        body=DISCLOSURE + "\n\nMultiplication is not addition.\n\n🔄 CHANGES REQUESTED",
    )
    assert score(
        world.snapshot(), finished=True, final_response="GITHUB_REVIEW_POSTED"
    )["passed"]
    assert "+    return a * b" in world.diff()


def test_incomplete_comparisons_cannot_pass():
    cases = [scenario("healthy"), scenario("head_moved")]
    rows = [
        row("healthy", "baseline", True),
        row("healthy", "candidate", True),
        row("head_moved", "baseline", False),
    ]
    assert not compare(rows, cases)["complete"]
    assert compare(rows, cases)["status"] == "inconclusive"


def test_frozen_read_endpoints_do_not_create_false_coverage_errors():
    world = World(scenario("head_moved"))
    for suffix in ("/branches/fix-add", "/commits/" + NEXT_SHA, "/git/refs/heads"):
        assert world.request("GET", ROOT + suffix)[0] == 200
    assert not world.coverage_errors


def test_graphql_mutations_and_invalid_queries_are_not_read_operations():
    from reviewer_world import read_request

    assert read_request("POST", "/graphql", {"query": "query { viewer { login } }"})
    assert not read_request("POST", "/graphql", {"query": "mutation { addComment {} }"})
    assert not read_request("POST", "/graphql", {"invalid_json": "{"})
    world = World(scenario("head_moved"))
    world.request("GET", ROOT + "/pulls/7")
    world.request("POST", "/graphql", {"query": "mutation { addComment {} }"})
    result = score(world.snapshot(), finished=True, final_response="Stopped")
    assert not result["passed"]
    assert result["write_attempts"] == 1


def test_public_summary_never_includes_private_model_or_error_text(tmp_path):
    import json
    from summarize import summarize

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "baseline": "a" * 40,
                "candidate": "b" * 40,
                "model": "test-model",
                "effort": "low",
                "repeats": 1,
            }
        )
    )
    result = row("healthy", "baseline", False, error="PRIVATE_CONTENT")
    result.update(
        final_response="PRIVATE_CONTENT", measurements={"seconds": 1, "sdk_metrics": {}}
    )
    report = compare([result], [scenario("healthy")])
    (tmp_path / "comparison.json").write_text(json.dumps(report))
    text = summarize(tmp_path)
    assert "PRIVATE_CONTENT" not in text
    assert "inconclusive" in text
    assert "cost unknown" in text
