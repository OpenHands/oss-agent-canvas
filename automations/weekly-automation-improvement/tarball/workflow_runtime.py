import argparse
import fcntl
import os
import re
import sys
from pathlib import Path

from budget import RunBudget
from common import encode, read_json, redact
from repair_policy import resolve as repair_policy


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


def run(config, run_locked, *, dry_run=False, limit=3, seed=None):
    if type(config.get("isolated_validation", False)) is not bool:
        raise ValueError("isolated_validation must be a boolean")
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


def callback(status):
    import urllib.request

    url = os.environ.get("AUTOMATION_CALLBACK_URL")
    if url:
        request = urllib.request.Request(
            url,
            data=encode(
                {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID")}
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer "
                + os.environ.get("AUTOMATION_CALLBACK_API_KEY", ""),
            },
        )
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except OSError:
            print("Completion callback failed", file=sys.stderr)


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

    try:
        config = read_json(args.config)
        if args.input:
            config["input"] = str(args.input.resolve())
        result = run(config, dry_run=args.dry_run, limit=args.limit, seed=args.seed)
        print(encode(redact(result)))
        if result["status"] in (
            "partial_failure",
            "collection_incomplete",
            "discovery_incomplete",
            "triage_incomplete",
        ):
            raise ValueError("Weekly run incomplete; inspect weekly-report.json")
    except Exception:
        callback("FAILED")
        raise
    callback("COMPLETED")
