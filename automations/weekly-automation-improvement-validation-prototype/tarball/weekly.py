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
from common import encode, read_json, redact, timestamp, write_json
from events import collect_events
from laminar import Laminar
from patches import command, load_source, propose_fix
from sources import github_auth, repository_index, resolve_source


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


def existing_prs(repository):
    rows = json.loads(
        command(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
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
    }
    path = output / "source-config.json"
    write_json(path, value)
    return path, samples, errors


def run(config, *, dry_run=False, limit=3, seed=None):
    if not 1 <= limit <= 3:
        raise ValueError("Weekly runs may investigate one to three findings")
    load_environment(config)
    state_dir = Path(config["state_dir"]).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(state_dir / "weekly.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped_overlap"}
        return run_locked(config, state_dir, dry_run=dry_run, limit=limit, seed=seed)
    finally:
        os.close(lock)


def run_locked(config, state_dir, *, dry_run, limit, seed):
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
        read_json(state_path)
        if state_path.exists()
        else {"events": {}, "publications": {}}
    )
    if not dry_run and week in state.get("completed_weeks", {}):
        summary = {
            "status": "weekly_batch_already_completed",
            "week": week,
            "previous": state["completed_weeks"][week],
            "output": str(output),
        }
        write_json(output / "weekly-report.json", summary)
        return summary
    github_auth(config)
    prs = existing_prs(config["repository"])
    handled = set(state["events"])
    for pr in prs:
        handled.update(
            re.findall(
                r"<!-- automation-improvement-event: ([0-9a-f-]{36}) -->", pr["body"]
            )
        )
    args = analysis.argument_parser().parse_args([])
    args.project_id = str(uuid.UUID(config["project_id"]))
    args.signal_id = str(uuid.UUID(config["signal_id"]))
    args.app_url = config.get("app_url", "https://laminar.app.all-hands.dev")
    args.analyzer = "flow1"
    args.limit, args.seed = limit, seed
    args.population_limit = 10000
    args.output = output / "collection"
    args.output.mkdir(mode=0o700)
    snapshot = collect_events(Laminar(args), handled)
    summary = {
        "status": "findings_unavailable"
        if snapshot["selection"]["selected_event_ids"]
        else "no_unhandled_findings",
        "week": week,
        "output": str(output),
        "selection": snapshot["selection"],
        "signal_runs": snapshot["signal_run_summary"],
        "collection_errors": snapshot["collection_errors"],
        "publication": "disabled" if dry_run else "none",
    }
    try:
        already_published = week in state["publications"] or any(
            week_key(timestamp(pr["createdAt"])) == week for pr in prs
        )
        if already_published and not dry_run:
            summary["status"] = "weekly_pr_limit_reached"
        elif snapshot["samples"]:
            path, samples, errors = source_config(config, snapshot, output)
            summary["source_errors"] = errors
            if not samples:
                summary["status"] = "no_approved_source_match"
            else:
                snapshot["samples"] = [
                    sample | {"case_id": f"c{index}"}
                    for index, sample in enumerate(samples, 1)
                ]
                snapshot["source"] = load_source(path)
                snapshot["existing_improvement_prs"] = prs
                frozen = output / "snapshot-with-source.json"
                write_json(frozen, snapshot)
                args.input, args.source_config = frozen, path
                args.output = output / "analysis"
                analysis.run(args)
                report = read_json(args.output / "report.json")
                by_id = {finding["id"]: finding for finding in report["findings"]}
                updates = {}
                for rejection in report["rejected_findings"]:
                    event_id = by_id[rejection["finding_id"]]["signal_event_id"]
                    updates[event_id] = {
                        "status": "rejected",
                        "reason": rejection["reason"],
                        "at": now.isoformat(),
                    }
                summary.update(
                    status="analyzed",
                    findings=len(report["findings"]),
                    proposals=len(report["proposals"]),
                    rejections=report["rejected_findings"],
                    coordinator_measurements=[
                        measurement
                        for measurement in report["measurements"]
                        if measurement.get("backend") != "persisted_flow1_signals"
                    ],
                    behavioral_evaluation="not_performed",
                )
                if report["proposals"]:
                    proposal = report["proposals"][0]
                    if (
                        proposal["patch"]
                        and config.get("publish_drafts", False)
                        and not dry_run
                    ):
                        state["publications"][week] = {
                            "status": "reserved",
                            "report": str(args.output / "report.json"),
                        }
                        save_state(state_path, state)
                        args.publish_draft = True
                        result = propose_fix(args, report, snapshot)
                        summary["publication"] = result
                        state["publications"][week] = result
                        for finding_id in proposal["finding_ids"]:
                            updates[by_id[finding_id]["signal_event_id"]] = {
                                "status": "draft_pr",
                                "pr_url": result["pr_url"],
                                "at": now.isoformat(),
                            }
                    elif not proposal["patch"]:
                        for finding_id in proposal["finding_ids"]:
                            updates[by_id[finding_id]["signal_event_id"]] = {
                                "status": "diagnostic_only",
                                "at": now.isoformat(),
                            }
                if not dry_run:
                    state["events"].update(updates)
                    save_state(state_path, state)
        if not dry_run:
            state.setdefault("completed_weeks", {})[week] = str(
                output / "weekly-report.json"
            )
            save_state(state_path, state)
        write_json(output / "weekly-report.json", redact(summary))
        return summary
    except Exception as error:
        summary.update(status="failed", error=str(error)[:1000])
        write_json(output / "weekly-report.json", redact(summary))
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".config/automation-improvement/config.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=analysis.bounded(1, 3), default=3)
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
    except Exception:
        analysis.callback("FAILED")
        raise
    analysis.callback("COMPLETED")


if __name__ == "__main__":
    main()
