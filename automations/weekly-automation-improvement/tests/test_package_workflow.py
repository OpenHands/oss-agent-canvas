import json
import runpy
import shutil
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import workflow_runtime

ROOT = Path(__file__).resolve().parents[1]
packaging = runpy.run_path(str(ROOT / "package_workflow.py"))


def test_discovery_with_installed_finder_uses_system_python(tmp_path):
    folder = packaging["package"]("discovery", tmp_path, without_sdk=True)
    manifest = json.loads((folder / "automation.yaml").read_text())
    assert manifest["entrypoint"] == "python3 triage_workflow.py --max-run-seconds 1500"
    assert manifest["enabled"] is False and manifest["setup_script_path"] is None
    assert (
        not manifest["tarball_executables"]
        and not (folder / "tarball/setup.sh").exists()
    )
    with pytest.raises(ValueError, match="standalone discovery"):
        packaging["package"]("native-repair", tmp_path, without_sdk=True)


@pytest.mark.parametrize("stage", list(packaging["STAGES"]))
def test_generated_entrypoint_and_setup_match_the_disabled_stage(tmp_path, stage):
    folder = packaging["package"](stage, tmp_path)
    fields = json.loads((folder / "automation.yaml").read_text())
    assert fields["enabled"] is False
    assert fields["trigger"]["type"] == "cron"
    assert fields["tarball_source"] == {"type": "internal", "url": None}
    tarball = folder / "tarball"
    modules = ", ".join(sorted(path.stem for path in tarball.glob("*.py")))
    subprocess.run(
        [sys.executable, "-B", "-E", "-S", "-c", f"import {modules}"],
        cwd=tarball,
        check=True,
        capture_output=True,
    )
    _, script, *args = shlex.split(fields["entrypoint"])
    assert script == packaging["STAGES"][stage][1]
    if stage == "factory-handoff":
        assert not (tarball / "setup.sh").exists()
        assert fields["setup_script_path"] is None and not fields["tarball_executables"]
        customer = subprocess.run(
            [sys.executable, "-B", "-E", "-S", "customer_workflow.py", "--help"],
            cwd=tarball,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "{claim,report,reconcile}" in customer.stdout
        config = tmp_path / "config.json"
        config.write_text("{}")
        result = subprocess.run(
            [sys.executable, "-E", "-S", script, "--config", str(config)],
            cwd=tarball,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(result.stdout) == {"status": "disabled", "issues": {}}
    else:
        assert (tarball / fields["setup_script_path"]).read_bytes() == (
            ROOT / "tarball/setup.sh"
        ).read_bytes()
        assert fields["tarball_executables"] == ["setup.sh"]
        assert args[0] == "--max-run-seconds" and 0 < int(args[1]) < fields["timeout"]
        subprocess.run(
            [sys.executable, "-E", "-S", script, *args, "--help"],
            cwd=tarball,
            check=True,
            capture_output=True,
        )
    assert not (tarball / "weekly.py").exists()
    assert (tarball / "factory_handoff.py").exists() == (stage == "factory-handoff")
    with pytest.raises(FileExistsError):
        packaging["package"](stage, tmp_path)


def test_unrelated_worker_files_do_not_enter_public_packages(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(
        ROOT / "tarball",
        source / "tarball",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(ROOT / "package_workflow.py", source)
    for name in ("credentials.py", "credentials.json", "private.log"):
        (source / "tarball" / name).write_text("private fixture")
    package = runpy.run_path(str(source / "package_workflow.py"))["package"]
    for stage in packaging["STAGES"]:
        files = set(
            p.name for p in (package(stage, tmp_path / "out") / "tarball").iterdir()
        )
        assert files.isdisjoint({"credentials.py", "credentials.json", "private.log"})


@pytest.mark.parametrize("configured,expected", [(10800, 1500), (900, 900)])
def test_packaged_budget_preserves_shorter_operator_limits(
    tmp_path, monkeypatch, configured, expected
):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"max_run_seconds": configured}))
    monkeypatch.delenv("AUTOMATION_CALLBACK_URL", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["stage.py", "--config", str(config), "--max-run-seconds", "1500"]
    )

    def run(config, **kwargs):
        assert config["max_run_seconds"] == expected
        return {"status": "completed"}

    workflow_runtime.main(run)
