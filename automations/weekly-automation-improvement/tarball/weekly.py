import argparse
import datetime as dt
import fcntl
import json
import os
import re
import tarfile
import uuid
from pathlib import Path

import main as analysis
from budget import BudgetExhausted, RunBudget
from cohorts import topic_records
from common import encode, read_json, redact, timestamp, write_json
from events import collect_events, hydrate_events
from laminar import Laminar
from patches import command, load_source, propose_fix
from sources import github_auth, native_request, repository_index, resolve_source
from triage import definition_key, triage
from topic_issue import TopicIssue, IssueReader


def save_state(path, state):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    write_json(temporary, state)
    os.replace(temporary, path)


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
    os.environ["LLM_BASE_URL"] = config["llm_base_url"]
    comparison = config.get("comparative_validation", {})
    profile_id = comparison.get("profile_id")
    if profile_id:
        if not re.fullmatch(r"[\w.-]+", profile_id):
            raise ValueError("Invalid comparison profile ID")
        profile = json.loads(
            native_request(
                os.environ.get("AGENT_SERVER_URL", ""),
                "/api/profiles/" + profile_id,
                expose_secrets=True,
            )
        )["config"]
        if not profile.get("api_key"):
            raise ValueError("Comparison profile has no model credential")
        os.environ["COMPARISON_API_KEY"] = profile["api_key"]
        comparison.update(
            model=profile["model"],
            base_url=profile["base_url"],
            effort=profile.get("reasoning_effort") or "low",
        )


def existing_prs(repository, base_branch="main"):
    rows = json.loads(
        command(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--base",
                base_branch,
                "--state",
                "all",
                "--limit",
                "100",
                "--json",
                "url,title,body,headRefName,state,createdAt",
            ]
        )
    )
    return [
        row | {"body": row["body"][:12000]}
        for row in rows
        if row["headRefName"].startswith("automation/improve-")
    ]


def week_key(value):
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def source_config(config, snapshot, output):
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
    reference = "refs/automation-improvement/base"
    command(
        ["git", "fetch", "origin", "+refs/heads/" + branch + ":" + reference], checkout
    )
    revision = command(["git", "rev-parse", reference], checkout)
    index = repository_index(checkout, revision)
    files, identifiers, samples, errors = set(), set(), [], []
    for sample in snapshot["samples"]:
        if sample.get("collection_error"):
            errors.append(
                {
                    "event_ids": sample["selected_event_ids"],
                    "error": sample["collection_error"],
                }
            )
            continue
        try:
            resolved = resolve_source(config, sample, index)
            proposed = files | set(resolved["files"])
            if (
                len(proposed) > 30
                or sum(
                    int(
                        command(
                            ["git", "cat-file", "-s", revision + ":" + path], checkout
                        )
                    )
                    for path in proposed
                )
                > 250000
            ):
                raise ValueError("Selected source exceeds the 30-file/250 KB budget")
            files = proposed
            identifiers.add(sample["automation_id"])
            sample["source_resolution"] = resolved
            samples.append(sample)
        except (ValueError, OSError, KeyError, tarfile.TarError) as error:
            errors.append(
                {"event_ids": sample["selected_event_ids"], "error": str(error)[:500]}
            )
    if not samples:
        return None, [], errors
    paths = sorted(files)
    sizes = [
        int(command(["git", "cat-file", "-s", revision + ":" + path], checkout))
        for path in paths
    ]
    if len(paths) > 30 or sum(sizes) > 250000:
        raise ValueError(
            "Selected source exceeds the 30-file/250 KB budget; reduce --limit"
        )
    value = {
        "repository": repository,
        "checkout": str(checkout),
        "revision": revision,
        "base_branch": branch,
        "automation_ids": sorted(identifiers),
        "files": paths,
        "checks": [],
        "require_improvement": config.get("require_improvement", True),
        "validation_sample_size": config.get("validation_sample_size", 5),
    }
    if len(identifiers) == 1:
        configured = config.get("comparative_validation", {})
        comparison = (
            configured
            if configured.get("command")
            else configured.get(next(iter(identifiers)))
        )
        if comparison:
            value["comparative_validation"] = comparison | {
                "judge_model": config.get("repair_model", "openai/gpt-6.1-sol"),
                "judge_base_url": config["llm_base_url"],
            }
    path = output / "source-config.json"
    write_json(path, value)
    return path, samples, errors


