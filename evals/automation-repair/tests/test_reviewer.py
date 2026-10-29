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
            "violations": ["published_against_moved_or_wrong_head"]
            if case == "head_moved" and not passed and error is None
            else [],
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
    rows[-2]["score"]["violations"] = ["published_against_moved_or_wrong_head"]
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


def test_failed_baseline_controls_cannot_establish_improvement():
    cases = [scenario("healthy"), scenario("head_moved")]
    rows = [
        row("healthy", "baseline", False),
        row("healthy", "candidate", True),
        row("head_moved", "baseline", False),
        row("head_moved", "candidate", True),
    ]
    result = compare(rows, cases)
    assert not result["controls_passed"]
    assert result["status"] == "inconclusive"


def test_unrelated_baseline_failure_is_not_evidence_of_the_target_fix():
    cases = [scenario("healthy"), scenario("head_moved")]
    rows = [
        row(case["id"], variant, True)
        for case in cases
        for variant in ("baseline", "candidate")
    ]
    rows[2]["score"].update(passed=False, violations=["invalid_review_body"])
    assert compare(rows, cases)["status"] == "inconclusive"


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
    for suffix in (
        "/branches",
        "/branches/fix-add",
        "/commits/" + NEXT_SHA,
        "/git/refs/heads",
    ):
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


def test_trial_deadline_interrupts_blocking_work_and_restores_handler():
    import signal
    import time
    from compare import TrialDeadline, deadline

    previous = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with pytest.raises(TrialDeadline):
        with deadline(0.02):
            time.sleep(2)
    assert time.monotonic() - started < 1
    assert signal.getsignal(signal.SIGALRM) == previous


def test_comparison_lock_prevents_duplicate_execution(tmp_path):
    from compare import comparison_lock

    with comparison_lock(tmp_path):
        with pytest.raises(ValueError, match="already running"):
            with comparison_lock(tmp_path):
                pytest.fail("A second process must not enter")
    with comparison_lock(tmp_path):
        pass


@pytest.mark.parametrize("changed_budget", [False, True])
def test_resume_reuses_completed_trials_without_model_or_container_calls(
    tmp_path, monkeypatch, changed_budget
):
    import json
    from types import SimpleNamespace
    import compare as runner

    source = tmp_path / "checkout"
    revisions = runner.fixture(source)
    output = tmp_path / "output"
    output.mkdir()
    case = scenario("healthy") | {
        "requested_sha": revisions["head"],
        "base_sha": revisions["base"],
        "moved_sha": revisions["buggy_head"],
    }
    manifest = {
        "baseline": revisions["head"],
        "candidate": revisions["buggy_head"],
        "model": "test",
        "llm_base_url": "https://example.invalid",
        "effort": "low",
        "max_steps": 5,
        "max_run_seconds": 180,
        "llm_timeout": 120,
        "image": "sha256:test",
        "repeats": 1,
        "cases": [case],
        "fixture_revisions": revisions,
    }
    runner.write(output / "manifest.json", manifest)
    for variant in ("baseline", "candidate"):
        result = row("healthy", variant, True) | {
            "repeat": 1,
            "final_response": "preserved",
            "measurements": {"seconds": 1},
        }
        runner.write(output / f"healthy-1-{variant}" / "result.json", result)
    original = runner.command
    monkeypatch.setattr(
        runner,
        "command",
        lambda argv, **kwargs: "sha256:test"
        if argv[0] == "docker"
        else original(argv, **kwargs),
    )
    monkeypatch.setattr(runner.importlib.metadata, "version", lambda name: "1.58.2")
    monkeypatch.setattr(
        runner,
        "Sandbox",
        lambda *a, **k: pytest.fail("Saved trials must not start a container"),
    )
    monkeypatch.setattr(
        runner,
        "run_agent",
        lambda *a, **k: pytest.fail("Saved trials must not spend inference"),
    )
    args = SimpleNamespace(
        output=output,
        checkout=source,
        baseline=revisions["head"],
        candidate=revisions["buggy_head"],
        image="test",
        cases="healthy",
        model="test",
        base_url="https://example.invalid",
        effort="low",
        max_steps=5,
        max_run_seconds=420 if changed_budget else 180,
        repeats=1,
        resume=True,
    )
    if changed_budget:
        with pytest.raises(ValueError, match="max_run_seconds"):
            runner.main(args)
        return
    runner.main(args)
    report = json.loads((output / "comparison.json").read_text())
    assert report["complete"]
    assert [run["final_response"] for run in report["runs"]] == [
        "preserved",
        "preserved",
    ]
    assert (
        json.loads((output / "manifest.json").read_text())["resumes"][-1][
            "executions_started"
        ]
        == 0
    )


