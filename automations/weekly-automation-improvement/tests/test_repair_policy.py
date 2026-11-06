import pytest

from repair_policy import instructions, permits, resolve, resource_errors
from test_topic_agent import context, resources, finished_conversation
import topic_agent
from budget import RunBudget


def test_default_scope_uses_only_home_and_explicit_repositories():
    policy = resolve(
        {
            "repository": "Acme/automations",
            "repositories": {"Acme/config": {"checkout": "/existing/config"}},
        }
    )
    assert policy == {
        "scope": "customer_stack",
        "repositories": ["Acme/automations", "Acme/config"],
    }
    assert permits(policy, "acme/CONFIG")
    assert not permits(policy, "OpenHands/software-agent-sdk")
    assert not permits(policy, "Acme/unregistered")
    assert permits(
        resolve({"repository": "Acme/automations", "repair_scope": "any_authorized"}),
        "OpenHands/software-agent-sdk",
    )


@pytest.mark.parametrize("scope", ["anywhere", None, False, {}, []])
def test_unknown_scope_is_rejected(scope):
    with pytest.raises(ValueError, match="repair_scope"):
        resolve({"repository": "Acme/app", "repair_scope": scope})


@pytest.mark.parametrize("scope", ["customer_stack", "any_authorized"])
def test_scope_and_configuration_guidance_reach_repair_review_and_continuation(
    tmp_path, scope
):
    ctx = context(tmp_path)
    ctx["repair_policy"] = resolve({"repository": "Acme/app", "repair_scope": scope})
    for review, feedback in [
        (False, None),
        (True, None),
        (False, {"feedback": "Fix the replay"}),
    ]:
        current = ctx | {"repair_attempt": 2, "max_repair_attempts": 2}
        if feedback:
            current["review_feedback"] = feedback
        prompt = topic_agent.prompt(
            current, tmp_path / "context.json", tmp_path, review=review
        )
        assert instructions(ctx["repair_policy"]) in prompt
        assert "source of truth" in prompt
        assert "Do not change live settings" in prompt
        assert "not a restriction" not in prompt


def test_out_of_scope_outputs_block_review_without_writing_to_other_repositories(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    ctx["repair_policy"] = resolve({"repository": "Example/automation"})
    rows = resources()
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": rows, "errors": []}
    )
    recorded, held = [], []
    monkeypatch.setattr(
        topic_agent,
        "record_evidence",
        lambda c, o, phase, rs: recorded.extend(rs) or [],
    )
    monkeypatch.setattr(topic_agent, "hold_drafts", lambda rs: held.extend(rs) or [])
    monkeypatch.setattr(
        topic_agent,
        "run_conversation",
        lambda *a: pytest.fail("scope violation must block review"),
    )
    result = topic_agent.finish(
        {}, ctx, tmp_path, RunBudget({}), {"status": "finished"}
    )
    assert result["status"] == "review_incomplete"
    assert len(resource_errors(ctx["repair_policy"], rows)) == 2
    assert any("outside customer_stack" in x for x in result["link_errors"])
    assert {r["repository"] for r in recorded + held} == {"Example/automation"}


