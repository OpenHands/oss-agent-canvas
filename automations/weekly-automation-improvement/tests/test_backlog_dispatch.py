import fcntl
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import backlog
import backlog_dispatch as dispatch
import parallel
import repair_workflow
import topic_agent
import topic_issue
from common import write_json
from test_weekly import inventory, record


class GitHub:
    def __init__(self, config, state_dir):
        self.config = config
        self.state_dir = state_dir
        self.rows = {}
        self.writes = []

    def issue(
        self, repository, number, rank, *, age="2026-10-10", extra=(), state="open"
    ):
        item = f"repos/{repository}/issues/{number}"
        url = f"https://github.com/{repository}/issues/{number}"
        identifier = f"{number:020x}"
        scope = backlog.namespace(self.config, self.state_dir)
        reader = dispatch.issue_for(url, self.state_dir, identifier, self.state_dir)
        source = {
            "namespace": scope,
            "repository": repository.casefold(),
            "topic": {"id": identifier, "priority": 3, "representative_event_ids": []},
        }
        ref = reader.artifact("topic-input.json", source)["reference"]
        self.rows[item] = {
            "number": number,
            "html_url": url,
            "state": state,
            "created_at": age,
            "title": "Test topic",
            "labels": [
                {"name": name}
                for name in (
                    "automation-improvement",
                    backlog.TOPIC,
                    backlog.READY,
                    backlog.PRIORITIES[rank],
                    *extra,
                )
            ],
            "body": (
                f"<!-- automation-improvement-backlog: {scope}/{identifier} -->\n"
                f"<!-- automation-improvement-topic: {identifier} -->\n"
                + backlog.block("backlog-status", "Ready")
                + "\n"
                + backlog.input_text(ref)
                + "\nHuman note to preserve."
            ),
        }
        return self.rows[item], ref, reader

    def pages(self, endpoint, request=None):
        if "/comments?" in endpoint:
            return []
        prefix = endpoint.split("?", 1)[0] + "/"
        return [
            row
            for key, row in self.rows.items()
            if key.startswith(prefix) and row["state"] == "open"
        ]

    def request(self, args):
        if "--method" not in args:
            return json.dumps(self.rows[args[-1]])
        method, endpoint = args[args.index("--method") + 1 : args.index("--method") + 3]
        self.writes.append((method, endpoint))
        if "/labels" in endpoint:
            item, _, name = endpoint.partition("/labels")
            labels = self.rows[item]["labels"]
            if method == "DELETE":
                labels.remove({"name": name.lstrip("/")})
            else:
                labels.append({"name": args[-1].split("=", 1)[1]})
            return "{}"
        self.rows[endpoint]["body"] = json.loads(Path(args[-1]).read_text())["body"]
        return json.dumps(self.rows[endpoint])


@pytest.fixture
def gh(prepared, monkeypatch):
    config, _ = prepared
    config["repositories"] = {"Example/secondary": {"checkout": config["checkout"]}}
    state_dir = Path(config["state_dir"])
    github = GitHub(config, state_dir)
    for module in (dispatch, topic_issue, topic_agent):
        monkeypatch.setattr(module, "github_pages", github.pages)
        monkeypatch.setattr(module, "command", github.request)
    monkeypatch.setattr(repair_workflow, "github_auth", lambda cfg: None)
    monkeypatch.setattr(
        repair_workflow,
        "repository_catalog",
        lambda cfg: {name: {} for name in [cfg["repository"], *cfg["repositories"]]},
    )
    return github


