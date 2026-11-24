import datetime as dt
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

from backlog import IDENTITY, TOPIC, identity, namespace, publish
from common import read_json, redact, replace_json, timestamp, write_json
from epic import is_index, refresh as refresh_epic
from events import collect_events
from github_io import command, github_pages, IMPROVEMENT_LABEL, work_item
from laminar import Laminar
from repair_policy import permits, resolve as repair_policy
from sources import github_auth
from triage import definition_key, triage
from workflow_runtime import main as workflow_main, run as run_workflow


save_state = replace_json


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


def update_backlog(state, inventory, prs, retry_topic_ids=()):
    handled = set(state["events"])
    for pr in prs:
        handled.update(
            re.findall(
                r"<!-- automation-improvement-event: ([0-9a-f-]{36}) -->", pr["body"]
            )
        )
    retry_events = {
        event
        for value in state.get("topic_weeks", {}).values()
        if value["topic"]["id"] in retry_topic_ids
        for event in value["topic"]["representative_event_ids"]
    }
    for pr in prs:
        if any(
            f"<!-- automation-improvement-topic: {topic_id} -->" in pr["body"]
            for topic_id in retry_topic_ids
        ):
            retry_events.update(
                re.findall(
                    r"<!-- automation-improvement-event: ([0-9a-f-]{36}) -->",
                    pr["body"],
                )
            )
    handled.difference_update(retry_events)
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
    args.run_id = args.output.parent.name
    args.dry_run = dry_run
    args.inventory_path = str(inventory_path)
    args.evidence_location = config.get("evidence_location")
    args.triage_agent_profile = config.get("triage_agent_profile")
    args.triage_agent_config = config.get("triage_agent", {})
    args.isolated_validation = config.get("isolated_validation", False)
    args.repair_policy = repair_policy(config)


