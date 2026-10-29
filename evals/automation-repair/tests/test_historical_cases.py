import copy
import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from historical_cases import load
from triage_world import World, score
from summarize import cohort_sections


def identifier(number):
    return str(uuid.UUID(int=number))


def cohort(size=2):
    task = {
        "repository": "Example/project",
        "marker": "<!-- marker -->",
        "task": {
            "issue": {
                "number": 123,
                "title": "Real task",
                "author_body": "Reported impact",
                "labels": [{"name": "customer"}],
            },
            "human_discussion": [
                {"id": 999, "author": "human", "body": "Do not delete this"}
            ],
        },
    }
    return {
        "sample_size": size,
        "project_id": identifier(1),
        "automation_id": identifier(2),
        "complete": True,
        "errors": [],
        "samples": [
            {
                "case_id": classification + "-" + str(index),
                "classification": classification,
                "automation_id": identifier(2),
                "conversation_id": identifier(
                    index + (10 if classification == "negative" else 20)
                ),
                "trace_id": identifier(
                    index + (100 if classification == "negative" else 200)
                ),
                "signal_event_id": identifier(999)
                if classification == "negative"
                else None,
                "native_payload": copy.deepcopy(task),
                "repository_snapshot": {
                    "revision": "a" * 40,
                    "files": {"AGENTS.md": "Actual repository guidance"},
                    "root_entries": [
                        {"name": "AGENTS.md", "path": "AGENTS.md", "type": "file"}
                    ],
                },
                "expected_outcome": {
                    "priority": "medium_or_high",
                    "readiness": "unspecified",
                },
            }
            for classification, count in (("negative", 1), ("non_negative", size))
            for index in range(1, count + 1)
        ],
    }


def freeze(tmp_path, value):
    digest = hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps(value | {"cohort_sha256": digest}))
    return {"path": str(path), "sha256": digest, "sample_size": value["sample_size"]}


def test_real_task_identity_preservation_and_human_comment_checks(tmp_path):
    cases = load(freeze(tmp_path, cohort()), "issue_priority")
    assert len(cases) == 3 and not any(case.get("draft_decision") for case in cases)
    world = World(cases[0])
    root = "/repos/Example/project/issues/123"
    status, value = world.request("GET", root)
    assert status == 200 and value["number"] == 123
    assert value["body"] == "Reported impact"
    world.request("POST", root + "/labels", {"labels": ["priority:medium"]})
    assert score(world.snapshot(), finished=True, final_response="Done")["passed"]
    world.request("DELETE", "/repos/Example/project/issues/comments/999")
    assert (
        "human_comment_changed"
        in score(world.snapshot(), finished=True, final_response="Done")["violations"]
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "duplicate",
        "duplicate_trace",
        "different_automation",
        "fewer_controls",
        "no_guidance",
        "path_case",
    ],
)
def test_invalid_or_incomplete_cohorts_cannot_satisfy_n(tmp_path, corruption):
    data = cohort()
    if corruption == "duplicate":
        data["samples"][-1]["conversation_id"] = data["samples"][0]["conversation_id"]
    elif corruption == "duplicate_trace":
        data["samples"][-1]["trace_id"] = data["samples"][0]["trace_id"]
    elif corruption == "different_automation":
        data["samples"][0]["automation_id"] = identifier(99)
    elif corruption == "fewer_controls":
        data["samples"].pop()
    elif corruption == "no_guidance":
        data["samples"][0].pop("repository_snapshot")
    else:
        data["samples"][0]["case_id"] = "../../outside"
    with pytest.raises(ValueError):
        load(freeze(tmp_path, data), "issue_priority")


def test_changed_data_is_rejected_and_reports_have_both_cohort_sections(tmp_path):
    metadata = freeze(tmp_path, cohort())
    cases = load(metadata, "issue_priority")
    rows = [
        {
            "case_id": case["id"],
            "variant": variant,
            "score": {
                "passed": True,
                "finished": True,
                "error": None,
                "violations": [],
            },
        }
        for case in cases
        for variant in ("baseline", "candidate")
    ]
    text = "\n".join(
        cohort_sections({"runs": rows}, {case["id"]: case for case in cases})
    )
    assert "Negative Signal inputs (1 inputs, N=2)" in text
    assert "Random non-negative inputs (2 inputs, N=2)" in text
    assert "Not reproduced" in text
    value = json.loads(Path(metadata["path"]).read_text())
    value["sample_size"] = 5
    Path(metadata["path"]).write_text(json.dumps(value))
    with pytest.raises(ValueError, match="changed after selection"):
        load(metadata, "issue_priority")


def test_historical_controls_allow_existing_failures_but_never_new_violations():
    from compare import compare

    cases = [
        {
            "id": "negative-1",
            "kind": "regression",
            "native_payload": {"task": 1},
            "target_violation": "incorrect_priority",
        },
        {"id": "non_negative-1", "kind": "control", "native_payload": {"task": 1}},
    ]

    def row(case, variant, violations):
        return {
            "case_id": case,
            "variant": variant,
            "score": {
                "passed": not violations,
                "violations": violations,
                "finished": True,
                "error": None,
            },
        }

    rows = [
        row("negative-1", "baseline", ["incorrect_priority"]),
        row("negative-1", "candidate", []),
        row("non_negative-1", "baseline", ["existing_failure"]),
        row("non_negative-1", "candidate", ["existing_failure"]),
    ]
    report = compare(rows, cases)
    assert report["status"] == "improvement_on_development_cases"
    assert report["control_acceptance"] == "no_new_violations"
    assert report["runs"][-1]["score"]["violations"] == ["existing_failure"]
    assert report["runs"][-1]["introduced_violations"] == []
    rows[-1]["score"]["violations"] = ["different_failure"]
    assert compare(rows, cases)["status"] == "regressed"
    rows[-1]["score"]["error"] = "Fixture mutation unavailable"
    assert compare(rows, cases)["status"] == "inconclusive"
    rows[-1]["score"].update(
        violations=["existing_failure"], error=None, finished=False
    )
    assert compare(rows, cases)["status"] == "inconclusive"
    rows[-1]["score"]["finished"] = True
    rows[0]["score"].update(passed=True, violations=[])
    assert compare(rows, cases)["status"] == "inconclusive"