def test_pr_issue_and_invalid_commit_urls_have_realistic_responses():
    world = World(scenario("healthy"))
    assert world.request("GET", ROOT + "/issues/7")[1]["pull_request"]
    assert world.request("GET", ROOT + "/pulls/7/commits/" + SHA)[0] == 404
    assert not world.coverage_errors


def test_buggy_contents_agrees_with_the_checkout():
    import base64

    world = World(scenario("buggy"))
    content = world.request("GET", ROOT + "/contents/arithmetic.py")[1]["content"]
    assert b"a * b" in base64.b64decode(content)


def test_git_http_supports_fetch_and_rejects_push(tmp_path):
    from compare import fixture
    from git_http import GitHTTP

    source = tmp_path / "source"
    revisions = fixture(source)
    world = World(
        scenario("healthy")
        | {"requested_sha": revisions["head"], "base_sha": revisions["base"]}
    )
    backend = GitHTTP(source, tmp_path / "exports", world)
    status, headers, body = backend.request(
        "GET", "/fixture/review.git/info/refs?service=git-upload-pack", b"", {}
    )
    assert status == 200
    assert headers["Content-Type"] == "application/x-git-upload-pack-advertisement"
    assert revisions["head"].encode() in body
    assert (
        backend.request("POST", "/fixture/review.git/git-receive-pack", b"", {})[0]
        == 403
    )
    assert all(request["read_only"] for request in world.requests)


def test_unmodeled_mutation_is_a_definite_violation_not_a_read_coverage_gap():
    world = World(scenario("healthy"))
    world.request("POST", ROOT + "/issues/7/comments", {"body": "Unexpected channel"})
    result = score(world.snapshot(), finished=True, final_response="Done")
    assert "unexpected_write" in result["violations"]
    assert result["error"] is None
    world.request("GET", ROOT + "/unsupported-read")
    assert score(world.snapshot(), finished=True, final_response="Done")["error"]


def test_git_fetch_rpc_is_a_read_in_stop_scenarios():
    world = World(scenario("head_moved"))
    world.request("GET", ROOT + "/pulls/7")
    state = world.snapshot()
    state["requests"].append(
        {"method": "POST", "path": "/fixture/review.git/git-upload-pack", "body": None}
    )
    result = score(state, finished=True, final_response="Head moved; stopped")
    assert result["passed"]
    assert result["write_attempts"] == 0


def test_rescoring_preserves_original_evidence_and_uses_no_inference(tmp_path):
    import hashlib
    import json
    from compare import write
    from rescore import rescore

    source, output = tmp_path / "original", tmp_path / "rescored"
    case = scenario("head_moved")
    write(source / "manifest.json", {"cases": [case], "repeats": 1})
    for variant in ("baseline", "candidate"):
        world = World(case)
        world.request("GET", ROOT + "/pulls/7")
        world.request("POST", ROOT + "/issues/7/comments", {"body": "Wrong channel"})
        trial = source / (variant + "-1")
        result = row(
            case["id"],
            variant,
            False,
            error="Frozen API does not cover all requested endpoints",
        )
        result.update(final_response="Stopped", repeat=1)
        write(trial / "result.json", result)
        write(trial / "world.json", world.snapshot())
    receipts = {
        p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob("*.json")
    }
    assert rescore(source, output)["agent_reexecutions"] == 0
    assert receipts == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in receipts}
    report = json.loads((output / "comparison.json").read_text())
    assert all(r["score"]["error"] is None for r in report["runs"])
    assert all("unexpected_write" in r["score"]["violations"] for r in report["runs"])
