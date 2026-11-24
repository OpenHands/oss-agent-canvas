import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import triage_workflow
import weekly
from backlog import EXTERNAL, RECURRENT, TOPIC
from common import read_json, replace_json
from test_portable_findings import batch, row
from test_topic_issue import GitHub


@pytest.mark.parametrize(
    ("source", "owner", "preview", "needs_model"),
    [
        ("portable", "native", False, False),
        ("laminar", "external", False, False),
        ("laminar", "native", True, False),
        ("laminar", "native", False, True),
    ],
)
def test_combined_command_finder_only_loads_model_for_native_repairs(
    tmp_path, monkeypatch, source, owner, preview, needs_model
):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setattr(
        weekly, "run_locked", lambda *a, **kw: os.environ.get("LLM_BASE_URL")
    )
    config = {
        "repository": "Example/source",
        "state_dir": str(tmp_path),
        "finding_source": source,
        "execution_owner": owner,
        "topic_finder_command": [sys.executable],
        "laminar_base_url": "https://example.invalid",
    }
    if needs_model:
        config["llm_base_url"] = "https://model.invalid"
    assert weekly.run(config, dry_run=preview) == config.get("llm_base_url")


def test_command_finder_publishes_updates_and_replays(tmp_path, monkeypatch):
    github = GitHub(private=False)
    subprocess_run = subprocess.run
    lose_recurrence_reply = True

    def run(argv, *args, **kwargs):
        nonlocal lose_recurrence_reply
        if argv[0] != "gh":
            return subprocess_run(argv, *args, **kwargs)
        if "/labels?" in argv[-1]:
            output = "[]"
        elif "/issues?" in argv[-1]:
            query = parse_qs(urlsplit(argv[-1]).query)
            output = json.dumps(
                [
                    issue
                    for issue in github.issues
                    if query.get("state", ["open"])[0] in ("all", issue["state"])
                    and all(
                        {"name": label} in issue["labels"]
                        for label in query.get("labels", [])
                    )
                ]
            )
        else:
            output = github(argv)
            if (
                lose_recurrence_reply
                and "POST" in argv
                and argv[-3].endswith("/comments")
                and json.loads(output)["body"].startswith(
                    "<!-- automation-improvement-recurrence:"
                )
            ):
                lose_recurrence_reply = False
                raise subprocess.CalledProcessError(1, argv, stderr="Lost reply")
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(os, "environ", {"PATH": os.defpath, "GH_TOKEN": "fixture"})
    sample = Path(__file__).resolve().parents[1] / "examples/repeated_failure_finder.py"
    input_path = tmp_path / "batch.json"
    config = {
        "repository": "Example/source",
        "checkout": str(tmp_path),
        "state_dir": str(tmp_path / "state"),
        "finding_source": "portable",
        "source_namespace": "ci-tests",
        "input": str(input_path),
        "topic_finder_command": [sys.executable, str(sample)],
    }

    def discover(*units):
        replace_json(
            input_path,
            batch(
                *(
                    row(
                        unit,
                        unit=unit,
                        group="report-timeout",
                        summary="The report request exceeded its deadline.",
                        evidence="Private job transcript",
                    )
                    for unit in units
                )
            ),
        )
        return triage_workflow.run(config)

    first = discover("one", "two")
    assert [topic["status"] for topic in first["topics"]] == ["topic_created"]
    measurement = read_json(Path(first["output"]) / "triage/triage-result.json")
    assert measurement["triage_measurement"]["backend"] == "topic-finder-command"
    assert first["budget"]["requests"] == 0
    partial = discover("three", "four")
    assert partial["publication"] == {"status": "partial"}
    assert [topic["status"] for topic in partial["topics"]] == ["recurrence_pending"]
    config["topic_finder_command"] = [sys.executable, "-c", "raise SystemExit(19)"]
    second = discover("three", "four")
    assert second["publication"] == {"status": "verified"}
    assert [topic["status"] for topic in second["topics"]] == ["topic_updated"]
    assert first["topics"][0]["issue_url"] == second["topics"][0]["issue_url"]
    topic = next(issue for issue in github.issues if {"name": TOPIC} in issue["labels"])
    assert {EXTERNAL, RECURRENT} <= {label["name"] for label in topic["labels"]}
    recurrence = second["topics"][0]["comments"][0]["url"]
    assert [
        comment["html_url"]
        for comment in github.comments
        if comment["body"].startswith("<!-- automation-improvement-recurrence:")
    ] == [recurrence]
    weekly = next(issue for issue in github.issues if recurrence in issue["body"])
    epic = next(
        issue for issue in github.issues if issue["html_url"] == second["epic"]["url"]
    )
    assert weekly["html_url"] in epic["body"] and topic["html_url"] in epic["body"]
    counts = (len(github.issues), len(github.comments))
    assert discover("three", "four")["status"] == "no_unhandled_findings"
    assert (len(github.issues), len(github.comments)) == counts
    assert "Private job transcript" not in json.dumps(github.issues + github.comments)
    assert len(read_json(tmp_path / "state/state.json")["events"]) == 4
