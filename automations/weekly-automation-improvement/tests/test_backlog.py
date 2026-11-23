import json

import pytest

import backlog
import topic_issue
from common import read_json
from tracked_work import matching


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
        },
    }
    rows, writes = [], []
    monkeypatch.setattr(backlog, "ensure_labels", lambda repository: None)
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
    ]
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
