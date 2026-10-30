import argparse
import datetime as dt
import fcntl
import json
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

from budget import RunBudget
from cohorts import collect as collect_cohort, topic_records
from common import encode, read_json, redact, replace_json, timestamp, write_json
from events import collect_events, hydrate_events
from github_io import command, github_pages, IMPROVEMENT_LABEL
from laminar import Laminar
from sources import github_auth, native_request
from triage import definition_key, triage


save_state = replace_json


def load_environment(config):
    if config.get("credentials_file"):
        path = Path(config["credentials_file"]).expanduser()
        if path.stat().st_mode & 0o077:
            raise ValueError("Credentials file must be owner-only")
        values = read_json(path)
        for name in ("LMNR_PROJECT_API_KEY", "LITELLM_API_KEY"):
            if name in values:
                os.environ[name] = values[name]
    os.environ["LMNR_BASE_URL"] = config["laminar_base_url"]
    os.environ.setdefault("LMNR_FORCE_HTTP", "true")
    os.environ["LLM_BASE_URL"] = config["llm_base_url"]


def week_key(value):
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def source_revision(config):
    checkout = Path(config["checkout"]).expanduser().resolve()
    repository = config["repository"]
    allowed_remotes = {
        f"https://github.com/{repository}",
        f"https://github.com/{repository}.git",
        f"git@github.com:{repository}.git",
    }
    if command(["git", "remote", "get-url", "origin"], checkout) not in allowed_remotes:
        raise ValueError("Source origin differs from the approved repository")
    branch = config.get("base_branch", "main")
    command(["git", "check-ref-format", "--branch", branch], checkout)
    if config.get("_source_revision"):
        revision = config["_source_revision"]
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("Invalid frozen source revision")
        return command(
            ["git", "rev-parse", "--verify", revision + "^{commit}"], checkout
        )
    reference = "refs/automation-improvement/base"
    command(
        ["git", "fetch", "origin", "+refs/heads/" + branch + ":" + reference], checkout
    )
    return command(["git", "rev-parse", reference], checkout)


def update_backlog(state, inventory, prs):
    handled = set(state["events"])
    for pr in prs:
        handled.update(
            re.findall(
                r"<!-- automation-improvement-event: ([0-9a-f-]{36}) -->", pr["body"]
            )
        )
    backlog = state.setdefault("backlog", {})
    latest = {row["trace_id"]: row for row in inventory.get("latest_runs", [])}
    current = definition_key(inventory["flow1_signal"])
    for identifier, record in list(backlog.items()):
        newer = latest.get(record["event"]["trace_id"])
        superseded = (
            definition_key(record["definition"]) == current
            and newer
            and timestamp(newer["updated_at"]) >= timestamp(record["run"]["updated_at"])
            and (newer.get("event_id") != identifier or newer["status"] != "COMPLETED")
        )
        if identifier in handled or superseded:
            del backlog[identifier]
    for record in inventory.get("events", []):
        identifier = record["event"]["id"]
        if identifier not in handled:
            backlog[identifier] = record
    return [
        record
        for record in backlog.values()
        if definition_key(record["definition"]) == current
    ]


def configure_triage(args, config, state_dir, week, dry_run, inventory_path):
    args.repository = config["repository"]
    args.checkout = config["checkout"]
    args.state_dir = str(state_dir)
    args.triage_week = week
    args.dry_run = dry_run
    args.inventory_path = str(inventory_path)
    args.evidence_location = config.get("evidence_location")
    args.triage_agent_profile = config.get("triage_agent_profile")
    args.triage_agent_config = config.get("triage_agent", {})


def repository_catalog(config):
    configured = {
        config["repository"]: {
            "checkout": config["checkout"],
            "base_branch": config.get("base_branch", "main"),
        }
    }
    if config["repository"] != "OpenHands/software-agent-sdk":
        configured["OpenHands/software-agent-sdk"] = {
            "checkout": str(
                Path(config["checkout"]).expanduser().parent / "software-agent-sdk"
            )
        }
    configured.update(config.get("repositories", {}))
    catalog = {}
    for repository, value in configured.items():
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository slug")
        source = {"repository": repository, "base_branch": "main"} | value
        source["checkout"] = str(Path(source["checkout"]).expanduser().resolve())
        try:
            source["revision"] = source_revision(source)
        except Exception as error:
            source["error"] = redact(str(error))[:1000]
        catalog[repository] = source
    return catalog


