import json

import pytest

import backlog
import topic_issue
from common import read_json
from github_io import github_pages
from public_status import read_marker
from tracked_work import matching
from test_topic_issue import GitHub


def test_topic_publish_labels_privacy_and_identity(tmp_path, monkeypatch):
    config = {"repository": "Example/source", "project_id": "p", "signal_id": "s"}
    context = {
        "state_dir": str(tmp_path),
        "inventory_path": "inventory.json",
        "records_path": "records.json",
        "triage_issue": None,
        "triage_evidence_dir": "weekly",
    }
    topic = {
        "id": "a" * 20,
        "title": "Verify submitted comments",
        "priority": 1,
        "representative_event_ids": ["private-finding"],
        "brief": {
            "problem": "A blank comment can count as submitted.",
            "example": "A review completed with an empty message.",
            "prevention": "Verify the saved comment before reporting success.",
            "validation": "Exercise one empty and one populated submission.",
            "submission": [
                {"heading": "Steps to reproduce", "text": "Submit an empty comment."}
            ],
            "labels": ["Bug"],
        },
    }
    rows, writes = [], []
    monkeypatch.setattr(backlog, "ensure_labels", lambda repository: {"bug": "bug"})
    monkeypatch.setattr(
        topic_issue,
        "github_pages",
        lambda endpoint, *_: [] if "/comments?" in endpoint else rows,
    )

    def request(args):
        payload = read_json(tmp_path / "output" / "create-issue.json")
        row = payload | {
            "number": 1,
            "html_url": "https://github.com/Example/source/issues/1",
            "state": "open",
        }
        rows.append(row)
        writes.append(args)
        return json.dumps(row)

    monkeypatch.setattr(topic_issue, "command", request)
    for _ in range(2):
        assert backlog.publish(
            config, context, topic, None, tmp_path / "output"
        ).endswith("/1")
    assert len(writes) == 1

    row = rows[0]
    assert row["labels"] == [
        "automation-improvement",
        backlog.TOPIC,
        backlog.READY,
        "priority:P1",
        "bug",
    ]
    assert "### Steps to reproduce\n\nSubmit an empty comment." in row["body"]
    status = read_marker(row["body"])
    assert (status["status"], status["owner"], status["next_actor"]) == (
        "ready",
        "native",
        "dispatcher",
    )
    assert "**Next action:** " + status["next_action"] in row["body"]
    item = {"kind": "issue", "body": row["body"], "url": row["html_url"]}
    assert backlog.identity(item, backlog.namespace(config, tmp_path)) == topic["id"]
    assert matching(topic | {"id": "b" * 20}, [item]) == [item["url"]]
    assert not topic_issue.public_issue_errors(row["body"])
    assert "private-finding" not in row["body"]
    ref = backlog.REFERENCE.search(row["body"])[1]
    assert (
        read_json(
            backlog.evidence_root(tmp_path, config["repository"], topic["id"]) / ref
        )["topic"]
        == topic
    )
    topic["brief"]["example"] = "In PrivateOrg/private-repo the comment was empty."
    with pytest.raises(ValueError, match="public text"):
        backlog.publish(
            config, context, topic, None, tmp_path / "preview", preview=True
        )
    assert len(writes) == 1


