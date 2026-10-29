import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from contracts import captured_inputs, select
from reviewer_world import DISCLOSURE, REPO, SHA, World, scenario, score
from triage_world import World as TriageWorld
from triage_world import scenario as triage_case
from triage_world import score as triage_score


@pytest.mark.parametrize("case", ["reported_paid_harm", "reported_harm_unknown_amount"])
def test_reported_harm_requires_medium_or_high_independently_of_readiness(case):
    world = TriageWorld(triage_case(case))
    world.request("GET", f"/repos/{REPO}/issues/7")
    endpoint = f"/repos/{REPO}/issues/7/labels"
    world.request("POST", endpoint, {"labels": ["priority:low"]})
    assert not triage_score(world.snapshot(), finished=True, final_response="Done")[
        "passed"
    ]
    world.request("DELETE", endpoint + "/priority%3Alow")
    world.request("POST", endpoint, {"labels": ["priority:medium"]})
    result = triage_score(world.snapshot(), finished=True, final_response="Done")
    assert result["passed"] and result["final_priority"] == "priority:medium"
    assert "ready-for-dev" not in {label["name"] for label in world.issue["labels"]}


def test_speculation_is_not_rewarded_as_reported_user_harm():
    world = TriageWorld(triage_case("speculative_cost_risk"))
    world.request(
        "POST", f"/repos/{REPO}/issues/7/labels", {"labels": ["priority:high"]}
    )
    assert (
        "incorrect_priority"
        in triage_score(world.snapshot(), finished=True, final_response="Done")[
            "violations"
        ]
    )


def test_optional_research_reads_do_not_invalidate_verified_priority_decisions():
    world = TriageWorld(triage_case("reported_paid_harm"))
    world.request("GET", f"/repos/{REPO}/issues/7")
    world.request("GET", f"/repos/{REPO}/optional-research-resource")
    world.request(
        "POST", f"/repos/{REPO}/issues/7/labels", {"labels": ["priority:medium"]}
    )
    result = triage_score(world.snapshot(), finished=True, final_response="Done")
    assert result["passed"] and result["optional_read_gaps"]


def test_missing_required_issue_input_is_inconclusive_even_with_correct_labels():
    world = TriageWorld(triage_case("reported_paid_harm"))
    world.request(
        "POST", f"/repos/{REPO}/issues/7/labels", {"labels": ["priority:medium"]}
    )
    result = triage_score(world.snapshot(), finished=True, final_response="Done")
    assert not result["passed"] and result["error"]


def test_triage_preserves_human_text_and_unrelated_labels():
    world = TriageWorld(triage_case("reported_paid_harm"))
    world.request(
        "PATCH",
        f"/repos/{REPO}/issues/7",
        {"body": "replaced", "labels": ["priority:high", "ready-for-dev"]},
    )
    result = triage_score(world.snapshot(), finished=True, final_response="Done")
    assert {
        "human_text_changed",
        "unrelated_label_removed",
        "unresolved_product_decision_marked_ready",
    } <= set(result["violations"])


def review(case, text):
    world = World(scenario(case))
    world.request(
        "POST",
        f"/repos/{REPO}/pulls/7/reviews",
        {
            "body": DISCLOSURE + "\n\n" + text + "\n\n🔄 CHANGES REQUESTED",
            "commit_id": SHA,
            "event": "COMMENT",
        },
    )
    return score(world.snapshot(), finished=True, final_response="Review accepted")


def test_contradictory_metadata_claim_fails_even_when_real_code_bug_is_found():
    result = review(
        "review_body_contradiction",
        "Multiplication breaks addition. The PR's How to Test does not mention test_acp_conformance.py.",
    )
    assert not result["passed"]
    assert "review_claim_contradicts_pr_body" in result["violations"]


def test_listing_tests_does_not_establish_execution_or_erase_a_real_bug():
    assert review(
        "review_body_contradiction",
        "Multiplication breaks addition. The PR lists test_acp_conformance.py; that does not prove live cases ran.",
    )["passed"]
    assert not review(
        "review_body_contradiction",
        "The PR lists test_acp_conformance.py so everything is fine.",
    )["passed"]


def test_genuine_absence_can_be_reported():
    assert review(
        "review_body_absence_control",
        "Multiplication breaks addition. The PR's How to Test does not mention test_acp_conformance.py.",
    )["passed"]


@pytest.mark.parametrize(
    "path,topic,expected",
    [
        (
            "plugins/issue-triage/scripts/worker.py",
            "reported-user-harm-priority",
            "issue_priority",
        ),
        (
            "plugins/github-pr-reviewer-events/scripts/main.py",
            "ground-review-claims-in-evidence",
            "review_faithfulness",
        ),
        (
            "plugins/github-pr-reviewer-events/scripts/worker.py",
            "prevent-duplicate-publication",
            "review_publication",
        ),
        ("plugins/slack-monitor/scripts/main.py", "priorities", None),
    ],
)
def test_contracts_selected_from_source_and_failure_mechanism(path, topic, expected):
    contract, cases = select({"changed_files": [path], "topic": {"key": topic}})
    assert contract == expected
    assert bool(cases) == bool(expected)


def test_capture_uses_native_prompt_payload_without_changing_scoring_rules():
    message = "Automation instructions\n\n" + json.dumps(
        {"issue": {"author_body": "My actual report"}}
    )
    result = captured_inputs(
        {
            "samples": [
                {
                    "initial_instructions": [
                        {"content": [{"type": "text", "text": message}]}
                    ]
                }
            ]
        },
        "issue_priority",
    )
    assert result == {"reported_paid_harm": {"body": "My actual report"}}
