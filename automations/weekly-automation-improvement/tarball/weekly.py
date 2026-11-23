from pathlib import Path

from common import redact, write_json
from triage_workflow import run_locked as prepare
from workflow_runtime import main, run as run_workflow


def run_locked(config, state_dir, *, dry_run, limit, seed, budget):
    summary = prepare(
        config, state_dir, dry_run=dry_run, limit=limit, seed=seed, budget=budget
    )
    if not dry_run and summary["status"] != "discovery_incomplete":
        from repair_workflow import run_locked as repair

        result = repair(
            config, state_dir, dry_run=False, limit=limit, seed=seed, budget=budget
        )
        summary["repair"] = result
        summary["topics"] = result["topics"]
        if summary["status"] not in ("triage_incomplete", "collection_incomplete"):
            summary["status"] = result["status"]
        summary["budget"] = budget.snapshot()
    write_json(Path(summary["output"]) / "weekly-report.json", redact(summary))
    return summary


def run(config, *, dry_run=False, limit=3, seed=None):
    return run_workflow(config, run_locked, dry_run=dry_run, limit=limit, seed=seed)


if __name__ == "__main__":
    main(run)
