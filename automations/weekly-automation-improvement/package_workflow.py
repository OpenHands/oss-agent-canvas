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
    "tracked_work.py",
    "triage.py",
    "triage_agent.py",
    "triage_workflow.py",
    "workflow_runtime.py",
    "setup.sh",
)
STAGE_FILES = {
    "discovery": DISCOVERY_FILES,
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


def package(stage, directory):
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
        native = stage != "factory-handoff"
        for filename in STAGE_FILES[stage]:
            shutil.copy2(root / "tarball" / filename, tarball)
        manifest = {
            "enabled": False,
            "name": name,
            "entrypoint": f".venv/bin/python {entrypoint} --max-run-seconds 1500"
            if native
            else f"python3 {entrypoint}",
            "setup_script_path": "setup.sh" if native else None,
            "tarball_executables": ["setup.sh"] if native else [],
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
    options = parser.parse_args()
    print(package(options.stage, options.directory))