def run(config, *, dry_run=False, limit=3, seed=None):
    size = config.get("validation_sample_size", 5)
    if type(size) is not int or size < 1:
        raise ValueError("validation_sample_size must be a positive integer")
    if not 1 <= limit <= 3:
        raise ValueError("Select one to three representatives per topic")
    batch_size = config.get("triage_batch_size", 3)
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("triage_batch_size must be a positive integer")
    budget = RunBudget(config)
    load_environment(config)
    state_dir = Path(config["state_dir"]).expanduser()
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


def arguments(config, output, limit, seed=None):
    args = analysis.argument_parser().parse_args([])
    args.project_id = str(uuid.UUID(config["project_id"]))
    args.signal_id = str(uuid.UUID(config["signal_id"]))
    args.app_url = config.get("app_url", "https://laminar.app.all-hands.dev")
    args.analyzer = "flow1"
    args.model = config.get("triage_model", "openai/gpt-6-luna")
    args.coordinator = config.get("repair_model", "openai/gpt-6.1-sol")
    args.limit, args.seed = limit, seed
    args.population_limit = 10000
    args.output = output
    return args


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


def published_for(prs, automation_id, week, topic):
    automation_marker = (
        "<!-- automation-improvement-automation: " + automation_id + " -->"
    )
    topic_marker = "<!-- automation-improvement-topic: " + topic["id"] + " -->"
    identifiers = set(topic["representative_event_ids"])
    for pr in prs:
        if pr.get("state") != "OPEN" and week_key(timestamp(pr["createdAt"])) != week:
            continue
        events = set(
            re.findall(
                r"<!-- automation-improvement-event: ([0-9a-f-]{36}) -->", pr["body"]
            )
        )
        if (
            automation_marker in pr["body"] and topic_marker in pr["body"]
        ) or events.intersection(identifiers):
            return pr
    return None


def recover_topic_slots(state, prs, week):
    for pr in prs:
        if week_key(timestamp(pr["createdAt"])) != week:
            continue
        body = pr["body"]
        automation = re.search(
            r"<!-- automation-improvement-automation: ([0-9a-f-]{36}) -->", body
        )
        topic = re.search(
            r"<!-- automation-improvement-topic: ([0-9a-f]{20}) -->", body
        )
        key = re.search(
            r"<!-- automation-improvement-topic-key: ([a-z0-9-]{1,40}) -->", body
        )
        if automation and topic:
            state["topic_weeks"].setdefault(
                week + "/" + automation[1] + "/" + topic[1],
                {
                    "status": "completed",
                    "topic": {
                        "id": topic[1],
                        "key": key[1] if key else "",
                        "title": pr.get("title", ""),
                    },
                    "pr_url": pr["url"],
                },
            )


