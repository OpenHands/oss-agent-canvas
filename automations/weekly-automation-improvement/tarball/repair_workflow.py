import argparse
import json
from pathlib import Path

from attribution import automation_ids as origin_automations
from budget import RunBudget
from cohorts import collect as collect_cohort, topic_records
from common import read_json, redact, write_json
from events import hydrate_events
from laminar import Laminar
from repair_policy import resolve as repair_policy
from sources import github_auth, native_request
from triage_workflow import arguments, eligible_topics
from workflow_runtime import callback, run as run_workflow


def prepare_topic(config, context, topic, root):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
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
        "triage_evidence_dir": str(
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


def run_locked(request_path, budget):
    from parallel import execute

    request = read_json(request_path, 64_000_000)
    config, context = request["config"], request["context"]
    output = Path(context["output"])
    report = output / "weekly-report.json"
    if report.exists():
        return read_json(report, 32_000_000)
    state = read_json(Path(context["state_path"]), 32_000_000)
    queue, skipped = eligible_topics(
        request["queue"], state, context["week"], config.get("retry_topic_ids", [])
    )
    execution, state = execute(config, context, queue, state, budget)
    summary = request["summary"]
    tracked = request["tracked"]
    summary.update(
        status=execution["status"],
        topics=execution["outcomes"] + request["skipped"] + skipped + tracked,
        execution={k: v for k, v in execution.items() if k != "outcomes"},
    )
    if any(item["status"] == "recurrence_pending" for item in tracked):
        summary["status"] = "triage_incomplete"
    if summary["collection_errors"] and summary["status"] in (
        "completed",
        "no_unhandled_findings",
    ):
        summary["status"] = "collection_incomplete"
    summary.update(pending_events=len(state["backlog"]), budget=budget.snapshot())
    write_json(report, redact(summary))
    return summary


def run(request_path):
    request_path = Path(request_path).expanduser().resolve()
    request = read_json(request_path, 64_000_000)

    def execute(config, state_dir, *, budget, **kwargs):
        github_auth(config)
        return run_locked(
            request_path, RunBudget.restore(request["budget"], request["saved_at"])
        )

    return run_workflow(request["config"], execute)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    from common import encode

    try:
        result = run(args.request)
        print(encode(redact(result)))
        if result["status"] in ("partial_failure", "collection_incomplete"):
            raise ValueError("Weekly run incomplete; inspect weekly-report.json")
    except Exception:
        callback("FAILED")
        raise
    callback("COMPLETED")


if __name__ == "__main__":
    main()
