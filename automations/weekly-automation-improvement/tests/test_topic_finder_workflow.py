import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import triage_workflow
import weekly
from backlog import EXTERNAL, RECURRENT, TOPIC, ledger_directory, namespace
from common import read_json, replace_json
from repair_policy import resolve
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


@pytest.mark.parametrize(
    "override", [False, True], ids=["default-home", "separate-backlog"]
)
def test_command_finder_publishes_updates_and_replays(tmp_path, monkeypatch, override):
    source_repo = "Example/source"
    destination = "Example/central" if override else source_repo
    repos = {source_repo: GitHub(private=True), destination: GitHub(private=False)}
    github = repos[destination]
    guide_revision = "e" * 40
    subprocess_run = subprocess.run
    lose_recurrence_reply = True

    def run(argv, *args, **kwargs):
        nonlocal lose_recurrence_reply
        if argv[0] != "gh":
            return subprocess_run(argv, *args, **kwargs)
        if argv[1] == "label":
            assert argv[argv.index("--repo") + 1] == destination
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        endpoint = next(item for item in argv if item.startswith("repos/"))
        repo = "/".join(endpoint.split("/")[1:3])
        remote = repos[repo]
        if endpoint.endswith("commits/HEAD"):
            assert repo == destination
            output = json.dumps({"sha": guide_revision})
        elif "/git/trees/" in endpoint:
            assert repo == destination and guide_revision in endpoint
            output = json.dumps({"tree": [{"path": "CONTRIBUTING.md", "type": "blob"}]})
        elif "/contents/" in endpoint:
            assert repo == destination and endpoint.endswith(
                "CONTRIBUTING.md?ref=" + guide_revision
            )
            output = json.dumps(
                {"encoding": "base64", "size": 5, "content": "R3VpZGU="}
            )
        elif "/labels?" in argv[-1]:
            output = "[]"
        elif "/issues?" in argv[-1]:
            query = parse_qs(urlsplit(argv[-1]).query)
            output = json.dumps(
                [
                    issue
                    for issue in remote.issues
                    if query.get("state", ["open"])[0] in ("all", issue["state"])
                    and all(
                        {"name": label} in issue["labels"]
                        for label in query.get("labels", [])
                    )
                ]
            )
        else:
            output = remote(argv)
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

    if override:
        config.update(issue_repository=destination, repair_scope="customer_stack")
        assert resolve(config)["repositories"] == [source_repo]

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
    assert first["issue_repository"] == destination
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
    state = tmp_path / "state"
    ledger = ledger_directory(config, state) / "state.json"
    assert len(read_json(ledger)["events"]) == 4
    assert all(
        issue["html_url"].startswith(f"https://github.com/{destination}/issues/")
        for issue in github.issues
    )
    if override:
        source = repos[source_repo]
        assert not source.issues and not source.comments
        assert source_repo not in json.dumps(github.issues + github.comments)
        assert not (state / "state.json").exists()
        cards = read_json(Path(first["output"]) / "triage/triage-input.json")
        assert cards["guide_revision"] == guide_revision
        assert cards["issue_guide_files"] == ["CONTRIBUTING.md"]
    else:
        same = config | {"issue_repository": source_repo.upper()}
        assert ledger_directory(same, state) == state
        assert namespace(same, state) == namespace(config, state)


@pytest.mark.parametrize(
    "value", ["", None, "https://github.com/Example/issues", "../backlog"]
)
def test_invalid_issue_repository_rejected_before_worker_writes(tmp_path, value):
    state = tmp_path / "worker"
    with pytest.raises(ValueError, match="issue_repository"):
        triage_workflow.run(
            {
                "repository": "Example/source",
                "issue_repository": value,
                "state_dir": str(state),
            }
        )
    assert not state.exists()


@pytest.mark.parametrize(
    "detail,empty",
    [
        ("gh: Git Repository is empty. (HTTP 409)", True),
        ("gh: Conflict (HTTP 409)", False),
        ("gh: Not Found (HTTP 404)", False),
    ],
)
def test_destination_guide_lookup_requires_a_readable_or_empty_repository(
    monkeypatch, detail, empty
):
    def unreadable(argv):
        raise subprocess.CalledProcessError(1, argv, stderr=detail)

    monkeypatch.setattr(triage_workflow, "command", unreadable)
    if empty:
        assert triage_workflow.destination_guide_source("Example/tickets", {}) == {
            "repository": "Example/tickets"
        }
    else:
        with pytest.raises(subprocess.CalledProcessError):
            triage_workflow.destination_guide_source("Example/tickets", {})
