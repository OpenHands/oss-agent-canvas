import json
from pathlib import Path

import topic_issue
import triage_workflow
from backlog import block
from common import read_json
from test_topic_issue import GitHub


def test_triage_publishes_topics_without_repair_code(prepared, monkeypatch):
    import sys

    config, topics = prepared
    for candidate in topics:
        candidate.update(
            title="Investigate missing publication", reason="Publication was required."
        )
    github = GitHub()
    github.issues.append(
        {
            "number": 1,
            "html_url": "https://github.com/Example/source/issues/1",
            "body": block("triage-plan", ""),
        }
    )
    monkeypatch.setattr(topic_issue, "command", github)
    monkeypatch.setitem(sys.modules, "repair_workflow", None)
    monkeypatch.setitem(sys.modules, "parallel", None)
    published = []

    def publish(config, context, topic, existing, output):
        published.append(topic)
        assert existing is None
        assert Path(context["inventory_path"]).is_file()
        return f"https://github.com/Example/source/issues/{len(published) + 1}"

    monkeypatch.setattr(triage_workflow, "publish", publish)
    monkeypatch.setattr(triage_workflow, "existing_work", lambda _: [])
    refreshes = []

    def refresh(config, state_dir, state, output, weekly):
        refreshes.append((len(state["events"]), weekly))
        return {"status": "refreshed"}

    monkeypatch.setattr(triage_workflow, "refresh_epic", refresh)
    result = triage_workflow.run(config, seed=42)
    assert result["status"] == "published"
    assert result["publication"]["status"] == "verified"
    assert (
        "**P0** [Investigate missing publication](https://github.com/Example/source/issues/2)"
        in github.issues[0]["body"]
    )
    assert "Publication was required." in github.issues[0]["body"]
    assert published == topics
    assert result["budget"]["reported_inference_cost_usd"] == 0.1
    state = read_json(Path(config["state_dir"]) / "state.json")
    assert not state["topic_weeks"]
    assert not state["backlog"]
    assert len(state["events"]) == 3
    output = Path(result["output"])
    assert not (output / "repair-request.json").exists()
    assert not (output / "workers").exists()
    assert triage_workflow.run(config)["status"] == "no_unhandled_findings"
    assert len(published) == 3
    assert [count for count, _ in refreshes] == [3, 3]
    assert refreshes[0][1] and refreshes[1][1] is None


def test_triage_preview_does_not_publish_topics(prepared, monkeypatch):
    import pytest

    config, _ = prepared
    monkeypatch.setattr(
        triage_workflow, "publish", lambda *a, **k: pytest.fail("publish")
    )
    monkeypatch.setattr(
        triage_workflow, "refresh_epic", lambda *a: pytest.fail("refresh epic")
    )
    result = triage_workflow.run(config, dry_run=True)
    assert result["status"] == "preview"
    assert not (Path(config["state_dir"]) / "state.json").exists()


def test_empty_plan_explains_held_findings_without_publishing_agent_notes(
    prepared, monkeypatch
):
    config, _ = prepared
    github = GitHub()
    issue = {
        "number": 1,
        "html_url": "https://github.com/Example/source/issues/1",
        "body": block("triage-plan", ""),
    }
    github.issues.append(issue)
    monkeypatch.setattr(topic_issue, "command", github)
    monkeypatch.setattr(triage_workflow, "existing_work", lambda _: [])
    monkeypatch.setattr(
        triage_workflow,
        "triage",
        lambda *a: (
            [],
            {
                "triage_issue": {"url": issue["html_url"]},
                "luna_triage": {"no_action_reason": "confidential observation"},
            },
        ),
    )
    report = triage_workflow.run(config)
    assert report["selection"]["held_findings"] == report["pending_events"] == 3
    assert "held 3 for later triage" in issue["body"]
    assert report["selection"]["next_action"] in issue["body"]
    assert "confidential observation" not in issue["body"] + json.dumps(report)


