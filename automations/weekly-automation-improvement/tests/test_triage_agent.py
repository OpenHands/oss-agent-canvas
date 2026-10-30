import json
import re

import pytest

from test_automation import arguments
from budget import RunBudget
from common import write_json
from test_coding_agent import install_sdk
from test_topic_issue import GitHub
from test_weekly import inventory, record
import topic_issue
import triage
import triage_agent


@pytest.mark.parametrize("status", ["finished", "error"])
def test_triage_agent_reads_the_issue_and_private_preparation_before_ranking(
    tmp_path, monkeypatch, capsys, status
):
    rows = [record(1), record(2, automation=2)]
    rows[0]["event"]["payload"] = json.dumps(
        {"summary": "PRIVATE ORIGINAL OBSERVATION", "signature": "example"}
    )
    frame = tmp_path / "inventory.json"
    write_json(frame, inventory(rows))
    output = tmp_path / "run" / "triage"
    output.mkdir(parents=True)
    github = GitHub(private=False)
    monkeypatch.setattr(topic_issue, "command", github)
    monkeypatch.delenv("LMNR_PROJECT_API_KEY", raising=False)

    def execute(workspace):
        root = output / "triage-agent"
        context = root / "context.json"
        triage_agent.evidence(context, "read_issue", {"section": "all", "offset": 0})
        brief = json.loads(capsys.readouterr().out)
        assert "Prepared 2 findings across 2 automations" in brief["content"]
        assert "PRIVATE ORIGINAL OBSERVATION" not in brief["content"]
        reference = re.search(
            r"\| signal-records.json \| `([^`]+)`", github.comments[0]["body"]
        )[1]
        triage_agent.evidence(
            context, "read_issue_artifact", {"reference": reference, "offset": 0}
        )
        data = json.loads(capsys.readouterr().out)
        assert "PRIVATE ORIGINAL OBSERVATION" in data["content"]
        (root / "result.json").write_text(
            json.dumps(
                {
                    "topics": [
                        {
                            "key": "one-issue",
                            "title": "One focused issue",
                            "priority": 1,
                            "reason": "Repeated actionable failure",
                            "representative_event_refs": ["e1"],
                        }
                    ],
                    "no_action_reason": "",
                }
            )
        )
        captured["instance"].state.execution_status.value = status

    captured = install_sdk(monkeypatch, execute)
    args = arguments(
        output,
        model="openai/gpt-6-luna",
        repository="Example/source",
        checkout=str(tmp_path),
        state_dir=str(tmp_path / "state"),
        triage_week="2026-W44",
        dry_run=False,
        inventory_path=str(frame),
        evidence_location="Authenticated worker",
    )
    args.budget = RunBudget({"max_inference_cost_usd": 2, "max_run_seconds": 60})
    if status == "error":
        with pytest.raises(ValueError, match="did not finish"):
            triage.triage(args, rows, [])
        assert not (output / "triage-result.json").exists()
    else:
        plans, context = triage.triage(args, rows, [])
        assert len(plans) == 1 and plans[0]["priority"] == 1
        assert (
            context["triage_issue"]["url"]
            == "https://github.com/Example/source/issues/1"
        )
    assert args.budget.cost == 0.12
    assert captured["llm"]["model"] == "openai/gpt-6-luna"
    assert captured["instance"].max_budget_per_run == 2
    assert captured["conversation"]["max_iteration_per_run"] == 40
    assert "Start at issue" in captured["prompt"]
    assert captured["closed"]
    assert "PRIVATE ORIGINAL OBSERVATION" not in json.dumps(
        github.issues + github.comments
    )
    measurement = json.loads((output / "triage-agent/measurement.json").read_text())
    assert measurement["role"] == "triage"
    assert (output / "triage-issue.json").exists()


def test_raw_triage_reads_reject_other_automations_before_contacting_laminar(tmp_path):
    context = tmp_path / "context.json"
    write_json(
        context,
        {
            "issue": {"number": 0, "url": None, "body": "preview"},
            "repository": "Example/source",
            "issue_evidence_dir": str(tmp_path),
            "automation_ids": ["approved"],
        },
    )
    with pytest.raises(ValueError, match="prepared inventory"):
        triage_agent.evidence(
            context, "search_runs", {"classification": "all", "offset": 0}, "foreign"
        )


def test_weekly_issue_reuses_catalogs_updates_summary_and_preserves_human_notes(
    tmp_path, monkeypatch
):
    github = GitHub(private=False)
    monkeypatch.setattr(topic_issue, "command", github)
    rows = [record()]
    frame = tmp_path / "first-inventory.json"
    write_json(frame, inventory(rows))
    args = arguments(
        tmp_path / "run1" / "triage",
        repository="Example/source",
        state_dir=str(tmp_path / "state"),
        triage_week="2026-W44",
        dry_run=False,
        inventory_path=str(frame),
        evidence_location=None,
    )
    context, _, prepared = triage.prepare(rows, [], [])
    first = triage_agent.prepare_issue(args, context, prepared)
    github.issues[0]["body"] += "\nHuman note: preserve this context."
    github.issues[0]["body"] = github.issues[0]["body"].replace(
        "A topic is one underlying problem and may span automations and repositories, including the SDK.",
        "One repair topic must belong to one automation.",
    )
    rows.append(record(2, automation=2))
    frame = tmp_path / "second-inventory.json"
    write_json(frame, inventory(rows))
    args.output = tmp_path / "run2" / "triage"
    args.inventory_path = str(frame)
    context, _, prepared = triage.prepare(rows, [], [])
    second = triage_agent.prepare_issue(args, context, prepared)
    assert second.value["html_url"] == first.value["html_url"]
    assert len(github.issues) == 1 and len(github.comments) == 2
    assert "Prepared 2 findings across 2 automations" in second.value["body"]
    assert "Human note: preserve this context" in second.value["body"]
    assert "One repair topic must belong to one automation." not in second.value["body"]
    assert "may span automations and repositories, including the SDK" in second.value["body"]
    github.issues[0]["state"] = "closed"
    args.output = tmp_path / "run3" / "triage"
    with pytest.raises(ValueError, match="closed"):
        triage_agent.prepare_issue(args, context, prepared)
    assert len(github.comments) == 2
