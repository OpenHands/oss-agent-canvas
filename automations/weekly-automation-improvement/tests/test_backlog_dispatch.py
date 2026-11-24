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
from budget import RunBudget
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
                + backlog.input_text(ref, source["topic"])
                + "\nHuman note to preserve."
            ),
        }
        return self.rows[item], ref, reader

    def pages(self, endpoint, request=None):
        if "/comments?" in endpoint:
            return []
        prefix = endpoint.split("?", 1)[0] + "/"
        pulls = prefix.endswith("/pulls/")
        if pulls:
            prefix = prefix.removesuffix("pulls/") + "issues/"
        return [
            row
            for key, row in self.rows.items()
            if key.startswith(prefix)
            and row["state"] == "open"
            and (not pulls or "pull_request" in row)
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
    monkeypatch.setattr(dispatch, "ensure_labels", lambda repository: None)
    monkeypatch.setattr(
        repair_workflow, "refresh_epic", lambda *a: {"status": "refreshed"}
    )
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
    gh.issue(second, 13, 0, extra=(backlog.EXTERNAL,))
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
    assert "epic" not in before
    selected = [
        x["topic"]["issue_url"]
        for x in before["topics"]
        if x["status"] in ("selected", "deferred_topic_limit")
    ]
    assert selected == [item["html_url"] for item in (old_p0, p0, first)]
    assert any(x["status"] == "open_repair_pr" for x in before["topics"])
    assert any(x["status"] == "external_owner" for x in before["topics"])
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
    starts, refreshes = [], []
    related = {
        "kind": "issues",
        "url": "https://github.com/Example/secondary/issues/27",
    }

    def refresh(config, state_dir, state, output):
        attempts = state["topic_weeks"].values()
        refreshes.append(
            [resource for attempt in attempts for resource in attempt["resources"]]
        )
        return {"status": "refreshed"}

    monkeypatch.setattr(repair_workflow, "refresh_epic", refresh)
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
                "outcome": {
                    "topic": topic,
                    "status": "unrepaired",
                    "resources": [related],
                },
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
    assert refreshes == [[related]]
    assert result["epic"]["status"] == "refreshed"
    assert "investigation completed" in row["body"].lower()
    assert "Human note to preserve." in row["body"]
    assert {"name": backlog.OUTCOME_LABELS["unrepaired"]} in row["labels"]
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


def test_partial_claim_reconciles_without_launching_a_second_worker(gh, monkeypatch):
    row, _, _ = gh.issue("Example/source", 1, 0)
    request = gh.request
    original = row["body"]
    injected = False

    def fail_once(args):
        nonlocal injected
        if not injected and args[-1].endswith("/labels/improvement-ready"):
            injected = True
            raise OSError("GitHub write interrupted")
        return request(args)

    monkeypatch.setattr(dispatch, "command", fail_once)
    monkeypatch.setattr(
        parallel, "launch", lambda *args: pytest.fail("unexpected repair")
    )
    first = repair_workflow.run(gh.config)
    assert first["status"] == "review_incomplete"
    assert {"name": backlog.ACTIVE} in row["labels"]
    second = repair_workflow.run(gh.config)
    assert second["reconciled"][0]["reconciled"]
    assert second["execution"]["started"] == []
    assert row["body"].endswith(original.splitlines()[-1])
    assert {"name": backlog.OUTCOME_LABELS["incomplete"]} in row["labels"]
    assert not {backlog.ACTIVE, backlog.READY} & {
        label["name"] for label in row["labels"]
    }
    assert not list((gh.state_dir / "native-claims").glob("*.json"))


def test_portable_source_cannot_dispatch_native_repair_without_external_label(
    gh, monkeypatch
):
    gh.config["finding_source"] = "portable"
    gh.config["source_namespace"] = "support"
    gh.issue("Example/source", 1, 0)
    monkeypatch.setattr(
        parallel, "launch", lambda *args: pytest.fail("unexpected repair")
    )
    result = repair_workflow.run_locked(
        gh.config,
        gh.state_dir,
        dry_run=True,
        limit=3,
        seed=None,
        budget=RunBudget(gh.config),
    )
    assert result["topics"][0]["status"] == "external_owner"
    assert gh.writes == []


@pytest.mark.parametrize("change", ["head", "check", "human_draft", "closed", "merged"])
def test_next_run_reconciles_native_promotion(gh, monkeypatch, change):
    row, _, _ = gh.issue("Example/source", 1, 0)
    identifier = f"{1:020x}"
    run_id = "previous-repair"
    topic = {
        "id": identifier,
        "issue_url": row["html_url"],
        "representative_event_ids": [],
    }
    markers = (
        f"<!-- automation-improvement-run: {run_id} -->\n{topic_agent.marker(topic)}\n"
    )
    row["body"] = markers.splitlines()[0] + "\n" + row["body"]
    row["labels"] = [
        label for label in row["labels"] if label["name"] != backlog.READY
    ] + [{"name": backlog.OUTCOME_LABELS["accepted"]}]
    pr = {
        "url": "https://github.com/Example/source/pull/2",
        "repository": "Example/source",
        "kind": "pull",
        "state": "open",
        "draft": False,
        "base": "reviewed-base",
        "head": "reviewed-head",
        "promoted": True,
    }
    live = {
        "body": markers,
        "state": "open",
        "draft": False,
        "base": {"sha": "reviewed-base"},
        "head": {"sha": "changed-head"},
    }
    if change != "head":
        live["head"]["sha"] = pr["head"]
    if change in ("closed", "merged"):
        live["state"] = "closed"
        live["merged"] = change == "merged"
    if change == "human_draft":
        live["draft"] = True
    monkeypatch.setattr(
        topic_agent,
        "verify_pr_ci",
        lambda *args, **kwargs: ["Required unit-tests failed"]
        if change == "check"
        else [],
    )
    state_path = gh.state_dir / "state.json"
    key = f"{run_id}/{identifier}"
    write_json(
        state_path,
        {
            "topic_weeks": {
                key: {
                    "topic": topic,
                    "decision": "accepted",
                    "status": "reviewed",
                    "resources": [pr],
                }
            }
        },
    )
    monkeypatch.setattr(topic_agent, "live_pr", lambda url: live.copy())

    def command(args):
        assert args == ["gh", "pr", "ready", pr["url"], "--undo"]
        live["draft"] = True

    monkeypatch.setattr(topic_agent, "command", command)
    monkeypatch.setattr(
        parallel, "launch", lambda *args: pytest.fail("unexpected repair")
    )
    result = repair_workflow.run(gh.config)
    reconciled = result["reconciled"][0]
    outcome = "accepted" if change == "merged" else "incomplete"
    assert result["status"] == (
        "completed" if change == "merged" else "review_incomplete"
    )
    assert live["draft"] == (change not in ("closed", "merged"))
    assert reconciled["stale_prs"] == ([] if change == "merged" else [pr["url"]])
    assert "head `reviewed-head`" in row["body"]
    if change == "head":
        assert "head `changed-head` require review" in row["body"]
    elif change in ("merged", "closed"):
        assert (" — merged" if change == "merged" else "closed without merging") in row[
            "body"
        ]
    assert {"name": backlog.OUTCOME_LABELS[outcome]} in row["labels"]
    assert repair_workflow.run(gh.config)["reconciled"] == []


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
    assert result["discovery"]["status"] == "published"
    assert result["status"] == "budget_exhausted"
    assert result["budget"]["reported_inference_cost_usd"] == 0.1
    assert result["topics"][0]["status"] == "deferred_budget"
    assert gh.writes == []
    assert (
        repair_workflow.run(gh.config, dry_run=True)["topics"][0]["status"]
        == "selected"
    )


def test_competing_pr_created_after_selection_prevents_claim(gh, monkeypatch):
    issue, _, _ = gh.issue("Example/source", 1, 0)
    original = repair_workflow.selection

    def select(*args):
        queue, excluded = original(*args)
        assert len(queue) == 1
        gh.rows["repos/Example/secondary/issues/99"] = {
            "html_url": "https://github.com/Example/secondary/pull/99",
            "state": "open",
            "title": "Already repairing",
            "body": issue["html_url"],
            "pull_request": {},
        }
        return queue, excluded

    monkeypatch.setattr(repair_workflow, "selection", select)
    monkeypatch.setattr(
        parallel, "launch", lambda *args: pytest.fail("competing repair")
    )
    result = repair_workflow.run(gh.config)
    assert result["topics"][0]["status"] == "issue_changed"
    assert gh.writes == [] and result["execution"]["started"] == []


def test_missing_private_topic_index_marks_run_incomplete(gh, monkeypatch):
    _, reference, issue = gh.issue("Example/source", 1, 0)
    (issue.evidence_root / reference).unlink()
    monkeypatch.setattr(
        parallel, "launch", lambda *args: pytest.fail("unexpected repair")
    )
    result = repair_workflow.run(gh.config)
    assert result["topics"][0]["status"] == "backlog_evidence_unavailable"
    assert result["status"] == "review_incomplete"
    assert result["execution"]["started"] == [] and gh.writes == []


def test_native_skips_topic_assigned_to_a_maintainer(gh, monkeypatch):
    issue, _, _ = gh.issue("Example/source", 1, 0)
    issue["assignees"] = [{"login": "triage-maintainer"}]
    monkeypatch.setattr(parallel, "launch", lambda *args: pytest.fail("owned issue"))
    result = repair_workflow.run(gh.config)
    assert result["topics"][0]["status"] == "issue_ineligible"
    assert gh.writes == []