def test_any_authorized_accepts_evidenced_cross_repository_outputs(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    ctx["repair_policy"] = resolve(
        {"repository": "Example/automation", "repair_scope": "any_authorized"}
    )
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": resources(), "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    monkeypatch.setattr(topic_agent, "run_conversation", finished_conversation)
    result = topic_agent.finish(
        {}, ctx, tmp_path, RunBudget({}), {"status": "finished"}
    )
    assert result["status"] == "reviewed"
    assert result["review_decision"]["decision"] == "accepted"


def test_out_of_scope_recurrence_cannot_publish_a_comment(tmp_path, monkeypatch):
    import tracked_work
    from test_tracked_work import work

    existing = [work(1, repository="Upstream/sdk")]
    topic = {"id": "one", "existing_work_urls": [existing[0]["url"]]}
    monkeypatch.setattr(
        tracked_work, "command", lambda *a: pytest.fail("out-of-scope GitHub write")
    )
    with pytest.raises(ValueError, match="outside customer_stack"):
        tracked_work.comment(
            topic,
            existing,
            [],
            None,
            tmp_path,
            policy=resolve({"repository": "Acme/app"}),
        )


@pytest.mark.parametrize("source", ["/checkout", {"repository": "Upstream/sdk"}])
def test_catalog_cannot_disguise_an_upstream_checkout_as_a_customer_repository(source):
    with pytest.raises(ValueError, match="catalog name"):
        resolve({"repository": "Acme/app", "repositories": {"Acme/config": source}})


@pytest.mark.parametrize(
    "metadata,private",
    [
        ({"visibility": "public"}, False),
        ({"visibility": "private"}, True),
        ({"visibility": "internal", "private": False}, True),
        ({"private": True}, True),
    ],
)
def test_repository_visibility_is_resolved_from_github(monkeypatch, metadata, private):
    import json

    monkeypatch.setattr(topic_agent, "command", lambda argv: json.dumps(metadata))
    assert topic_agent.repository_private("Acme/config", topic_agent.command) is private


def test_missing_visibility_fails_closed(monkeypatch):
    monkeypatch.setattr(topic_agent, "command", lambda argv: "{}")
    with pytest.raises(ValueError, match="visibility"):
        topic_agent.repository_private("Acme/config", topic_agent.command)


def test_private_customer_and_public_upstream_issues_have_safe_reciprocal_links():
    rows = resources()
    for row in rows:
        row["private"] = row["repository"] == "Example/automation"
    for row in rows:
        if not row["private"]:
            row["body"] = "\n".join(r["url"] for r in rows if not r["private"])
    assert topic_agent.link_errors(rows) == []
    public = next(r for r in rows if not r["private"])
    private = rows[0]
    assert not topic_agent.can_link(public, private)
    assert topic_agent.can_link(private, public)
    public["body"] += "\n" + private["url"]
    assert any(
        "public text references private repository" in e
        for e in topic_agent.link_errors(rows)
    )


def test_public_upstream_cannot_reference_a_private_weekly_home_issue():
    row = resources()[-1] | {
        "private": False,
        "private_repositories": ["Acme/internal"],
        "body": "See https://github.com/Acme/internal/issues/1",
    }
    assert any(
        "public text references private repository" in e
        for e in topic_agent.link_errors([row])
    )


def test_discovery_checks_private_weekly_and_linked_repository_visibility(
    tmp_path, monkeypatch
):
    import json

    ctx = context(tmp_path)
    ctx["repositories"] = {"Acme/private": {}}
    ctx["triage_issue"] = {"url": "https://github.com/Acme/private/issues/1"}
    url = "https://github.com/Upstream/sdk/issues/2"
    body = topic_agent.marker(ctx["topic"]) + "\n" + ctx["triage_issue"]["url"]
    (tmp_path / "progress.md").write_text(url)
    calls = []

    def request(argv):
        endpoint = argv[-1]
        calls.append(endpoint)
        if endpoint == "repos/Upstream/sdk/issues/2":
            return json.dumps({"body": body, "state": "open", "title": "SDK repair"})
        return json.dumps(
            {"visibility": "private" if endpoint == "repos/Acme/private" else "public"}
        )

    monkeypatch.setattr(topic_agent, "command", request)
    monkeypatch.setattr(topic_agent, "github_pages", lambda *a: [])
    receipt = topic_agent.discover(ctx, tmp_path)
    assert receipt["errors"] == []
    resource = receipt["resources"][0]
    assert resource["private"] is False
    assert resource["private_repositories"] == ["Acme/private"]
    assert calls.count("repos/Acme/private") == 1
    assert any(
        "public text references private repository" in e
        for e in topic_agent.link_errors([resource])
    )


def test_recurrence_comments_hide_private_counterparts_but_keep_full_evidence(
    tmp_path, monkeypatch
):
    import json
    from types import SimpleNamespace
    import tracked_work
    from common import topic_id
    from test_topic_issue import GitHub
    from test_tracked_work import work
    from test_weekly import record

    rows = [record()]
    existing = [work(1, repository="Acme/private"), work(2, repository="Upstream/sdk")]
    candidate = {
        "id": topic_id("shared", "known-problem"),
        "representative_event_ids": [rows[0]["event"]["id"]],
    }
    captured = []
    github = GitHub()

    def request(argv):
        if argv[-1] == "repos/Upstream/sdk":
            return json.dumps({"visibility": "public"})
        return github(argv)

    issue = SimpleNamespace(
        preview=False,
        value={"html_url": "https://github.com/Acme/private/issues/77"},
        artifact=lambda name, value: captured.append(value)
        or {"reference": "a" * 64 + ".json"},
    )
    monkeypatch.setattr(tracked_work, "command", request)
    tracked_work.comment(candidate, existing, rows, issue, tmp_path)
    public = next(c for c in github.comments if "Upstream/sdk" in c["issue_url"])
    private = next(c for c in github.comments if "Acme/private" in c["issue_url"])
    assert "Acme/private" not in public["body"]
    assert "Upstream/sdk" in private["body"]
    assert issue.value["html_url"] in private["body"]
    assert captured[0]["related_work"] == sorted(r["url"] for r in existing)
    assert captured[0]["triage_issue"] == issue.value["html_url"]


def test_private_repository_name_does_not_match_a_public_prefix_neighbor():
    row = resources()[0] | {
        "private": False,
        "private_repositories": ["Acme/lib"],
        "body": "Changes to Acme/library",
    }
    assert topic_agent.link_errors([row]) == []
    row["body"] = "https://github.com/Acme/lib.git"
    assert any(
        "public text references private repository" in e
        for e in topic_agent.link_errors([row])
    )


def test_upstream_discovery_history_never_expands_customer_scope():
    import weekly

    state = {
        "topic_weeks": {"last/topic": {"resources": [{"repository": "Upstream/sdk"}]}}
    }
    config = {"repository": "Acme/app"}
    assert weekly.discovery_repositories(config, {"Acme/app": {}}, state) == [
        "Acme/app"
    ]
    assert weekly.discovery_repositories(
        config | {"repair_scope": "any_authorized"}, {"Acme/app": {}}, state
    ) == ["Acme/app", "Upstream/sdk"]
    assert (
        weekly.discovery_repositories(
            config | {"isolated_validation": True}, {"Acme/app": {}}, state
        )
        == []
    )


@pytest.mark.parametrize(
    "isolated,scope", [(True, "any_authorized"), (False, "customer_stack")]
)
def test_unregistered_existing_work_cannot_bypass_qc_or_customer_scope(
    monkeypatch, isolated, scope
):
    import triage

    references = {
        "existing_work": [],
        "repair_policy": resolve({"repository": "Acme/app", "repair_scope": scope}),
        "isolated_validation": isolated,
    }
    monkeypatch.setattr(
        triage, "command", lambda *a: pytest.fail("unapproved GitHub access")
    )
    with pytest.raises(ValueError, match="unknown"):
        triage.discover_work(["https://github.com/Upstream/sdk/issues/1"], references)


def test_triage_verifies_new_upstream_work_and_records_recurrence_before_repair(
    tmp_path, monkeypatch
):
    import topic_issue
    import tracked_work
    import triage
    import triage_agent
    from common import write_json
    from test_automation import arguments
    from test_topic_issue import GitHub
    from test_triage import topic
    from test_weekly import record, inventory

    rows = [record()]
    frame = tmp_path / "input.json"
    write_json(frame, inventory(rows))
    output = tmp_path / "run/triage"
    output.mkdir(parents=True)
    github = GitHub(private=False)
    url = "https://github.com/Upstream/sdk/issues/1"
    github.issues = [
        {
            "number": 1,
            "html_url": url,
            "body": "Human report of the same problem",
            "title": "Known failure",
            "state": "open",
        }
    ]
    for module in (triage, tracked_work, topic_issue):
        monkeypatch.setattr(module, "command", github)
    args = arguments(
        output,
        repository="Acme/app",
        checkout=str(tmp_path),
        state_dir=str(tmp_path / "state"),
        triage_week="2026-W45",
        run_id="discovered-work",
        dry_run=False,
        inventory_path=str(frame),
        evidence_location="Authorized worker",
        repair_policy=resolve(
            {"repository": "Acme/app", "repair_scope": "any_authorized"}
        ),
    )
    plan = {
        "topics": [topic("e1", "known-failure") | {"existing_work_urls": [url]}],
        "no_action_reason": "",
    }

    def rank(args, context, records, schema):
        return plan, {}, triage_agent.prepare_issue(args, context, records)

    monkeypatch.setattr(triage, "rank", rank)
    existing = []
    queue, summary = triage.triage(args, rows, existing)
    assert queue == []
    assert summary["tracked_topics"][0]["status"] == "recurrence_recorded"
    assert existing[0]["url"] == url
    assert github.issues[0]["body"] == "Human report of the same problem"
    assert any(
        c["issue_url"] == "https://api.github.com/repos/Upstream/sdk/issues/1"
        for c in github.comments
    )


def test_existing_work_urls_use_github_canonical_case_without_duplicate_lookup(
    monkeypatch,
):
    import triage
    from test_triage import topic
    from test_weekly import record

    rows = [record()]
    original = "https://github.com/Upstream/sdk/issues/1"
    existing = [{"url": original, "body": "Known issue"}]
    _, references, _ = triage.prepare(rows, existing, [])
    monkeypatch.setattr(
        triage, "command", lambda *a: pytest.fail("known URL fetched again")
    )
    plan = {
        "topics": [topic("e1") | {"existing_work_urls": [original.lower()]}],
        "no_action_reason": "",
    }
    assert triage.validate(plan, rows, references, 3)[0]["existing_work_urls"] == [
        original
    ]


def test_upstream_recurrence_history_is_discovered_without_a_previous_repair():
    import weekly

    state = {
        "events": {
            "event": {
                "existing_work_urls": ["https://github.com/Upstream/sdk/issues/1"]
            }
        }
    }
    config = {"repository": "Acme/app", "repair_scope": "any_authorized"}
    assert weekly.discovery_repositories(config, {"Acme/app": {}}, state) == [
        "Acme/app",
        "Upstream/sdk",
    ]


def test_standard_github_issue_references_are_valid_links():
    rows = resources()
    for row in rows:
        row["body"] = (
            "Related: Example/automation#1, Example/sdk#1, Example/automation#2, Example/sdk#2"
        )
    assert topic_agent.link_errors(rows) == []
    own_issue = rows[0]
    local_pr = next(
        r
        for r in rows
        if r["repository"] == own_issue["repository"] and r["kind"] == "pull"
    )
    local_pr["body"] = "See issue #1 for the problem and evidence."
    assert topic_agent.links_to(local_pr, own_issue)
    local_pr["body"] = "Unrelated #10 or Other/repo#1"
    assert not topic_agent.links_to(local_pr, own_issue)
    local_pr["body"] = "Evidence: https://github.com/Example/automation/issues/10"
    assert not topic_agent.links_to(local_pr, own_issue)