def test_selection_honors_priority_across_repositories_and_exclusions(gh):
    home, second = "Example/source", "Example/secondary"
    first, _, _ = gh.issue(home, 1, 1, age="2026-10-01")
    p0, _, _ = gh.issue(second, 2, 0)
    old_p0, _, _ = gh.issue(home, 3, 0, age="2026-10-01")
    gh.issue(second, 4, 0, extra=(backlog.ACTIVE,))
    gh.issue(second, 5, 0, extra=(backlog.BLOCKED,))
    gh.issue(second, 6, 0, state="closed")
    gh.issue(second, 7, 0, extra=(backlog.PRIORITIES[1],))
    gh.issue(second, 11, 0, extra=("Not ready for review",))
    gh.issue(second, 12, 0, extra=("Awaiting issue checklist",))
    gh.config["issue_hold_labels"] = ["awaiting-issue-checklist"]
    covered, _, _ = gh.issue(second, 8, 0)
    gh.rows[f"repos/{home}/issues/99"] = {
        "state": "open",
        "pull_request": {},
        "title": "Candidate fix",
        "html_url": f"https://github.com/{home}/pull/99",
        "body": covered["html_url"],
    }
    before = repair_workflow.run(gh.config, dry_run=True)
    selected = [
        x["topic"]["issue_url"]
        for x in before["topics"]
        if x["status"] in ("selected", "deferred_topic_limit")
    ]
    assert selected == [item["html_url"] for item in (old_p0, p0, first)]
    assert any(x["status"] == "open_repair_pr" for x in before["topics"])
    first["labels"].remove({"name": backlog.PRIORITIES[1]})
    first["labels"].append({"name": backlog.PRIORITIES[0]})
    old_p0["labels"].remove({"name": backlog.READY})
    after = repair_workflow.run(gh.config, dry_run=True)
    assert [
        x["topic"]["issue_url"] for x in after["topics"] if x["status"] == "selected"
    ] == [first["html_url"], p0["html_url"]]
    assert gh.writes == []
    assert not (gh.state_dir / "state.json").exists()
    assert not list(gh.state_dir.rglob("repair-request.json"))


@pytest.mark.parametrize("chained", [False, True])
def test_claim_finish_repeat_and_explicit_requeue(gh, monkeypatch, chained):
    import weekly

    row, _, _ = gh.issue("Example/source", 1, 0)
    starts = []
    if chained:

        def prepare(config, state_dir, *, budget, **kwargs):
            output = state_dir / f"triage-{time.monotonic_ns()}"
            output.mkdir(exist_ok=True)
            budget.record(0.1)
            return {"status": "no_unhandled_findings", "output": str(output)}

        monkeypatch.setattr(weekly, "prepare", prepare)

    def launch(cfg, context, topic, budget, root):
        labels = {value["name"] for value in row["labels"]}
        assert backlog.ACTIVE in labels and backlog.READY not in labels
        assert budget.cost == pytest.approx(0.1 if chained else 0)
        ledger = json.loads((gh.state_dir / "state.json").read_text())
        assert (
            ledger["topic_weeks"][Path(context["output"]).name + "/" + topic["id"]][
                "status"
            ]
            == "dispatched"
        )
        write_json(
            root / "result.json",
            {
                "outcome": {"topic": topic, "status": "unrepaired", "resources": []},
                "budget": {"reported_inference_cost_usd": 0.2},
            },
        )
        starts.append(topic["id"])
        return {
            "root": root,
            "topic": topic,
            "started": time.monotonic(),
            "process": SimpleNamespace(pid=123, poll=lambda: 0),
        }

    monkeypatch.setattr(parallel, "launch", launch)
    runner = weekly.run if chained else repair_workflow.run
    result = runner(gh.config)
    assert result["budget"]["reported_inference_cost_usd"] == pytest.approx(
        0.3 if chained else 0.2
    )
    assert len(starts) == 1
    assert "investigation completed" in row["body"].lower()
    assert "Human note to preserve." in row["body"]
    assert runner(gh.config)["topics"][0]["status"] == "issue_ineligible"
    assert len(starts) == 1
    row["labels"].append({"name": backlog.READY})
    runner(gh.config)
    assert len(starts) == 2
    assert (
        len(json.loads((gh.state_dir / "state.json").read_text())["topic_weeks"]) == 2
    )