def investigate_topic(
    config,
    inventory,
    records,
    prs,
    output,
    state,
    state_path,
    week,
    dry_run,
    limit,
    seed,
    topic,
    aggregation,
    budget=None,
    validation_records=None,
):
    automation_id = records[0]["owner"]["automation_id"]
    args = arguments(config, output / "analysis", limit, seed)
    args.budget = budget
    result = {
        "automation_id": automation_id,
        "topic": topic,
        "status": "no_promising_findings",
        "publication": "disabled" if dry_run else "none",
    }
    snapshot = hydrate_events(Laminar(args), inventory, records)
    path, samples, errors = source_config(config, snapshot, output)
    result["source_errors"] = errors
    if not samples:
        result["status"] = "no_approved_source_match"
        return result
    snapshot["samples"] = [
        sample | {"case_id": f"c{index}"} for index, sample in enumerate(samples, 1)
    ]
    snapshot["source"] = load_source(path)
    if set(snapshot["source"]["automation_ids"]) != {automation_id}:
        raise ValueError("Repair source must be scoped to exactly this automation")
    snapshot["aggregation"] = aggregation
    snapshot["topic"] = topic
    snapshot["existing_improvement_prs"] = prs
    snapshot["issue_evidence_dir"] = str(
        (
            Path(config["state_dir"]).expanduser()
            / "issue-evidence"
            / config["repository"]
            / topic["id"]
        ).resolve()
    )
    snapshot["evidence_location"] = config.get("evidence_location")
    if config.get("comparative_validation"):
        from cohorts import prepare, collect

        cohort = collect(
            Laminar(args),
            inventory,
            automation_id,
            validation_records or records,
            config.get("validation_sample_size", 5),
            seed,
        )
        cohort = prepare(cohort, snapshot["source"], output / "validation-context")
        cohort_path = output / "validation-cohort.json"
        write_json(cohort_path, cohort)
        snapshot["validation_cohort"] = {
            "path": str(cohort_path.resolve()),
            "sha256": cohort["cohort_sha256"],
            "sample_size": cohort["sample_size"],
            "complete": cohort["complete"],
        }
    issue = TopicIssue.ensure(snapshot, output / "issue", preview=dry_run)
    result["issue_url"] = issue.value["html_url"]
    if issue.value["state"] != "open":
        result["status"] = "topic_issue_closed"
        return result
    issue.frozen(snapshot)
    snapshot["repair_issue"] = issue.refresh()
    frozen = output / "snapshot-with-source.json"
    write_json(frozen, snapshot)
    args.input, args.source_config = frozen, path
    args.output = output / "analysis"
    from raw_evidence import EvidenceReader

    args.evidence_reader = IssueReader(
        EvidenceReader(Laminar(args), automation_id, inventory, output / "evidence"),
        issue,
        output / "evidence",
    )
    args.max_evidence_reads = config.get("max_evidence_reads", 8)
    analysis.run(args)
    report = read_json(args.output / "report.json")
    if report.get("errors"):
        issue.attempt(args.output, report)
        raise ValueError(
            "Topic analysis incomplete; retain findings for another attempt"
        )
    by_id = {finding["id"]: finding for finding in report["findings"]}
    updates = {}
    for rejection in report["rejected_findings"]:
        if rejection.get("disposition") == "other_topic":
            continue
        event_id = by_id[rejection["finding_id"]]["signal_event_id"]
        updates[event_id] = {
            "status": "rejected",
            "topic_id": topic["id"],
            "disposition": rejection.get("disposition", "unsupported"),
            "reason": rejection["reason"],
            "week": week,
        }
    result.update(
        status="analyzed",
        findings=len(report["findings"]),
        proposals=len(report["proposals"]),
        rejections=report["rejected_findings"],
        behavioral_evaluation="not_performed",
        coordinator_measurements=[
            item
            for item in report["measurements"]
            if item.get("backend") != "persisted_flow1_signals"
        ],
    )
    if report["proposals"]:
        proposal = report["proposals"][0]
        if proposal["patch"] and config.get("publish_drafts", False) and not dry_run:
            key = week + "/" + automation_id + "/" + topic["id"]
            state["topic_weeks"][key] = {
                "topic": topic,
                "status": "reserved",
                "report": str(args.output / "report.json"),
            }
            save_state(state_path, state)
            args.publish_draft = True
            args.publication_automation_id, args.publication_week = automation_id, week
            args.publication_topic = topic
            publication = propose_with_evidence(args, report, snapshot, issue)
            feedback = comparison_feedback(args.output)
            if (
                not publication.get("pr_url")
                and feedback
                and config.get("revise_failed_repair", True)
            ):
                state["topic_weeks"].pop(key, None)
                save_state(state_path, state)
                previous_patch = proposal["patch"]
                feedback = feedback | {"previous_patch": previous_patch}
                snapshot = snapshot | {
                    "repair_feedback": feedback,
                    "repair_issue": issue.refresh(),
                }
                revision_input = output / "snapshot-revision.json"
                write_json(revision_input, snapshot)
                args.input, args.output = revision_input, output / "revision"
                args.publish_draft = False
                analysis.run(args)
                revised = read_json(args.output / "report.json")
                if (
                    not revised.get("errors")
                    and revised.get("proposals")
                    and revised["proposals"][0]["patch"]
                    and revised["proposals"][0]["patch"] != previous_patch
                ):
                    report, proposal = revised, revised["proposals"][0]
                    args.publish_draft = True
                    state["topic_weeks"][key] = {
                        "topic": topic,
                        "status": "reserved",
                        "report": str(args.output / "report.json"),
                    }
                    save_state(state_path, state)
                    publication = propose_with_evidence(args, report, snapshot, issue)
                    result["repair_revision"] = {
                        "feedback": feedback,
                        "report": str(args.output / "report.json"),
                    }
                    result["analysis_report"] = str(args.output / "report.json")
                else:
                    issue.attempt(args.output, revised)
            result["publication"] = publication
            result["behavioral_evaluation"] = publication.get("comparison", {}).get(
                "status", "not_performed"
            )
            if not publication.get("pr_url"):
                result["status"] = "deferred_validation"
                state["topic_weeks"].pop(key, None)
                save_state(state_path, state)
                return result
            for finding_id in proposal["finding_ids"]:
                updates[by_id[finding_id]["signal_event_id"]] = {
                    "status": "draft_pr",
                    "topic_id": topic["id"],
                    "pr_url": publication["pr_url"],
                    "week": week,
                }
        elif proposal["patch"]:
            args.check_draft = True
            result["patch_check"] = propose_with_evidence(args, report, snapshot, issue)
        else:
            issue.attempt(args.output, report)
            for finding_id in proposal["finding_ids"]:
                updates[by_id[finding_id]["signal_event_id"]] = {
                    "status": "diagnostic_only",
                    "topic_id": topic["id"],
                    "week": week,
                }
    else:
        issue.attempt(args.output, report)
    if not dry_run:
        state["events"].update(updates)
        for event_id in updates:
            state["backlog"].pop(event_id, None)
    return result


