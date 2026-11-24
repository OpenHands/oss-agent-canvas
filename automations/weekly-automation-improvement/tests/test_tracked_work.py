import pytest

from budget import RunBudget
from common import topic_id, write_json
from test_triage import topic
from test_weekly import inventory, record, SIGNAL
import tracked_work
import triage
import triage_workflow as weekly


def work(number, key="known-problem", kind="issue", repository="Example/source"):
    return {
        "url": f"https://github.com/{repository}/{'pull' if kind == 'pull' else 'issues'}/{number}",
        "repository": repository,
        "title": "Tracked problem",
        "body": f"<!-- automation-improvement-topic: {topic_id('shared', key)} -->\nExisting human explanation.",
        "kind": kind,
        "state": "OPEN",
        "createdAt": "2026-10-25T00:00:00Z",
    }


def test_matching_uses_stable_identity_and_expands_semantic_matches_across_repos():
    existing = [
        work(10),
        work(11, kind="pull", repository="Example/sdk"),
        work(12, "other"),
    ]
    assert tracked_work.matching(
        {"id": topic_id("shared", "known-problem")}, existing
    ) == sorted([existing[0]["url"], existing[1]["url"]])
    assert tracked_work.matching(
        {"id": "new-name", "existing_work_urls": [existing[0]["url"]]}, existing
    ) == sorted([existing[0]["url"], existing[1]["url"]])


@pytest.mark.parametrize(
    "urls", [["https://github.com/Foreign/repo/issues/1"], [None], "not-a-list"]
)
def test_triage_rejects_foreign_or_malformed_work_references(urls):
    rows = [record()]
    _, references, _ = triage.prepare(rows, [work(10)], [])
    candidate = topic("e1") | {"existing_work_urls": urls}
    with pytest.raises(ValueError, match="existing work"):
        triage.validate(
            {"topics": [candidate], "no_action_reason": ""}, rows, references, 3
        )


def test_triage_requires_an_explicit_existing_work_decision():
    rows = [record()]
    _, references, _ = triage.prepare(rows, [work(10)], [])
    candidate = topic("e1")
    del candidate["existing_work_urls"]
    with pytest.raises(ValueError, match="Invalid topic fields"):
        triage.validate(
            {"topics": [candidate], "no_action_reason": ""}, rows, references, 3
        )


def test_discovery_includes_open_human_work_and_closed_improvements_not_weekly_briefs(
    monkeypatch,
):
    calls = []

    def pages(endpoint):
        calls.append(endpoint)
        return (
            [
                {
                    "html_url": "https://github.com/Example/source/issues/1",
                    "title": "Human issue",
                    "body": "Problem",
                    "state": "open",
                    "created_at": "2026-10-25",
                },
                {
                    "html_url": "https://github.com/Example/source/issues/2",
                    "title": "Weekly",
                    "body": "<!-- automation-improvement-triage: week/run -->",
                    "state": "open",
                    "created_at": "2026-10-25",
                },
                {
                    "html_url": "https://github.com/Example/source/issues/4",
                    "title": "Human report quoting a weekly marker",
                    "body": "Bug in weekly reconciliation:\n`<!-- automation-improvement-triage: week/run -->`",
                    "state": "open",
                    "created_at": "2026-10-25",
                },
            ]
            if "state=open" in endpoint
            else [
                {
                    "html_url": "https://github.com/Example/source/pull/3",
                    "title": "Old repair",
                    "state": "closed",
                    "created_at": "2026-10-25",
                    "pull_request": {},
                },
            ]
        )

    monkeypatch.setattr(weekly, "github_pages", pages)
    found = weekly.existing_work("Example/source")
    assert [item["title"] for item in found] == [
        "Human issue",
        "Human report quoting a weekly marker",
        "Old repair",
    ]
    assert "labels=automation-improvement" in calls[1]


def test_discovery_failure_does_not_dispatch_repairs(tmp_path, monkeypatch):
    frame = tmp_path / "inventory.json"
    write_json(frame, inventory([record()]))
    monkeypatch.setattr(weekly, "github_auth", lambda _: None)
    monkeypatch.setattr(weekly, "repository_catalog", lambda _: {"Example/source": {}})
    monkeypatch.setattr(
        weekly,
        "existing_work",
        lambda _: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        weekly,
        "triage",
        lambda *a: pytest.fail("Must not triage without existing-work discovery"),
    )
    config = {
        "repository": "Example/source",
        "checkout": str(tmp_path),
        "project_id": "00000000-0000-0000-0000-000000000001",
        "signal_id": SIGNAL,
        "input": str(frame),
    }
    summary = weekly.run_locked(
        config, tmp_path, dry_run=False, limit=3, seed=1, budget=RunBudget({})
    )
    assert (
        summary["status"] == "discovery_incomplete" and summary["pending_events"] == 1
    )
