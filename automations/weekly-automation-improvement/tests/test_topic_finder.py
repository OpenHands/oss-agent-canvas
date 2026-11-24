import json
import sys

import pytest

import topic_finder as finder
from finding_record import read_batch
from test_portable_findings import batch, row
from test_weekly import record
from triage import TRIAGE, prepare


def portable(*rows):
    records = read_batch({"source_namespace": "ci-tests"}, batch(*rows), {})
    context, _, prepared = prepare(records, [], [])
    return context, prepared


def test_portable_request_minimizes_worker_and_issue_data(monkeypatch):
    monkeypatch.setenv("EXAMPLE_SECRET", "restricted-value")
    context, records = portable(
        row(
            "first",
            group="delivery",
            unit="job-1",
            summary="restricted-value failed in /tmp/private-state",
            evidence="private-evidence",
            evidence_ref="/tmp/private-pointer",
        ),
        row("second", group="delivery", unit="job-1", origins=["private-origin"]),
    )
    url = "https://github.com/Example/source/issues/1"
    context.update(
        existing_work=[
            {"url": url, "title": "Delivery deadline", "body": "private-body"},
            {"url": "https://github.com/Other/repo/issues/1", "title": "private-title"},
        ],
        repair_policy={"scope": "customer_stack", "repositories": ["Example/source"]},
        plan_schema=TRIAGE,
        issue_guide_files=["CONTRIBUTING.md"],
        issue_guides=[
            {"path": "CONTRIBUTING.md", "text": "Include testing requirements."},
            {"path": "/absolute/path", "text": "private-guide"},
        ],
    )
    request = finder.build_request(context, records, 2)
    assert request["schema"] == "openhands.topic-finder-input.v1"
    assert request["plan_schema"] == TRIAGE
    assert request["representative_limit"] == 2
    assert request["existing_work"] == [{"url": url, "title": "Delivery deadline"}]
    assert request["issue_guides"] == [
        {"path": "CONTRIBUTING.md", "text": "Include testing requirements."}
    ]
    assert [item["unit_refs"] for item in request["findings"]] == [
        ["unit:u1"],
        ["unit:u1"],
    ]
    assert all(item["group"] == "delivery" for item in request["findings"])
    data = json.dumps(request)
    for hidden in (
        "private-",
        "restricted-value",
        "job-1",
        "/tmp",
        records[0]["occurrence_id"],
    ):
        assert hidden not in data


def test_laminar_request_groups_every_event_and_encodes_all_units():
    rows = [record(i) for i in range(1, 6)]
    rows[-1]["owner_candidates"] = [rows[0]["owner"], rows[1]["owner"]]
    rows[-1]["owner"] = None
    rows[-1]["event"]["trace_id"] = rows[0]["event"]["trace_id"]
    context, _, records = prepare(rows, [], [])
    request = finder.build_request(context, records, 3)
    first, second, *_, last = request["findings"]
    assert len(request["findings"]) == 5
    assert last["summary"] == "Failure hidden"
    assert last["group"] == first["group"] and last["group"]
    assert last["unit_refs"] == [
        first["unit_refs"][0],
        first["unit_refs"][1],
        second["unit_refs"][1],
    ]
    assert last["unit_refs"][0].startswith("trace:") and last["unit_refs"][
        1
    ].startswith("conversation:")
    assert rows[0]["event"]["trace_id"] not in json.dumps(request)
    assert "laminar.app" not in json.dumps(request)


def test_private_group_hints_remain_distinct():
    context, records = portable(
        row("a", group="/private/group-a"),
        row("b", group="/private/group-b"),
    )
    request = finder.build_request(context, records, 2)
    hints = {item["group"] for item in request["findings"]}
    assert len(hints) == 2 and all(hint.startswith("group-") for hint in hints)
    assert "/private/" not in json.dumps(request)


def test_request_binds_occurrences_scope_and_input():
    context, records = portable(row("a"), row("b"))
    request = finder.build_request(context, records, 2)
    assert request == finder.build_request(context, records[::-1], 2)
    other, fresh = portable(row("c"), row("d"))
    updated = finder.build_request(other, fresh, 2)
    assert request["findings"] == updated["findings"]
    assert request["request_id"] != updated["request_id"]
    assert (
        request["request_id"]
        != finder.build_request(context | {"backlog_scope": "new"}, records, 2)[
            "request_id"
        ]
    )


def run_script(script, *, timeout=3):
    context, records = portable(row("a"))
    return finder.run_finder(
        [sys.executable, "-c", script], context, records, limit=2, timeout=timeout
    )


SCRIPT = """
import json, os, sys
request = json.load(sys.stdin)
assert 'EXAMPLE_SECRET' not in os.environ
plan = dict(schema='openhands.topic-plan.v1', request_id=request['request_id'], topics=[], no_action_reason='No selection')
"""


def test_finder_reads_json_on_stdin_and_returns_plan(monkeypatch):
    monkeypatch.setenv("EXAMPLE_SECRET", "unavailable-in-child")
    assert run_script(SCRIPT + "print(json.dumps(plan))") == {
        "topics": [],
        "no_action_reason": "No selection",
    }


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("plan['schema'] = 'unknown'", "invalid plan"),
        ("plan['request_id'] = 'other'", "stale"),
        (
            "plan['topics'] = [dict(representative_event_refs=['e99'], existing_work_urls=[])]",
            "unknown finding",
        ),
        (
            "plan['topics'] = [dict(representative_event_refs=['e1'], existing_work_urls=['https://github.com/org/repo/issues/2'])]",
            "unknown existing work",
        ),
    ],
)
def test_finder_rejects_foreign_plans_and_references(mutation, error):
    with pytest.raises(ValueError, match=error):
        run_script(SCRIPT + mutation + "\nprint(json.dumps(plan))")


def test_command_failure_timeout_and_output_limits(monkeypatch):
    with pytest.raises(ValueError, match="^Topic finder command failed$"):
        run_script("import sys; sys.exit('private-message')")
    with pytest.raises(TimeoutError, match="timed out"):
        run_script("import time; time.sleep(20)", timeout=0.05)
    monkeypatch.setattr(finder, "MAX_OUTPUT_BYTES", 512)
    with pytest.raises(ValueError, match="byte limit"):
        run_script("print('x' * 513)")
    with pytest.raises(ValueError, match="^Topic finder returned invalid JSON$"):
        run_script("print('private-invalid-value')")


@pytest.mark.parametrize(
    "value",
    [None, [], "python finder.py", ["python"], ["/bin/echo", 3], ["/bin/echo", "x\0y"]],
)
def test_command_requires_explicit_argv(value):
    with pytest.raises(ValueError, match="absolute executable"):
        finder.validate_command(value)