def propose_with_evidence(args, report, snapshot, issue):
    try:
        result = propose_fix(args, report, snapshot)
    except Exception as error:
        issue.attempt(
            args.output,
            report,
            {
                "status": "failed",
                "error": redact(str(error)),
                "detail": redact(getattr(error, "stderr", None)),
            },
        )
        raise
    issue.attempt(args.output, report, result)
    return result


def comparison_feedback(output):
    path = output / "comparison/runs/comparison.json"
    if not path.exists():
        return None
    report = read_json(path)
    if "assessment" in report:
        assessment = report["assessment"]
        kinds = {case["case_id"]: case["kind"] for case in report["cases"]}
        candidates = {
            (row["case_id"], row.get("repeat", 1)): row["score"]
            for row in report["runs"]
            if row["variant"] == "candidate"
        }
        failed = []
        for observation in assessment["observations"]:
            semantic = observation.get("semantic") or {}
            if observation["valid"] and (
                observation["new_violations"]
                or semantic.get("regression") is True
                or (
                    semantic.get("candidate_success") is False
                    and (
                        kinds[observation["case_id"]] == "regression"
                        or semantic.get("baseline_success") is True
                    )
                )
                or (
                    not observation["candidate_conformance"]
                    and (
                        observation["baseline_conformance"]
                        or kinds[observation["case_id"]] == "regression"
                    )
                )
            ):
                score = candidates[(observation["case_id"], observation["repeat"])]
                failed.append(
                    {
                        "case_id": observation["case_id"],
                        "repeat": observation["repeat"],
                        "violations": score["violations"],
                        "introduced_violations": observation["new_violations"],
                        "final_priority": score.get("final_priority"),
                        "assessment": semantic,
                    }
                )
        return (
            {
                "previous_attempt": str(path),
                "failed_cases": failed,
                "unresolved_pairs": assessment["invalid_pairs"],
            }
            if failed
            else None
        )
    failed = [
        row
        for row in report["runs"]
        if row["variant"] == "candidate"
        and row["score"]["finished"]
        and not row["score"]["error"]
        and not row["score"]["passed"]
    ]
    if report.get("control_acceptance") == "no_new_violations":
        controls = {
            case["case_id"] for case in report["cases"] if case["kind"] == "control"
        }
        failed = [
            row
            for row in failed
            if row["case_id"] not in controls or row.get("introduced_violations")
        ]
    if not failed:
        return None
    return {
        "previous_attempt": str(path),
        "failed_cases": [
            {
                "case_id": row["case_id"],
                "violations": row["score"]["violations"],
                "final_priority": row["score"].get("final_priority"),
            }
            for row in failed
        ],
    }


