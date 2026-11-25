import argparse
import fcntl
import os
import re
from pathlib import Path

from budget import RunBudget
from common import callback, encode, read_json, redact
from repair_policy import resolve as repair_policy
from topic_finder import validate_command
from trace_filter import normalize_trace_filter


def load_environment(config, *, use_model=True):
    uses_laminar = config.get("finding_source", "laminar") == "laminar"
    if config.get("credentials_file") and (use_model or uses_laminar):
        path = Path(config["credentials_file"]).expanduser()
        if path.stat().st_mode & 0o077:
            raise ValueError("Credentials file must be owner-only")
        values = read_json(path)
        keys = []
        if use_model:
            keys.append("LITELLM_API_KEY")
        if uses_laminar:
            keys.append("LMNR_PROJECT_API_KEY")
        for name in keys:
            if name in values:
                os.environ[name] = values[name]
    if uses_laminar:
        os.environ["LMNR_BASE_URL"] = config["laminar_base_url"]
        os.environ.setdefault("LMNR_FORCE_HTTP", "true")
    if use_model:
        os.environ["LLM_BASE_URL"] = config["llm_base_url"]


def run(config, run_locked, *, dry_run=False, limit=3, seed=None, use_model=True):
    if "trace_filter" in config:
        if config.get("finding_source", "laminar") != "laminar":
            raise ValueError("trace_filter applies to Laminar Signal collection")
        normalize_trace_filter(config["trace_filter"])
    if "topic_finder_command" in config:
        validate_command(config["topic_finder_command"])
    if config.get("finding_source", "laminar") not in ("laminar", "portable"):
        raise ValueError("Unsupported finding_source")
    if config.get("execution_owner", "native") not in ("native", "external"):
        raise ValueError("Unsupported execution_owner")
    if type(config.get("isolated_validation", False)) is not bool:
        raise ValueError("isolated_validation must be a boolean")
    hold_labels = config.get("issue_hold_labels", [])
    if not isinstance(hold_labels, list) or any(
        not isinstance(label, str) or not label.strip() for label in hold_labels
    ):
        raise ValueError("issue_hold_labels must be a list of GitHub label names")
    if "max_prs_per_run" in config:
        raise ValueError(
            "Replace max_prs_per_run with max_topics_per_run; one topic may produce several PRs"
        )
    for name, default in (
        ("max_topics_per_run", 10),
        ("repair_concurrency", config.get("max_topics_per_run", 10)),
        ("validation_sample_size", 5),
        ("max_repair_attempts", 2),
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
    repair_policy(config)
    budget = RunBudget(config)
    load_environment(config, use_model=use_model)
    state_dir = Path(config["state_dir"]).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(state_dir / "weekly.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped_overlap"}
        return run_locked(
            config | {"_workflow_lock_fd": lock},
            state_dir,
            dry_run=dry_run,
            limit=limit,
            seed=seed,
            budget=budget,
        )
    finally:
        os.close(lock)


def main(run):
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
        "--issue-repository",
        metavar="OWNER/REPO",
        help="Override the GitHub repository for discovery issues and the repair backlog",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Read a frozen Laminar inventory or portable finding batch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepare and triage only; no repair agents or GitHub writes",
    )
    parser.add_argument("--limit", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--seed", type=int)
    parser.add_argument(
        "--max-run-seconds",
        type=int,
        help="Cap the configured run budget for this invocation",
    )
    args = parser.parse_args()

    try:
        config = read_json(args.config)
        if args.issue_repository is not None:
            config["issue_repository"] = args.issue_repository
        if args.max_run_seconds is not None:
            if args.max_run_seconds <= 0:
                raise ValueError("--max-run-seconds must be positive")
            config["max_run_seconds"] = min(
                RunBudget(config).maximum_seconds, args.max_run_seconds
            )
        if args.input:
            config["input"] = str(args.input.resolve())
        result = run(config, dry_run=args.dry_run, limit=args.limit, seed=args.seed)
        print(encode(redact(result)))
        if result["status"] in (
            "partial_failure",
            "review_incomplete",
            "collection_incomplete",
            "discovery_incomplete",
            "triage_incomplete",
        ):
            raise ValueError(
                f"Workflow incomplete; inspect the receipts under {result.get('output', 'the configured state directory')}"
            )
    except Exception:
        callback("FAILED")
        raise
    callback("COMPLETED")