def existing_prs(repository):
    return [
        {
            "url": row["html_url"],
            "repository": repository,
            "title": row["title"],
            "body": row.get("body") or "",
            "state": row["state"].upper(),
            "createdAt": row["created_at"],
        }
        for row in github_pages(
            f"repos/{repository}/issues?state=all&labels={IMPROVEMENT_LABEL}&per_page=100"
        )
        if row.get("pull_request")
    ]


def arguments(config, output, limit, seed=None):
    return SimpleNamespace(
        project_id=str(uuid.UUID(config["project_id"])),
        signal_id=str(uuid.UUID(config["signal_id"])),
        app_url=config.get("app_url", "https://laminar.app.all-hands.dev"),
        model=config.get("triage_model", "openai/gpt-6-luna"),
        retry_topic_ids=config.get("retry_topic_ids", []),
        population_limit=config.get("population_limit", 10000),
        max_response_bytes=8_000_000,
        max_sample_bytes=2_000_000,
        max_spans=1000,
        since=config.get("since"),
        until=config.get("until"),
        limit=limit,
        seed=seed,
        output=output,
    )


def prepare_topic(config, context, topic, root):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    inventory = read_json(Path(context["inventory_path"]), 32_000_000)
    by_id = {record["event"]["id"]: record for record in context["records"]}
    selected = [by_id[identifier] for identifier in topic["representative_event_ids"]]
    automation_ids = sorted({r["owner"]["automation_id"] for r in selected})
    scoped = [
        r for r in context["records"] if r["owner"]["automation_id"] in automation_ids
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
        "automation_api_url": config.get("automation_api_url"),
        "topic": topic,
        "automation_ids": automation_ids,
        "evidence_automation_ids": sorted(
            {r["owner"]["automation_id"] for r in context["records"]}
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
        "snapshot_path": str(snapshot_path),
        "cohort_path": str(cohort_path),
        "state_dir": context["state_dir"],
        "repositories": context["repositories"],
        "validation_sample_size": config.get("validation_sample_size", 5),
        "evidence_location": config.get("evidence_location"),
        "previous_attempts": context["previous"],
    }


def eligible_topics(queue, state, week, retry_ids):
    eligible, skipped = [], []
    for topic in queue:
        slots = [
            value
            for key, value in state["topic_weeks"].items()
            if key.startswith(week + "/") and value["topic"]["id"] == topic["id"]
        ]
        if slots and topic["id"] not in retry_ids:
            skipped.append(
                {"topic": topic, "status": "already_attempted", "prior_attempts": slots}
            )
        else:
            eligible.append(topic)
    return eligible, skipped


def run_locked(config, state_dir, *, dry_run, limit, seed, budget):
    from parallel import execute

    now = dt.datetime.now(dt.UTC)
    week = week_key(now)
    output = (
        state_dir
        / "runs"
        / (now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    )
    output.mkdir(parents=True, mode=0o700)
    state_path = state_dir / "state.json"
    state = read_json(state_path, 32_000_000) if state_path.exists() else {"events": {}}
    state.setdefault("topic_weeks", {})
    github_auth(config)
    repositories = repository_catalog(config)
    prs, discovery_errors = [], []
    for repository in repositories:
        try:
            prs.extend(existing_prs(repository))
        except Exception as error:
            discovery_errors.append(
                {"repository": repository, "error": redact(str(error))[:1000]}
            )
    collection = output / "collection"
    collection.mkdir(mode=0o700)
    args = arguments(config, collection, limit, seed)
    inventory = (
        read_json(Path(config["input"]), 32_000_000)
        if config.get("input")
        else collect_events(Laminar(args), set(), aggregate=True)
    )
    inventory_path = collection / "input.json"
    if not inventory_path.exists():
        write_json(inventory_path, inventory)
    records = update_backlog(state, inventory, prs)
    if not dry_run:
        save_state(state_path, state)
    previous = [
        value["topic"]
        | {
            "status": value["status"],
            "report": value.get("report"),
            "reason": value.get("reason"),
            "resources": value.get("resources", []),
        }
        for value in state["topic_weeks"].values()
    ]
    summary = {
        "week": week,
        "output": str(output),
        "status": "no_unhandled_findings",
        "collection_errors": inventory.get("collection_errors", []),
        "discovery_errors": discovery_errors,
        "topics": [],
        "max_topics_per_run": config.get("max_topics_per_run", 10),
    }
    if records:
        triage_args = arguments(config, output / "triage", limit, seed)
        triage_args.output.mkdir(mode=0o700)
        triage_args.budget = budget
        triage_args.repositories = repositories
        configure_triage(triage_args, config, state_dir, week, dry_run, inventory_path)
        queue, aggregation = triage(triage_args, records, prs, previous)
        queue, skipped = eligible_topics(
            queue, state, week, config.get("retry_topic_ids", [])
        )
        context = {
            "inventory_path": str(inventory_path),
            "records": records,
            "aggregation": aggregation,
            "repositories": repositories,
            "output": str(output),
            "state_path": str(state_path),
            "state_dir": str(state_dir),
            "week": week,
            "limit": limit,
            "seed": seed,
            "previous": previous,
        }
        if dry_run:
            summary.update(
                status="preview",
                topics=[{"topic": t, "status": "planned"} for t in queue] + skipped,
            )
        else:
            execution, state = execute(config, context, queue, state, budget)
            summary.update(
                status=execution["status"],
                topics=execution["outcomes"] + skipped,
                execution={k: v for k, v in execution.items() if k != "outcomes"},
            )
    if summary["collection_errors"] and summary["status"] in (
        "completed",
        "no_unhandled_findings",
    ):
        summary["status"] = "collection_incomplete"
    if discovery_errors and summary["status"] == "completed":
        summary["status"] = "review_incomplete"
    summary.update(pending_events=len(state["backlog"]), budget=budget.snapshot())
    write_json(output / "weekly-report.json", redact(summary))
    return summary


def run(config, *, dry_run=False, limit=3, seed=None):
    if "max_prs_per_run" in config:
        raise ValueError(
            "Replace max_prs_per_run with max_topics_per_run; one topic may produce several PRs"
        )
    for name, default in (
        ("max_topics_per_run", 10),
        ("repair_concurrency", config.get("max_topics_per_run", 10)),
        ("validation_sample_size", 5),
    ):
        if type(config.get(name, default)) is not int or config.get(name, default) < 1:
            raise ValueError(name + " must be a positive integer")
    for role in ("repair", "triage", "review"):
        profile = config.get(role + "_agent_profile")
        if profile is not None and (
            not isinstance(profile, str) or not re.fullmatch(r"[\w.-]+", profile)
        ):
            raise ValueError(
                role + "_agent_profile must be a saved OpenHands profile name or null"
            )
    if type(limit) is not int or not 1 <= limit <= 3:
        raise ValueError("Select one to three representatives per topic")
    budget = RunBudget(config)
    load_environment(config)
    state_dir = Path(config["state_dir"]).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(state_dir / "weekly.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped_overlap"}
        return run_locked(
            config, state_dir, dry_run=dry_run, limit=limit, seed=seed, budget=budget
        )
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(
            os.environ.get(
                "AUTOMATION_CONFIG_PATH",
                str(Path.home() / ".config/automation-improvement/config.json"),
            )
        ),
    )
    parser.add_argument(
        "--input", type=Path, help="Load a frozen Laminar inventory before analysis"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepare and triage only; no repair agents or GitHub writes",
    )
    parser.add_argument("--limit", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    from main import callback

    try:
        config = read_json(args.config)
        if args.input:
            config["input"] = str(args.input.resolve())
        result = run(config, dry_run=args.dry_run, limit=args.limit, seed=args.seed)
        print(encode(redact(result)))
        if result["status"] in ("partial_failure", "collection_incomplete"):
            raise ValueError("Weekly run incomplete; inspect weekly-report.json")
    except Exception:
        callback("FAILED")
        raise
    callback("COMPLETED")


if __name__ == "__main__":
    main()