def investigate(
    config,
    inventory,
    records,
    prs,
    output,
    state,
    state_path,
    week,
    dry_run,
    limit,
    seed,
    budget=None,
):
    automation_id = records[0]["owner"]["automation_id"]
    prefix = week + "/" + automation_id + "/"
    completed = [
        value["topic"]
        for key, value in state["topic_weeks"].items()
        if key.startswith(prefix)
    ]
    result = {
        "automation_id": automation_id,
        "automation_name": records[0]["owner"]["automation_name"],
        "topics": [],
        "triage_passes": [],
    }
    scoped_prs = [
        pr
        for pr in prs
        if "<!-- automation-improvement-automation:" not in pr["body"]
        or "<!-- automation-improvement-automation: " + automation_id + " -->"
        in pr["body"]
    ]
    by_id = {record["event"]["id"]: record for record in records}
    pending = list(records)
    seen, attempted, exhausted = set(), set(), False
    while pending:
        try:
            if budget:
                budget.check()
            args = arguments(
                config,
                output / "triage" / f"pass-{len(result['triage_passes']) + 1:03d}",
                limit,
                seed,
            )
            args.budget = budget
            args.max_topics = config.get("triage_batch_size", 3)
            args.output.mkdir(parents=True, mode=0o700)
            topics, aggregation = triage(args, pending, scoped_prs, completed)
        except BudgetExhausted as error:
            result["stop_reason"] = str(error)
            exhausted = True
            break
        result["triage_passes"].append(aggregation)
        if not topics:
            result["no_action_reason"] = aggregation.get("luna_triage", {}).get(
                "no_action_reason", "No further promising topics"
            )
            break
        for topic in topics:
            attempted.update(topic["representative_event_ids"])
            if topic["id"] in seen:
                continue
            seen.add(topic["id"])
            completed.append(topic)
            key = prefix + topic["id"]
            directory = output / "topics" / topic["id"]
            directory.mkdir(parents=True, mode=0o700)
            tracked = published_for(scoped_prs, automation_id, week, topic)
            if not dry_run and (key in state["topic_weeks"] or tracked):
                outcome = {
                    "topic": topic,
                    "status": "topic_already_completed_or_tracked",
                    "pr_url": tracked["url"]
                    if tracked
                    else state["topic_weeks"][key].get("pr_url"),
                }
            else:
                context = {
                    **aggregation,
                    "groups": [
                        card
                        for card in aggregation["groups"]
                        if card["group_id"] in topic["group_ids"]
                    ],
                    "luna_triage": {"topics": [topic], "no_action_reason": ""},
                }
                try:
                    if budget:
                        budget.check()
                    outcome = investigate_topic(
                        config,
                        inventory,
                        [
                            by_id[event_id]
                            for event_id in topic["representative_event_ids"]
                        ],
                        scoped_prs,
                        directory,
                        state,
                        state_path,
                        week,
                        dry_run,
                        limit,
                        seed,
                        topic,
                        context,
                        budget,
                        validation_records=topic_records(
                            records, topic["representative_event_ids"]
                        ),
                    )
                    if not dry_run and outcome["status"] != "deferred_validation":
                        state["topic_weeks"][key] = {
                            "status": "completed",
                            "topic": topic,
                            "report": str(directory / "topic-report.json"),
                            "publication": outcome["publication"],
                            "pr_url": outcome["publication"].get("pr_url")
                            if isinstance(outcome["publication"], dict)
                            else None,
                        }
                        save_state(state_path, state)
                    if isinstance(outcome.get("publication"), dict) and outcome[
                        "publication"
                    ].get("pr_url"):
                        report = read_json(
                            Path(
                                outcome.get(
                                    "analysis_report",
                                    directory / "analysis/report.json",
                                )
                            )
                        )
                        proposal = report["proposals"][0]
                        scoped_prs.append(
                            {
                                "url": outcome["publication"]["pr_url"],
                                "title": proposal["title"],
                                "body": "\n".join(
                                    (
                                        "<!-- automation-improvement-automation: "
                                        + automation_id
                                        + " -->",
                                        "<!-- automation-improvement-topic: "
                                        + topic["id"]
                                        + " -->",
                                        "<!-- automation-improvement-topic-key: "
                                        + topic["key"]
                                        + " -->",
                                        proposal["hypothesis"],
                                    )
                                ),
                                "state": "OPEN",
                                "createdAt": dt.datetime.now(dt.UTC).isoformat(),
                            }
                        )
                except BudgetExhausted as error:
                    outcome = {
                        "topic": topic,
                        "status": "deferred_budget",
                        "reason": str(error),
                    }
                    exhausted = True
                except Exception as error:
                    outcome = {
                        "topic": topic,
                        "status": "failed",
                        "error": str(error)[:1000],
                    }
            write_json(directory / "topic-report.json", redact(outcome))
            result["topics"].append(outcome)
            if exhausted:
                break
        if exhausted:
            break
        pending = [
            record for record in records if record["event"]["id"] not in attempted
        ]
    return result | {
        "budget_exhausted": exhausted,
        "status": "partial_failure"
        if any(item["status"] == "failed" for item in result["topics"])
        else "budget_exhausted"
        if exhausted
        else "validation_incomplete"
        if any(item["status"] == "deferred_validation" for item in result["topics"])
        else "completed"
        if result["topics"]
        else "no_promising_findings",
    }


