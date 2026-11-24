import datetime as dt
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

from backlog import IDENTITY, TOPIC, identity, namespace, publish
from common import read_json, redact, replace_json, timestamp, write_json
from epic import is_index, refresh as refresh_epic
from events import collect_events
from finding_record import occurrence_id, read_batch
from github_io import command, github_pages, IMPROVEMENT_LABEL, work_item
from laminar import Laminar
from repair_policy import permits, resolve as repair_policy
from sources import github_auth
from topic_issue import TopicIssue
from tracked_work import WORK_URL
from triage import (
    definition_key,
    publish_recurrence,
    render_plan,
    selection_summary,
    triage,
)
from triage_agent import triage_evidence_root
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
        if "occurrence_id" in record:
            continue
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
        if "occurrence_id" not in record
        and definition_key(record["definition"]) == current
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
    args.topic_finder_command = config.get("topic_finder_command")
    args.isolated_validation = config.get("isolated_validation", False)
    args.repair_policy = repair_policy(config)


def repository_catalog(config):
    repair_policy(config)
    configured = {
        config["repository"]: {
            "checkout": config["checkout"],
            "base_branch": config.get("base_branch", "main"),
            "_source_revision": config.get("_source_revision"),
        }
    }
    configured.update(config.get("repositories", {}))
    catalog = {}
    for repository, value in configured.items():
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid repository slug")
        source = {"repository": repository, "base_branch": "main"} | value
        source.pop("revision", None)
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
    portable = config.get("finding_source") == "portable"
    if not portable:
        names.update(
            resource["repository"]
            for attempt in state.get("topic_weeks", {}).values()
            for resource in attempt.get("resources", [])
            if resource.get("repository")
        )
    scope = namespace(config, None) if portable else None
    for event in state.get("events", {}).values():
        if portable and event.get("discovery_scope") != scope:
            continue
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
        project_id=None
        if config.get("finding_source") == "portable"
        else str(uuid.UUID(config["project_id"])),
        signal_id=None
        if config.get("finding_source") == "portable"
        else str(uuid.UUID(config["signal_id"])),
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
    portable = config.get("finding_source") == "portable"
    if portable and not config.get("input"):
        raise ValueError("Portable discovery requires an input batch")
    args = arguments(config, collection, limit, seed)
    inventory = (
        read_json(Path(config["input"]), 32_000_000)
        if config.get("input")
        else collect_events(Laminar(args), set(), aggregate=True)
    )
    inventory_path = collection / "input.json"
    if not inventory_path.exists():
        write_json(inventory_path, inventory)
    records = (
        read_batch(config, inventory, state)
        if portable
        else update_backlog(state, inventory, prs)
    )
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
        if not portable and not config.get("isolated_validation")
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
    pending_by_scope = state.setdefault("pending_discovery", {})
    pending = pending_by_scope.get(scope) if not dry_run else None
    if not pending and records and not discovery_errors:
        triage_args = arguments(config, output / "triage", limit, seed)
        triage_args.output.mkdir(mode=0o700)
        triage_args.budget = budget
        triage_args.repositories = repositories
        triage_args.publish_backlog = True
        triage_args.backlog_scope = scope
        triage_args.topic_namespace = scope if portable or isolated else "shared"
        configure_triage(triage_args, config, state_dir, week, dry_run, inventory_path)
        plans, aggregation = triage(triage_args, records, prs, previous)
        weekly = aggregation.get("triage_issue")
        updates = [{"topic": topic, "status": "planned"} for topic in plans]
        updates.extend(aggregation.get("tracked_topics", []))
        if dry_run:
            summary.update(
                status="preview",
                topics=updates,
                selection=selection_summary(records, updates),
            )
        else:
            records_path = output / "signal-records.json"
            write_json(records_path, records)
            targets = {
                url
                for update in updates
                for url in update.get("existing_work_urls", [])
            }
            pending = {
                "output": str(output),
                "context": {
                    "state_dir": str(state_dir),
                    "inventory_path": str(inventory_path),
                    "records_path": str(records_path),
                    "source_revision": repositories.get(config["repository"], {}).get(
                        "revision"
                    ),
                    "triage_issue": weekly,
                    "triage_evidence_dir": str(triage_evidence_root(triage_args)),
                },
                "topics": updates,
                "existing_work": {
                    item["url"]: item
                    for item in aggregation.get("existing_work", prs)
                    if item["url"] in targets
                },
            }
            pending_by_scope[scope] = pending
            save_state(state_path, state)
    if pending and not discovery_errors:
        weekly = pending["context"]["triage_issue"]
        summary["topics"] = pending["topics"]
        summary["status"] = "published"
        try:
            reconcile_publication(
                config,
                scope,
                state,
                state_path,
                pending,
                output,
                {occurrence_id(row) for row in records},
            )
            if weekly and weekly.get("url"):
                state.setdefault("discovery_indices", {})[scope] = {
                    "url": weekly["url"]
                }
            incomplete = any(
                item["status"] in ("recurrence_pending", "publication_pending")
                for item in pending["topics"]
            )
            if incomplete:
                summary["status"] = "triage_incomplete"
                summary["publication"] = {"status": "partial"}
            else:
                pending_by_scope.pop(scope)
                summary["publication"] = {"status": "verified"}
            save_state(state_path, state)
        except Exception as error:
            summary.update(
                status="triage_incomplete",
                publication={"status": "failed", "error": redact(str(error))[:1000]},
            )
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
        if summary["epic"]["status"] != "refreshed":
            summary["status"] = "triage_incomplete"
    if "selection" not in summary:
        summary["selection"] = (pending or {}).get("selection") or selection_summary(
            [], []
        )
    if discovery_errors:
        summary["selection"]["next_action"] = (
            "Restore access to existing work and retry discovery."
        )
    elif summary.get("publication", {}).get("status") == "failed":
        summary["selection"]["next_action"] = (
            "Retry discovery to reconcile the saved publication."
        )
    summary.update(
        pending_events=sum(occurrence_id(row) in state["backlog"] for row in records),
        budget=budget.snapshot(),
    )
    write_json(output / "triage-report.json", redact(summary))
    return summary


