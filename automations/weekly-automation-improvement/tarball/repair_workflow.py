import datetime as dt
import json
import uuid
from pathlib import Path

from attribution import automation_ids as origin_automations
from backlog_dispatch import issue_for, selection
from cohorts import collect as collect_cohort, topic_records
from common import read_json, redact, write_json
from epic import refresh as refresh_epic
from events import hydrate_events
from laminar import Laminar
from repair_policy import resolve as repair_policy
from sources import github_auth, native_request
from triage_workflow import arguments, repository_catalog, week_key
from workflow_runtime import main, run as run_workflow


def prepare_topic(config, context, topic, root):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if topic.get("issue_url"):
        issue = issue_for(topic["issue_url"], context["state_dir"], topic["id"], root)
        source = json.loads(issue.read_artifact(topic["input_reference"]))
        context = context | {
            "inventory_path": source["inventory_path"],
            "records": read_json(Path(source["records_path"]), 32_000_000),
            "aggregation": {"triage_issue": source["triage_issue"]},
            "triage_evidence_dir": source["triage_evidence_dir"],
        }
    inventory = read_json(Path(context["inventory_path"]), 32_000_000)
    by_id = {record["event"]["id"]: record for record in context["records"]}
    selected = [by_id[identifier] for identifier in topic["representative_event_ids"]]
    automation_ids = sorted({a for r in selected for a in origin_automations(r)})
    scoped = [
        r
        for r in context["records"]
        if set(origin_automations(r)).intersection(automation_ids)
        or r["event"]["id"] in topic["representative_event_ids"]
    ]
    negatives = topic_records(scoped, topic["representative_event_ids"])
    args = arguments(config, root, context["limit"], context["seed"])
    client = Laminar(args)
    snapshot = hydrate_events(client, inventory, selected)
    snapshot.update(topic=topic, aggregation=context["aggregation"])
    snapshot_path, cohort_path = (
        root / "frozen-inputs.json",
        root / "validation-cohort.json",
    )
    write_json(snapshot_path, snapshot)
    try:
        cohort = collect_cohort(
            client,
            inventory,
            automation_ids,
            negatives,
            config.get("validation_sample_size", 5),
            context["seed"],
        )
    except Exception as error:
        cohort = {
            "samples": [],
            "errors": [{"error": redact(str(error))[:2000]}],
            "complete": False,
        }
    write_json(cohort_path, cohort)
    signal_records_path = root / "signal-records.json"
    write_json(signal_records_path, context["records"])
    runtime = {}
    for identifier in automation_ids:
        try:
            if not config.get("automation_api_url"):
                raise ValueError("automation_api_url is not configured")
            runtime[identifier] = json.loads(
                native_request(config["automation_api_url"], "/v1/" + identifier)
            )
        except Exception as error:
            runtime[identifier] = {"error": redact(str(error))[:1000]}
    return {
        "runtime": runtime,
        "run_id": Path(context["output"]).name
        if context.get("output")
        else root.parent.name,
        "automation_api_url": config.get("automation_api_url"),
        "topic": topic,
        "automation_ids": automation_ids,
        "evidence_automation_ids": sorted(
            {a for r in context["records"] for a in origin_automations(r)}
        ),
        "triage_issue": context["aggregation"]["triage_issue"],
        "topic_issue_url": topic.get("issue_url"),
        "triage_evidence_dir": context.get("triage_evidence_dir")
        or str(
            Path(context["state_dir"])
            / "issue-evidence"
            / config["repository"]
            / "triage"
            / context["week"]
        ),
        "inventory_path": context["inventory_path"],
        "signal_records_path": str(signal_records_path),
        "snapshot_path": str(snapshot_path),
        "cohort_path": str(cohort_path),
        "state_dir": context["state_dir"],
        "repositories": context["repositories"],
        "repair_policy": repair_policy(config),
        "validation_sample_size": config.get("validation_sample_size", 5),
        "evidence_location": config.get("evidence_location"),
        "previous_attempts": context["previous"],
        "isolated_validation": config.get("isolated_validation", False),
    }


def run_locked(config, state_dir, *, dry_run, limit, seed, budget):
    from parallel import execute

    github_auth(config)
    now = dt.datetime.now(dt.UTC)
    output = (
        state_dir
        / "runs"
        / (now.strftime("%Y%m%dT%H%M%SZ") + "-repair-" + uuid.uuid4().hex[:8])
    )
    output.mkdir(parents=True, mode=0o700)
    state_path = state_dir / "state.json"
    state = read_json(state_path, 32_000_000) if state_path.exists() else {}
    for key in ("events", "backlog", "topic_weeks"):
        state.setdefault(key, {})
    context = {
        "output": str(output),
        "state_path": str(state_path),
        "state_dir": str(state_dir),
        "week": week_key(now),
        "limit": limit,
        "seed": seed,
        "repositories": repository_catalog(config),
        "repair_policy": repair_policy(config),
        "previous": list(state["topic_weeks"].values()),
    }
    queue, excluded = selection(config, context, state)
    if dry_run:
        count = config.get("max_topics_per_run", 10)
        execution = {
            "status": "preview",
            "started": [],
            "outcomes": [
                {
                    "topic": topic,
                    "status": "selected" if n < count else "deferred_topic_limit",
                }
                for n, topic in enumerate(queue)
            ],
        }
    else:
        execution, state = execute(config, context, queue, state, budget)
    summary = {
        "status": execution["status"],
        "output": str(output),
        "topics": execution["outcomes"] + excluded,
        "execution": {
            key: value for key, value in execution.items() if key != "outcomes"
        },
        "budget": budget.snapshot(),
    }
    if not dry_run:
        summary["epic"] = refresh_epic(config, state_dir, state, output / "epic")
    write_json(output / "repair-report.json", redact(summary))
    return summary


def run(config, *, dry_run=False, limit=3, seed=None):
    return run_workflow(config, run_locked, dry_run=dry_run, limit=limit, seed=seed)


if __name__ == "__main__":
    main(run)