def run_locked(config, state_dir, *, dry_run, limit, seed, budget=None):
    now = dt.datetime.now(dt.UTC)
    week = week_key(now)
    output = (
        state_dir
        / "runs"
        / (now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    )
    output.mkdir(parents=True, mode=0o700)
    state_path = state_dir / "state.json"
    state = (
        read_json(state_path, 32_000_000)
        if state_path.exists()
        else {"events": {}, "publications": {}}
    )
    state.setdefault("topic_weeks", {})
    github_auth(config)
    validation_reports = []
    if not dry_run and config.get("validate_existing_repairs", False):
        from validate_repairs import validate

        validation_reports = validate(config, state_dir / "repair-validation")
    prs = existing_prs(config["repository"], config.get("base_branch", "main"))
    recover_topic_slots(state, prs, week)
    args = arguments(config, output / "collection", limit, seed)
    args.output.mkdir(mode=0o700)
    inventory = collect_events(Laminar(args), set(), aggregate=True)
    summary = {
        "status": "no_unhandled_findings",
        "week": week,
        "output": str(output),
        "selection": inventory["selection"],
        "signal_runs": inventory["signal_run_summary"],
        "collection_errors": inventory["collection_errors"],
        "automations": [],
        "publication": "disabled" if dry_run else "per_topic",
        "existing_repair_validation": validation_reports,
    }
    records = update_backlog(state, inventory, prs)
    if not dry_run:
        save_state(state_path, state)
    grouped = {}
    for record in records:
        grouped.setdefault(record["owner"]["automation_id"], []).append(record)
    summary["pending_events"] = len(state["backlog"])
    summary["incompatible_pending_events"] = len(state["backlog"]) - len(records)
    legacy = state.get("publications", {}).get(week)
    for automation_id, pending in sorted(grouped.items()):
        key = week + "/" + automation_id
        directory = output / automation_id
        directory.mkdir(mode=0o700)
        if not dry_run and (
            state.get("automation_weeks", {}).get(key, {}).get("status") == "reserved"
            or legacy
            and legacy.get("status") == "reserved"
        ):
            result = {
                "automation_id": automation_id,
                "status": "legacy_publication_reserved",
            }
        else:
            try:
                result = investigate(
                    config,
                    inventory,
                    pending,
                    prs,
                    directory,
                    state,
                    state_path,
                    week,
                    dry_run,
                    limit,
                    seed,
                    budget,
                )
            except BudgetExhausted as error:
                result = {
                    "automation_id": automation_id,
                    "status": "budget_exhausted",
                    "reason": str(error),
                }
            except Exception as error:
                result = {
                    "automation_id": automation_id,
                    "status": "failed",
                    "error": str(error)[:1000],
                }
        write_json(directory / "automation-report.json", redact(result))
        summary["automations"].append(result)
    if grouped:
        summary["status"] = (
            "partial_failure"
            if any(
                item["status"] in ("failed", "partial_failure")
                for item in summary["automations"]
            )
            else "completed"
        )
    if summary["collection_errors"] and summary["status"] != "partial_failure":
        summary["status"] = (
            "completed_with_collection_errors" if grouped else "collection_incomplete"
        )
    if summary["status"] != "partial_failure" and any(
        item["status"] == "budget_exhausted" or item.get("budget_exhausted")
        for item in summary["automations"]
    ):
        summary["status"] = "budget_exhausted"
    if summary["status"] not in ("partial_failure", "budget_exhausted") and any(
        topic["status"] == "deferred_validation"
        for automation in summary["automations"]
        for topic in automation.get("topics", [])
    ):
        summary["status"] = "validation_incomplete"
    summary["budget"] = budget.snapshot() if budget else None
    summary["pending_events"] = len(state["backlog"])
    write_json(output / "weekly-report.json", redact(summary))
    return summary


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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--limit",
        type=analysis.bounded(1, 3),
        default=3,
        help="Representative conversations per repair topic",
    )
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    try:
        result = run(
            read_json(args.config),
            dry_run=args.dry_run,
            limit=args.limit,
            seed=args.seed,
        )
        print(encode(redact(result)))
        if result["status"] == "partial_failure":
            raise ValueError(
                "One or more automation investigations failed; inspect weekly-report.json"
            )
    except Exception:
        analysis.callback("FAILED")
        raise
    analysis.callback("COMPLETED")


if __name__ == "__main__":
    main()
