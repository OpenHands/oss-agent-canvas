import argparse
import json
import shutil
import tempfile
from pathlib import Path

STAGES = {
    "discovery": (
        "Discover automation improvements",
        "triage_workflow.py",
        "0 8 * * 1",
    ),
    "native-repair": (
        "Repair automation improvements",
        "repair_workflow.py",
        "30 8 * * 1",
    ),
    "factory-handoff": (
        "Reconcile Factory handoffs",
        "factory_handoff.py",
        "*/10 * * * *",
    ),
}


SHARED_FILES = (
    "attribution.py",
    "backlog.py",
    "common.py",
    "finding_record.py",
    "flow1.py",
    "flow1-pricing.json",
    "github_io.py",
    "laminar.py",
    "public_status.py",
    "raw_evidence.py",
    "read_status.py",
    "repair_policy.py",
    "sources.py",
    "topic_issue.py",
)
DISCOVERY_FILES = SHARED_FILES + (
    "agent_runtime.py",
    "budget.py",
    "epic.py",
    "events.py",
    "signal_history.py",
    "tracked_work.py",
    "topic_finder.py",
    "trace_filter.py",
    "triage.py",
    "triage_agent.py",
    "triage_workflow.py",
    "workflow_runtime.py",
    "setup.sh",
)
STAGE_FILES = {
    "discovery": DISCOVERY_FILES + ("collect_signals.py",),
    "native-repair": DISCOVERY_FILES
    + (
        "backlog_dispatch.py",
        "cohorts.py",
        "parallel.py",
        "repair_workflow.py",
        "repository_snapshot.py",
        "topic_agent.py",
    ),
    "factory-handoff": SHARED_FILES
    + ("customer_workflow.py", "factory_handoff.py", "factory_linear.py"),
}


def package(stage, directory, *, without_sdk=False):
    if without_sdk and stage != "discovery":
        raise ValueError("--without-sdk applies to standalone discovery")
    name, entrypoint, schedule = STAGES[stage]
    root = Path(__file__).resolve().parent
    destination = directory / ("automation-improvement-" + stage)
    directory.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        folder = Path(tmp) / destination.name
        tarball = folder / "tarball"
        tarball.mkdir(parents=True)
        use_sdk = stage != "factory-handoff" and not without_sdk
        for filename in STAGE_FILES[stage]:
            if without_sdk and filename == "setup.sh":
                continue
            shutil.copy2(root / "tarball" / filename, tarball)
        python = ".venv/bin/python" if use_sdk else "python3"
        run = f"{python} {entrypoint}"
        if stage != "factory-handoff":
            run += " --max-run-seconds 1500"
        manifest = {
            "enabled": False,
            "name": name,
            "entrypoint": run,
            "setup_script_path": "setup.sh" if use_sdk else None,
            "tarball_executables": ["setup.sh"] if use_sdk else [],
            "tarball_source": {"type": "internal", "url": None},
            "keep_alive": False,
            "timeout": 1800,
            "trigger": {"type": "cron", "schedule": schedule, "timezone": "UTC"},
        }
        (folder / "automation.yaml").write_text(json.dumps(manifest, indent=2) + "\n")
        folder.rename(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--without-sdk",
        action="store_true",
        help="Package standalone discovery for an operator-installed topic finder",
    )
    options = parser.parse_args()
    if options.without_sdk and options.stage != "discovery":
        parser.error("--without-sdk applies to standalone discovery")
    print(package(options.stage, options.directory, without_sdk=options.without_sdk))