def test_recovered_topic_keeps_human_body_labels_and_distinct_private_inputs(
    tmp_path, monkeypatch
):
    config = {
        "repository": "Example/source",
        "finding_source": "portable",
        "source_namespace": "ci",
    }
    context = {
        "state_dir": str(tmp_path),
        "inventory_path": "batch.json",
        "records_path": "records.json",
        "triage_issue": None,
        "triage_evidence_dir": "weekly",
    }
    candidate = {
        "id": "a" * 20,
        "title": "Missing report",
        "priority": 2,
        "representative_event_ids": ["one"],
        "brief": dict.fromkeys(backlog.BRIEF_FIELDS, "A required report was missing."),
    }
    github = GitHub()
    monkeypatch.setattr(topic_issue, "command", github)

    def pages(endpoint, request):
        if "labels=" in endpoint:
            return [
                i
                for i in github.issues
                if {"name": "automation-improvement"} in i["labels"]
            ]
        return github_pages(endpoint, request)

    monkeypatch.setattr(topic_issue, "github_pages", pages)
    monkeypatch.setattr(backlog, "ensure_labels", lambda repo: {})
    first = backlog.publish(config, context, candidate, None, tmp_path / "first")
    issue = github.issues[0]
    initial = backlog.initial_status_text(True)
    status = read_marker(issue["body"])
    assert (status["status"], status["owner"], status["next_actor"]) == (
        "awaiting_enrollment",
        "external",
        "maintainer",
    )
    assert all(
        label in status["next_action"] for label in (backlog.FACTORY, backlog.CUSTOMER)
    )
    assert "**Next action:** " + status["next_action"] in issue["body"]
    assert not github.comments
    current_body = issue["body"]
    old_body = current_body.replace(initial, backlog.LEGACY_EXTERNAL_STATUS)
    issue["body"] = old_body
    assert (
        backlog.publish(config, context, candidate, None, tmp_path / "recover") == first
    )
    assert issue["body"] == current_body
    previous = backlog.REFERENCE.search(issue["body"])[1]
    issue["body"] = "Human instructions.\n" + old_body
    issue["labels"] = [{"name": "hold"}]
    second = candidate | {"representative_event_ids": ["two"]}
    assert backlog.publish(config, context, second, None, tmp_path / "retry") == first
    current = backlog.REFERENCE.search(issue["body"])[1]
    data = read_json(
        backlog.evidence_root(tmp_path, config["repository"], candidate["id"]) / current
    )
    assert data["previous_reference"] == previous
    assert data["topic"]["representative_event_ids"] == ["two"]
    assert len(github.issues) == 1 and issue["labels"] == [{"name": "hold"}]
    assert issue["body"].startswith("Human instructions.\n")
    assert backlog.LEGACY_EXTERNAL_STATUS in issue["body"]


@pytest.mark.parametrize("changed", ["enrolled", "published", "human", "hold"])
def test_legacy_enrollment_migration_preserves_manually_or_externally_managed_status(
    changed, tmp_path, monkeypatch
):
    github = GitHub()
    monkeypatch.setattr(topic_issue, "command", github)
    monkeypatch.setattr(
        topic_issue, "github_pages", lambda endpoint, _: github_pages(endpoint, github)
    )
    repo = "Example/source"
    body = (
        f"<!-- automation-improvement-backlog: {'a' * 20}/{'b' * 20} -->\n"
        + backlog.block("backlog-status", backlog.LEGACY_EXTERNAL_STATUS)
    )
    row = {
        "number": 1,
        "html_url": f"https://github.com/{repo}/issues/1",
        "body": body,
        "state": "open",
        "labels": [
            {"name": name} for name in (backlog.TOPIC, backlog.EXTERNAL, backlog.READY)
        ],
    }
    github.issues.append(row)
    if changed == "human":
        row["body"] = body.replace(
            backlog.LEGACY_EXTERNAL_STATUS,
            backlog.LEGACY_EXTERNAL_STATUS + "\nInvestigating manually.",
        )
    elif changed == "published":
        github.comments.append(
            {
                "id": 10,
                "issue_url": f"https://api.github.com/repos/{repo}/issues/1",
                "body": "<!-- automation-improvement-factory: 1 -->\nReview underway.",
            }
        )
    else:
        row["labels"].append(
            {"name": backlog.FACTORY if changed == "enrolled" else "needs-triage"}
        )
    before = row["body"]
    issue = topic_issue.TopicIssue(repo, tmp_path, tmp_path, row, [])
    backlog.refresh_enrollment_status({}, issue)
    assert row["body"] == before
    assert not any("PATCH" in call for call in github.calls)


def test_summary_backlink_requires_a_verified_public_home_for_other_destinations(
    monkeypatch,
):
    home, destination = "Company/home", "Community/project"
    url = f"https://github.com/{home}/issues/2"
    weekly = {"url": url}
    for visibility in ("private", "internal", None):
        monkeypatch.setattr(
            backlog, "command", lambda *a: json.dumps({"visibility": visibility})
        )
        assert not backlog.weekly_summary_link(home, destination, weekly)
    monkeypatch.setattr(
        backlog, "command", lambda *a: json.dumps({"visibility": "public"})
    )
    assert url in backlog.weekly_summary_link(home, destination, weekly)
    assert not backlog.weekly_summary_link(
        home, destination, {"url": "https://github.com/Unrelated/repo/issues/3"}
    )