def test_summary_patch_retried_after_topics_are_published(prepared, monkeypatch):
    import pytest

    config, topics = prepared
    for item in topics:
        item.update(title="Missing report", reason="The report was required.")
    github = GitHub()
    weekly = {
        "number": 1,
        "html_url": "https://github.com/Example/source/issues/1",
        "body": block("triage-plan", "") + "\n\nHuman notes stay.",
    }
    github.issues.append(weekly)
    fail_summary = True
    published = []

    def request(argv):
        if fail_summary and "PATCH" in argv:
            raise RuntimeError("Summary denied")
        return github(argv)

    def publish(config, context, topic, existing, output):
        published.append(topic)
        return f"https://github.com/Example/source/issues/{len(published) + 1}"

    monkeypatch.setattr(topic_issue, "command", request)
    monkeypatch.setattr(triage_workflow, "publish", publish)
    monkeypatch.setattr(triage_workflow, "existing_work", lambda _: [])
    first = triage_workflow.run(config)
    assert first["status"] == "triage_incomplete"
    assert first["publication"]["status"] == "failed"
    assert len(published) == 3
    assert "Missing report" not in weekly["body"]

    fail_summary = False
    monkeypatch.setattr(
        triage_workflow,
        "triage",
        lambda *args: pytest.fail("Re-ranked a published plan"),
    )
    second = triage_workflow.run(config)
    assert second["publication"] == {"status": "verified"}
    assert len(published) == 3
    assert "Missing report" in weekly["body"]
    assert "Human notes stay." in weekly["body"]
    state = read_json(Path(config["state_dir"]) / "state.json")
    assert not state["pending_discovery"]


def test_portable_lost_create_and_superseded_recurrence_reconcile(
    tmp_path, monkeypatch
):
    import backlog
    import epic
    import tracked_work
    import triage
    import triage_agent
    from budget import RunBudget
    from common import replace_json
    from github_io import work_item
    from test_portable_findings import batch, row
    from test_triage import topic

    home = "Example/source"
    human_url = f"https://github.com/{home}/issues/1"
    human_body = "A maintainer's report and reproduction."
    github = GitHub(private=False)
    github.issues.append(
        {
            "number": 1,
            "title": "Reported problem",
            "body": human_body,
            "html_url": human_url,
            "state": "open",
            "labels": [],
        }
    )
    lost_create = True
    allow_comment = False

    def request(argv):
        nonlocal lost_create
        if (
            "POST" in argv
            and f"repos/{home}/issues/1/comments" in argv
            and not allow_comment
        ):
            raise RuntimeError("Recurrence comment unavailable")
        response = github(argv)
        if "POST" in argv and f"repos/{home}/issues" in argv and lost_create:
            if backlog.TOPIC in (
                label["name"] for label in json.loads(response)["labels"]
            ):
                lost_create = False
                raise RuntimeError("Lost create response")
        return response

    for module in (topic_issue, tracked_work):
        monkeypatch.setattr(module, "command", request)
    monkeypatch.setattr(epic, "github_pages", lambda *args: github.issues)
    monkeypatch.setattr(backlog, "ensure_labels", lambda repo: {})
    monkeypatch.setattr(triage_workflow, "github_auth", lambda _: None)
    monkeypatch.setattr(triage_workflow, "repository_catalog", lambda _: {home: {}})
    monkeypatch.setattr(
        triage_workflow,
        "existing_work",
        lambda repo: [
            work_item(item, repo)
            for item in github.issues
            if not epic.is_index(item["body"])
        ],
    )
    ranked = []

    def rank(args, context, records, schema):
        ranked.append({r["id"] for r in records})
        plans = [
            topic(r["event_ref"], key=r["id"])
            | {
                "brief": {
                    "problem": "A required report was missing.",
                    "example": "A completed job returned empty output.",
                    "prevention": "Check the report before marking the job complete.",
                    "validation": "Verify empty and complete reports.",
                }
            }
            for r in records
        ]
        return (
            {"topics": plans, "no_action_reason": ""},
            {},
            triage_agent.prepare_issue(args, context, records),
        )

    monkeypatch.setattr(triage, "rank", rank)
    batch_path = tmp_path / "portable.json"
    config = {
        "repository": home,
        "checkout": str(tmp_path),
        "finding_source": "portable",
        "source_namespace": "ci-tests",
        "input": str(batch_path),
        "repair_scope": "customer_stack",
    }

    def run(*findings):
        replace_json(batch_path, batch(*findings))
        return triage_workflow.run_locked(
            config, tmp_path, dry_run=False, limit=3, seed=0, budget=RunBudget({})
        )

    new = row("missing-report", evidence="PRIVATE RUN CONTENT")
    old = row("prior-human", issue_url=human_url.lower(), evidence="RETIRED CONTENT")
    corrected = row(
        "corrected",
        issue_url=human_url.lower(),
        supersedes=[old["id"]],
        evidence="PRIVATE CORRECTION",
    )
    first = run(new, old)
    assert first["publication"] == {"status": "partial"}
    assert {item["status"] for item in first["topics"]} == {
        "publication_pending",
        "recurrence_pending",
    }
    child = next(
        item
        for item in github.issues
        if backlog.EXTERNAL in [label["name"] for label in item["labels"]]
    )
    reference = backlog.REFERENCE.search(child["body"])[1]
    saved_input = next(tmp_path.glob(f"issue-evidence/**/{reference}"))
    saved = read_json(saved_input)
    assert (
        f"[Weekly discovery summary]({saved['triage_issue']['url']})" in child["body"]
    )
    assert (
        "status comment records the latest handoff, any blocker, and the next actor"
        in child["body"]
    )
    assert "Ready for investigation" not in child["body"]
    assert read_json(Path(saved["records_path"]))[0]["source_namespace"] == "ci-tests"
    assert Path(saved["triage_evidence_dir"]).parent.name == first["week"]

    allow_comment = True
    second = run(corrected)
    assert second["publication"] == {"status": "verified"}
    assert {item["status"] for item in second["topics"]} == {
        "topic_created",
        "superseded",
    }
    assert second["pending_events"] == 1
    assert all(not c["issue_url"].endswith("/issues/1") for c in github.comments)
    third = run(old, corrected, new)
    assert [item["status"] for item in third["topics"]] == ["recurrence_recorded"]
    assert third["pending_events"] == 0
    assert run(old, corrected, new)["status"] == "no_unhandled_findings"
    assert ranked == [{old["id"], new["id"]}, {corrected["id"]}]

    state = read_json(tmp_path / "state.json")
    assert len(state["events"]) == 2 and not state["pending_discovery"]
    assert github.issues[0]["body"] == human_body and not github.issues[0]["labels"]
    assert (
        sum(backlog.EXTERNAL in [x["name"] for x in i["labels"]] for i in github.issues)
        == 1
    )
    comments = [c for c in github.comments if c["issue_url"].endswith("/issues/1")]
    assert len(comments) == 1
    assert any(comments[0]["html_url"] in item["body"] for item in github.issues)
    public = json.dumps([github.issues, github.comments])
    assert all(value["evidence"] not in public for value in (new, old, corrected))
    assert all(
        normalized
        in next(i["body"] for i in github.issues if i["body"].startswith(epic.EPIC))
        for normalized in (human_url, child["html_url"])
    )