def repository_catalog(config):
    repair_policy(config)
    configured = {
        config["repository"]: {
            "checkout": config["checkout"],
            "base_branch": config.get("base_branch", "main"),
        }
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


def existing_work(repository):
    rows = github_pages(f"repos/{repository}/issues?state=open&per_page=100")
    rows += github_pages(
        f"repos/{repository}/issues?state=closed&labels={IMPROVEMENT_LABEL}&per_page=100"
    )
    return [
        work_item(row, repository)
        for row in rows
        if not is_index(row.get("body") or "")
    ]


def discovery_repositories(config, repositories, state):
    if config.get("isolated_validation"):
        return []
    names = set(repositories)
    names.update(
        resource["repository"]
        for attempt in state.get("topic_weeks", {}).values()
        for resource in attempt.get("resources", [])
        if resource.get("repository")
    )
    for event in state.get("events", {}).values():
        for url in event.get("existing_work_urls", []):
            match = re.fullmatch(
                r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/[1-9][0-9]*",
                url,
            )
            if match:
                names.add(match[1])
    policy = repair_policy(config)
    return sorted(name for name in names if permits(policy, name))


def arguments(config, output, limit, seed=None):
    return SimpleNamespace(
        project_id=str(uuid.UUID(config["project_id"])),
        signal_id=str(uuid.UUID(config["signal_id"])),
        app_url=config.get("app_url", "https://laminar.app.all-hands.dev"),
        model=config.get("triage_model", "openai/gpt-6-luna"),
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


def backlog_work(repository):
    return [
        work_item(row, repository)
        for row in github_pages(
            f"repos/{repository}/issues?state=all&labels={TOPIC}&per_page=100"
        )
    ]


def run_locked(config, state_dir, *, dry_run, limit, seed, budget):
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
    scope = namespace(config, state_dir)
    prs, discovery_errors = [], []
    isolated = config.get("isolated_validation", False)
    names = (
        repositories
        if isolated
        else discovery_repositories(config, repositories, state)
    )
    for repository in names:
        try:
            if isolated:
                prs.extend(
                    item for item in backlog_work(repository) if identity(item, scope)
                )
            else:
                prs.extend(
                    item
                    for item in existing_work(repository)
                    if not IDENTITY.search(item["body"]) or identity(item, scope)
                )
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
        if not config.get("isolated_validation")
    ]
    summary = {
        "week": week,
        "output": str(output),
        "status": "no_unhandled_findings",
        "collection_errors": inventory.get("collection_errors", []),
        "attribution_warnings": inventory.get("attribution_warnings", []),
        "discovery_errors": discovery_errors,
        "topics": [],
        "max_topics_per_run": config.get("max_topics_per_run", 10),
        "repair_policy": repair_policy(config),
        "isolated_validation": config.get("isolated_validation", False),
    }
    weekly = None
    if records and not discovery_errors:
        triage_args = arguments(config, output / "triage", limit, seed)
        triage_args.output.mkdir(mode=0o700)
        triage_args.budget = budget
        triage_args.repositories = repositories
        triage_args.publish_backlog = True
        triage_args.topic_namespace = scope if isolated else "shared"
        configure_triage(triage_args, config, state_dir, week, dry_run, inventory_path)
        plans, aggregation = triage(triage_args, records, prs, previous)
        weekly = aggregation.get("triage_issue")
        tracked = aggregation.get("tracked_topics", [])
        by_url = {item["url"]: item for item in aggregation.get("existing_work", prs)}
        records_path = output / "signal-records.json"
        write_json(records_path, records)
        context = {
            "state_dir": str(state_dir),
            "inventory_path": str(inventory_path),
            "records_path": str(records_path),
            "triage_issue": aggregation.get("triage_issue"),
            "triage_evidence_dir": str(
                state_dir / "issue-evidence" / config["repository"] / "triage" / week
            ),
        }
        summary["status"] = "preview" if dry_run else "published"
        for update in [{"topic": topic} for topic in plans] + tracked:
            topic = update["topic"]
            matches = [
                by_url[url]
                for url in update.get("existing_work_urls", [])
                if identity(by_url[url], scope)
            ]
            if not dry_run and (not update.get("existing_work_urls") or matches):
                try:
                    if len(matches) > 1:
                        raise ValueError("Multiple backlog issues match this topic")
                    update["issue_url"] = publish(
                        config,
                        context,
                        topic,
                        matches[0] if matches else None,
                        output / "topics" / topic["id"],
                    )
                    if update.get("status") != "recurrence_pending":
                        update["status"] = (
                            "topic_updated" if matches else "topic_created"
                        )
                except Exception as error:
                    update.update(
                        status="publication_pending", error=redact(str(error))[:1000]
                    )
            update.setdefault("status", "planned")
            summary["topics"].append(update)
            if update["status"] in ("recurrence_pending", "publication_pending"):
                summary["status"] = "triage_incomplete"
            elif not dry_run:
                for identifier in topic["representative_event_ids"]:
                    state["events"][identifier] = {
                        "topic_id": topic["id"],
                        "existing_work_urls": [
                            *update.get("existing_work_urls", []),
                            *([update["issue_url"]] if update.get("issue_url") else []),
                        ],
                        "comments": update.get("comments", []),
                    }
                    state["backlog"].pop(identifier, None)
                save_state(state_path, state)
    if summary["collection_errors"] and summary["status"] in (
        "published",
        "no_unhandled_findings",
    ):
        summary["status"] = "collection_incomplete"
    if discovery_errors:
        summary["status"] = "discovery_incomplete"
    elif not dry_run:
        summary["epic"] = refresh_epic(
            config, state_dir, state, output / "epic", weekly
        )
    summary.update(pending_events=len(state["backlog"]), budget=budget.snapshot())
    write_json(output / "triage-report.json", redact(summary))
    return summary


def run(config, *, dry_run=False, limit=3, seed=None):
    return run_workflow(config, run_locked, dry_run=dry_run, limit=limit, seed=seed)


if __name__ == "__main__":
    workflow_main(run)