def reconcile_publication(config, scope, state, state_path, pending, output, eligible):
    repository = config["repository"]
    context = pending["context"]
    weekly = context["triage_issue"]
    match = WORK_URL.fullmatch((weekly or {}).get("url") or "")
    if (
        not match
        or match[1].casefold() != repository.casefold()
        or match[2] != "issues"
        or match[4]
    ):
        raise ValueError("Weekly discovery issue is outside the home repository")
    issue = TopicIssue(
        repository,
        output / "summary",
        Path(context["triage_evidence_dir"]),
        {"number": int(match[3]), "html_url": weekly["url"]},
        [],
    )
    records = read_json(Path(context["records_path"]), 32_000_000)
    targets = pending["existing_work"]
    superseded = state.get("superseded_occurrences", {})
    for update in pending["topics"]:
        topic = update["topic"]
        if any(
            identifier in superseded
            or identifier not in eligible
            and identifier not in state["events"]
            for identifier in topic["representative_event_ids"]
        ):
            update["status"] = "superseded"
            update.pop("error", None)
            save_state(state_path, state)
            continue
        if update.get("status") in (
            "topic_created",
            "topic_updated",
            "recurrence_recorded",
            "superseded",
        ):
            continue
        urls = update.get("existing_work_urls", [])
        update.pop("error", None)
        if urls:
            publish_recurrence(
                update,
                list(targets.values()),
                records,
                issue,
                output / "recurrences",
                policy=repair_policy(config),
                scope=scope,
            )
            if update["status"] == "recurrence_pending":
                save_state(state_path, state)
                continue
        matches = [targets[url] for url in urls if identity(targets[url], scope)]
        if not update.get("issue_url") and (not urls or matches):
            try:
                if len(matches) > 1:
                    raise ValueError("Multiple backlog issues match this topic")
                update["issue_url"] = publish(
                    config,
                    context,
                    topic,
                    matches[0] if matches else None,
                    Path(pending["output"]) / "topics" / topic["id"],
                )
                update["status"] = "topic_updated" if matches else "topic_created"
            except Exception as error:
                update.update(
                    status="publication_pending", error=redact(str(error))[:1000]
                )
        if update["status"] not in ("recurrence_pending", "publication_pending"):
            links = list(
                dict.fromkeys(
                    [*urls, *([update["issue_url"]] if update.get("issue_url") else [])]
                )
            )
            for identifier in topic["representative_event_ids"]:
                state["events"][identifier] = {
                    "topic_id": topic["id"],
                    "discovery_scope": scope,
                    "existing_work_urls": links,
                    "comments": update.get("comments", []),
                }
                state["backlog"].pop(identifier, None)
        save_state(state_path, state)

    pending["selection"] = selection_summary(records, pending["topics"])
    issue.update_block(
        "triage-plan",
        render_plan(repository, pending["topics"], pending["selection"]),
        verify=True,
    )


def run(config, *, dry_run=False, limit=3, seed=None):
    return run_workflow(
        config,
        run_locked,
        dry_run=dry_run,
        limit=limit,
        seed=seed,
        use_model="topic_finder_command" not in config,
    )


if __name__ == "__main__":
    workflow_main(run)