def test_narrowed_scope_cannot_update_a_previously_managed_topic(tmp_path, monkeypatch):
    import pytest
    from backlog import namespace
    from common import write_json
    from test_tracked_work import work
    import tracked_work

    config = {
        "repository": "Example/source",
        "finding_source": "portable",
        "source_namespace": "ci-tests",
        "repair_scope": "customer_stack",
    }
    scope = namespace(config, tmp_path)
    target = work(42, repository="Foreign/private")
    target["body"] += f"\n<!-- automation-improvement-backlog: {scope}/{'a' * 20} -->"
    records = tmp_path / "records.json"
    write_json(records, [{"occurrence_id": "event"}])
    weekly = {
        "number": 1,
        "html_url": "https://github.com/Example/source/issues/1",
        "body": block("triage-plan", ""),
    }
    github = GitHub()
    github.issues.append(weekly)
    monkeypatch.setattr(topic_issue, "command", github)
    monkeypatch.setattr(
        tracked_work, "command", lambda *a: pytest.fail("Out-of-scope GitHub access")
    )
    monkeypatch.setattr(
        triage_workflow,
        "publish",
        lambda *a: pytest.fail("Out-of-scope backlog update"),
    )
    update = {
        "topic": {
            "id": "a" * 20,
            "title": "Known report problem",
            "priority": 2,
            "representative_event_ids": ["event"],
            "existing_work_urls": [target["url"]],
        },
        "existing_work_urls": [target["url"]],
        "status": "recurrence_pending",
    }
    pending = {
        "context": {
            "triage_issue": {"url": weekly["html_url"]},
            "triage_evidence_dir": str(tmp_path),
            "records_path": str(records),
        },
        "existing_work": {target["url"]: target},
        "topics": [update],
    }
    state = {"events": {}, "backlog": {"event": {}}}
    triage_workflow.reconcile_publication(
        config, scope, state, tmp_path / "state.json", pending, tmp_path, {"event"}
    )
    assert update["status"] == "recurrence_pending"
    assert not state["events"] and "event" in state["backlog"]
    assert "Foreign/private" not in weekly["body"]