def test_interrupted_claim_and_shared_lock_prevent_dispatch(gh, monkeypatch):
    row, _, _ = gh.issue("Example/source", 1, 0, extra=(backlog.ACTIVE,))
    monkeypatch.setattr(
        parallel, "launch", lambda *a: pytest.fail("duplicate dispatch")
    )
    first = repair_workflow.run(gh.config)
    assert first["topics"][0]["status"] == "issue_ineligible"
    descriptor = os.open(gh.state_dir / "weekly.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        row["labels"].remove({"name": backlog.ACTIVE})
        assert repair_workflow.run(gh.config) == {"status": "skipped_overlap"}
    finally:
        os.close(descriptor)


def test_selected_issue_resolves_private_signal_records(gh, monkeypatch):
    from backlog_dispatch import REFERENCE, issue_for

    row, old_ref, issue = gh.issue("Example/source", 1, 0)
    records = [record()]
    snapshot = gh.state_dir / "inventory.json"
    signals = gh.state_dir / "records.json"
    write_json(snapshot, inventory(records))
    write_json(signals, records)
    manifest = json.loads(issue.read_artifact(old_ref))
    manifest.update(
        inventory_path=str(snapshot),
        records_path=str(signals),
        triage_issue=None,
        triage_evidence_dir="unused",
    )
    manifest["topic"]["representative_event_ids"] = [records[0]["event"]["id"]]
    new_ref = issue.artifact("topic-input.json", manifest)["reference"]
    row["body"] = row["body"].replace(old_ref, new_ref)
    identifier = manifest["topic"]["id"]
    config = gh.config
    context = {
        "state_dir": str(gh.state_dir),
        "output": str(gh.state_dir / "run"),
        "limit": 2,
        "seed": 42,
        "repositories": {},
        "previous": [],
    }
    topic = manifest["topic"] | {
        "issue_url": row["html_url"],
        "input_reference": REFERENCE.search(row["body"])[1],
    }
    monkeypatch.setattr(repair_workflow, "Laminar", lambda *a: None)
    monkeypatch.setattr(
        repair_workflow,
        "hydrate_events",
        lambda client, frame, selected: {"samples": selected},
    )
    monkeypatch.setattr(repair_workflow, "collect_cohort", lambda *a: {"samples": []})
    prepared = repair_workflow.prepare_topic(
        config, context, topic, gh.state_dir / "topic"
    )
    assert json.loads(Path(prepared["signal_records_path"]).read_text()) == records
    output = gh.state_dir / "reader"
    output.mkdir()
    row["body"] = "<!-- automation-improvement-run: newer-run -->\n" + row["body"]
    selected = topic_agent.issue_reference(prepared, row["html_url"], output)
    assert json.loads(selected.read_artifact(new_ref)) == manifest
    assert (
        issue_for(row["html_url"], gh.state_dir, identifier, output).evidence_root
        == selected.evidence_root
    )


def test_chained_budget_exhaustion_keeps_ready_issue_for_standalone_repair(
    gh, monkeypatch
):
    import weekly

    gh.issue("Example/source", 1, 0)
    gh.config["max_inference_cost_usd"] = 0.1

    def prepare(config, state_dir, *, budget, **kwargs):
        budget.record(0.1)
        return {"status": "published", "output": str(state_dir / "triage")}

    monkeypatch.setattr(weekly, "prepare", prepare)
    monkeypatch.setattr(parallel, "launch", lambda *a: pytest.fail("budget reset"))
    result = weekly.run(gh.config)
    assert result["status"] == "budget_exhausted"
    assert result["budget"]["reported_inference_cost_usd"] == 0.1
    assert result["topics"][0]["status"] == "deferred_budget"
    assert gh.writes == []
    assert (
        repair_workflow.run(gh.config, dry_run=True)["topics"][0]["status"]
        == "selected"
    )
