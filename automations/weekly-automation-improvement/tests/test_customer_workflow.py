import copy

import pytest

import customer_workflow as customer
from backlog import (
    ACTIVE,
    BLOCKED,
    CUSTOMER,
    EXTERNAL,
    FACTORY,
    OUTCOME_LABELS,
    READY,
    TOPIC,
)
from common import read_json, replace_json
from public_status import STATUS_END, STATUS_START, markdown_text, read_marker
from test_factory_handoff import GitHub, ISSUE, REPO, URL


class CustomerGitHub(GitHub):
    def __init__(self):
        super().__init__()
        self.actor = "customer-runner"
        self.issue["labels"] = [
            {"name": name} for name in [EXTERNAL, CUSTOMER, READY, "priority:P1"]
        ]
        self.timeline.append(
            {
                "id": 2,
                "event": "labeled",
                "actor": {"login": "maintainer"},
                "label": {"name": CUSTOMER},
            }
        )
        self.pulls = {}
        self.visibility = {}

    def api(self, endpoint, method="GET", fields=()):
        if endpoint in self.pulls:
            assert method == "GET"
            return copy.deepcopy(self.pulls[endpoint])
        if endpoint in self.visibility:
            return {"visibility": self.visibility[endpoint]}
        return super().api(endpoint, method, fields)

    def pull(self, repo, num, private=False):
        url = f"https://github.com/{repo}/pull/{num}"
        value = {
            "html_url": url,
            "state": "open",
            "draft": True,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
        }
        self.pulls[f"repos/{repo}/pulls/{num}"] = value
        self.visibility[f"repos/{repo}"] = "private" if private else "public"
        return value


@pytest.fixture
def route(tmp_path):
    gh = CustomerGitHub()
    config = {
        "state_dir": str(tmp_path),
        "customer_workflow": {
            "executor_login": gh.actor,
            "trusted_enrollers": ["maintainer"],
            "repositories": [REPO, "Example/private", "Other/public"],
        },
    }

    def run(operation, attempt="customer-1", report=None):
        return customer.run(config, operation, URL, attempt, report, github=gh)

    return gh, config, run


def result(outcome, *pulls):
    return {
        "outcome": outcome,
        "validation": "Compared matching failing and passing inputs.",
        "limitations": "Production credentials were unavailable.",
        "next_action": "A maintainer can review the reported evidence.",
        "pull_requests": [
            {
                "url": pr["html_url"],
                "head": pr["head"]["sha"],
                "base": pr["base"]["sha"],
            }
            for pr in pulls
        ],
    }


def test_claim_then_reconcile_customer_revisions_privately_and_publicly(
    route, tmp_path
):
    gh, _, run = route
    gh.issue["assignees"] = [{"login": gh.actor}]
    prior_body = gh.issue["body"]
    claim = run("claim")
    assert claim["start"] and claim["next_actor"] == "executor"
    assert claim["next_actor_login"] == gh.actor
    status_url = f"{URL}#issuecomment-{gh.comments[0]['id']}"
    assert claim["status_url"] == status_url
    assert not run("claim")["start"]
    assert customer.label_names(gh.issue) == {EXTERNAL, CUSTOMER, ACTIVE, "priority:P1"}
    public = gh.pull("Other/public", 1)
    private = gh.pull("Example/private", 2, True)
    private["head"]["sha"] = "9" * 40
    revised = result("revised", public, private)
    assert run("report", report=revised)["status"] == "revised"
    accepted = result("accepted", public, private) | {
        "next_actor": "reviewer",
        "next_actor_login": "project-reviewer",
        "next_action": "Review the failing and passing inputs before merging.",
    }
    published = run("report", report=accepted)
    assert published["status"] == "accepted"
    reference = {
        "url": public["html_url"],
        "checks_url": f"{public['html_url']}/checks?sha={public['head']['sha']}",
        "state": "draft",
        "head": public["head"]["sha"],
        "base": public["base"]["sha"],
        "reported_head": public["head"]["sha"],
        "reported_base": public["base"]["sha"],
    }
    assert published["pull_requests"] == [reference]
    assert run("report", report=revised)["status"] == "accepted"
    body = gh.comments[0]["body"]
    assert f"[PR Other/public#1]({public['html_url']})" in body
    assert f"[Checks for Other/public#1]({reference['checks_url']})" in body
    assert public["head"]["sha"] in body and "(draft)" in body
    assert "Example/private" not in body and private["head"]["sha"] not in body
    assert (
        "recorded privately" in body and markdown_text(accepted["limitations"]) in body
    )
    assert (
        "**Customer-reported validation:**" in body
        and "**GitHub review and CI:**" in body
    )
    assert "**Next actor:** Reviewer (`project-reviewer`)" in body
    invocation = {
        "start": False,
        "attempt": "customer-1",
        "issue": URL,
        "status_url": status_url,
    }
    assert read_marker(body) | invocation == published
    public["head"]["sha"] = "c" * 40
    public["base"]["sha"] = "d" * 40
    changed = run("reconcile")
    assert changed["status"] == "incomplete" and changed["next_actor"] == "executor"
    assert changed["next_actor_login"] == gh.actor
    assert changed["pull_requests"] == [
        reference
        | {
            "head": "c" * 40,
            "base": "d" * 40,
            "checks_url": f"{public['html_url']}/checks?sha={'c' * 40}",
        }
    ]
    body = gh.comments[0]["body"]
    assert "a" * 40 in body and "c" * 40 in body and "Revalidate" in body
    assert "project-reviewer" not in body and accepted["next_action"] not in body
    assert read_marker(body) | invocation == changed
    assert len(gh.comments) == 1 and gh.issue["body"] == prior_body
    assert customer.label_names(gh.issue) == {
        EXTERNAL,
        CUSTOMER,
        OUTCOME_LABELS["incomplete"],
        "priority:P1",
    }
    receipt = read_json(tmp_path / "customer-attempts" / REPO.casefold() / "42.json")
    assert receipt["outcome"] == "incomplete" and receipt["report"] == accepted
    assert [pr["head"] for pr in receipt["observed_pulls"]] == ["c" * 40, "9" * 40]


@pytest.mark.parametrize(
    "constraint",
    [
        "factory",
        "factory_receipt",
        "native_journal",
        "native_ledger",
        "assigned",
        "hold",
        "custom_hold",
        "untrusted",
        "open_pr",
    ],
)
def test_customer_does_not_compete_or_override_maintainer(route, tmp_path, constraint):
    gh, config, run = route
    if constraint == "factory":
        gh.issue["labels"].append({"name": FACTORY})
    if constraint == "factory_receipt":
        replace_json(
            tmp_path / "factory-handoff.json", {ISSUE: {"status": "implementing"}}
        )
    if constraint == "native_journal":
        replace_json(
            tmp_path / "native-claims" / "topic.json", {"topic": {"issue_url": URL}}
        )
    if constraint == "native_ledger":
        replace_json(
            tmp_path / "state.json",
            {
                "topic_weeks": {
                    "run/topic": {"topic": {"issue_url": URL}, "status": "dispatched"}
                }
            },
        )
    if constraint == "assigned":
        gh.issue["assignees"] = [{"login": gh.actor}, {"login": "human"}]
    if constraint == "hold":
        gh.issue["labels"].append({"name": BLOCKED})
    if constraint == "custom_hold":
        config["issue_hold_labels"] = ["waiting for maintainer"]
        gh.issue["labels"].append({"name": "Waiting-for-maintainer"})
    if constraint == "untrusted":
        gh.timeline[-1]["actor"] = {"login": "reporter"}
    if constraint == "open_pr":
        gh.timeline.append(
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "state": "open",
                        "html_url": "https://github.com/Other/public/pull/9",
                        "body": f"Fixes {REPO}#42",
                    }
                },
            }
        )
    with pytest.raises(ValueError):
        run("claim")
    assert gh.writes == [] and not (tmp_path / "customer-attempts").exists()


def test_interrupted_customer_claim_and_stale_requeue(route, tmp_path):
    gh, _, run = route
    gh.fail_once = ISSUE + "/labels"
    with pytest.raises(OSError):
        run("claim")
    assert run("claim")["start"]
    assert not run("claim")["start"]
    gh.issue["labels"].append({"name": READY})
    with pytest.raises(ValueError):
        run("claim", "new-job")
    open_pr = gh.pull(REPO, 2)
    run("report", report=result("blocked", open_pr))
    run("report", report=result("unrepaired"))
    with pytest.raises(ValueError, match="prior open"):
        run("claim", "new-job")
    open_pr["state"] = "closed"
    assert run("claim", "new-job")["start"]
    assert (
        tmp_path / "customer-attempts" / REPO.casefold() / "42-customer-1.json"
    ).exists()
    with pytest.raises(ValueError):
        run("report", report=result("unrepaired"))
    gh.issue["labels"].append({"name": BLOCKED})
    with pytest.raises(ValueError, match="hold"):
        run("report", "new-job", result("accepted"))


def test_changed_enrollment_rejects_old_attempt_and_protects_brief(route):
    gh, _, run = route
    run("claim")
    writes = list(gh.writes)
    gh.timeline.append(gh.timeline[-1] | {"id": 3})
    with pytest.raises(ValueError, match="enrollment changed"):
        run("report", report=result("accepted"))
    assert gh.writes == writes and gh.issue["body"].startswith("Human issue")


@pytest.mark.parametrize("during", ["verify_pr", "publish_comment", "add_outcome"])
def test_transfer_stops_remaining_customer_publication(route, monkeypatch, during):
    gh, _, run = route
    run("claim")
    pr = gh.pull(REPO, 2)
    trigger = {
        "verify_pr": (f"repos/{REPO}/pulls/2", "GET"),
        "publish_comment": (f"repos/{REPO}/issues/comments/1", "PATCH"),
        "add_outcome": (ISSUE + "/labels", "POST"),
    }[during]
    request = gh.api
    writes_at_change = None

    def transfer(endpoint, method="GET", fields=()):
        nonlocal writes_at_change
        reply = request(endpoint, method, fields)
        if (endpoint, method) == trigger:
            if during == "publish_comment":
                gh.timeline.append(gh.timeline[-1] | {"id": 3})
            else:
                gh.issue["labels"].remove({"name": CUSTOMER})
                gh.issue["labels"].append({"name": FACTORY})
            writes_at_change = len(gh.writes)
        return reply

    monkeypatch.setattr(gh, "api", transfer)
    with pytest.raises(ValueError, match="ownership|enrollment changed"):
        run("report", report=result("accepted", pr))
    assert writes_at_change is not None and not gh.writes[writes_at_change:]
    assert ACTIVE in customer.label_names(gh.issue)
    if during == "verify_pr":
        assert read_marker(gh.comments[0]["body"])["status"] == "implementing"
    if during != "add_outcome":
        assert OUTCOME_LABELS["accepted"] not in customer.label_names(gh.issue)


@pytest.mark.parametrize("status", ["implementing", "accepted"])
def test_customer_hold_pauses_public_status_and_restores_attempt(
    route, tmp_path, status
):
    gh, _, run = route
    original = run("claim")
    if status == "accepted":
        original = run("report", report=result("accepted") | {"next_actor": "reviewer"})
    receipt = tmp_path / "customer-attempts" / REPO.casefold() / "42.json"
    saved = receipt.read_bytes()
    gh.issue["labels"].append({"name": BLOCKED})
    labels = customer.label_names(gh.issue)
    before = len(gh.writes)
    held = run("reconcile")
    assert held["status"] == "maintainer_hold" and held["next_actor"] == "maintainer"
    assert not held["start"] and held["status_url"] == original["status_url"]
    assert read_marker(gh.comments[0]["body"])["next_action"] == held["next_action"]
    assert original["next_action"] not in gh.comments[0]["body"]
    assert run("reconcile") == held
    with pytest.raises(ValueError, match="hold"):
        run("claim", "another-attempt")
    assert [method for _, method, _ in gh.writes[before:]] == ["PATCH"]
    assert receipt.read_bytes() == saved and customer.label_names(gh.issue) == labels
    gh.issue["labels"].remove({"name": BLOCKED})
    restored = run("reconcile")
    assert restored == original | {"start": False}
    assert read_marker(gh.comments[0]["body"])["status"] == status


@pytest.mark.parametrize(
    "change",
    ["unclaimed", "reenrolled", "untrusted", "assignee", "closed", "other_attempt"],
)
def test_customer_hold_requires_the_current_open_claim(route, change):
    gh, _, run = route
    if change != "unclaimed":
        run("claim")
    if change == "reenrolled":
        gh.timeline.append(gh.timeline[-1] | {"id": 3})
    if change == "untrusted":
        gh.timeline[-1]["actor"] = {"login": "untrusted"}
    if change == "assignee":
        gh.issue["assignees"] = [{"login": "another-executor"}]
    if change == "closed":
        gh.issue["state"] = "closed"
    gh.issue["labels"].append({"name": BLOCKED})
    writes = gh.writes.copy()
    with pytest.raises(ValueError):
        run("reconcile", "another-id" if change == "other_attempt" else "customer-1")
    assert gh.writes == writes


def test_customer_hold_publication_rechecks_assignee(route, monkeypatch):
    gh, _, run = route
    run("claim")
    gh.issue["labels"].append({"name": BLOCKED})
    pages = gh.pages
    writes = gh.writes.copy()

    def reassign(endpoint):
        rows = pages(endpoint)
        if endpoint.startswith(ISSUE + "/timeline"):
            gh.issue["assignees"] = [{"login": "new-owner"}]
        return rows

    monkeypatch.setattr(gh, "pages", reassign)
    with pytest.raises(ValueError, match="another assignee"):
        run("reconcile")
    assert gh.writes == writes


def test_hold_during_claim_publication_prevents_label_changes_and_start(
    route, monkeypatch
):
    gh, _, run = route
    write_status = gh.write_status

    def hold_after_status(*args, **kwargs):
        url = write_status(*args, **kwargs)
        gh.issue["labels"].append({"name": BLOCKED})
        return url

    monkeypatch.setattr(gh, "write_status", hold_after_status)
    with pytest.raises(ValueError, match="hold"):
        run("claim")
    assert all("/labels" not in endpoint for endpoint, *_ in gh.writes)
    monkeypatch.setattr(gh, "write_status", write_status)
    assert run("reconcile")["status"] == "maintainer_hold"
    gh.issue["labels"].remove({"name": BLOCKED})
    assert run("claim")["start"]
    assert not run("claim")["start"]


def test_legacy_customer_receipt_reconciles_without_another_start(route, tmp_path):
    gh, _, run = route
    previous = {
        "owner": gh.actor,
        "attempt": "older-run",
        "report": result("unrepaired"),
        "start_delivered": True,
    }
    receipt = tmp_path / "customer-attempts" / REPO.casefold() / "42.json"
    replace_json(receipt, previous)
    status = run("reconcile", "older-run")
    assert status["status"] == "unrepaired" and status["next_actor"] == "maintainer"
    assert not status.get("next_actor_login")
    assert status["next_action"] == previous["report"]["next_action"]
    assert not run("claim", "older-run")["start"]
    assert read_json(receipt)["enrollment"] == gh.timeline[-1]["id"]


def test_managed_customer_brief_recovers_interrupted_status_update(route):
    gh, _, run = route
    gh.issue["labels"].append({"name": TOPIC})
    prefix = f"<!-- automation-improvement-backlog: {'a' * 20}/{'b' * 20} -->\n\nOriginal brief.\n"
    suffix = "\nMaintainer notes."
    gh.issue["body"] = prefix + STATUS_START + "\nReady.\n" + STATUS_END + suffix
    run("claim")
    gh.fail_once = ISSUE
    with pytest.raises(OSError):
        run("report", report=result("blocked"))
    status = run("reconcile")
    body = gh.issue["body"]
    assert body.startswith(prefix + STATUS_START) and body.endswith(STATUS_END + suffix)
    assert len(gh.comments) == 1
    assert f"{URL}#issuecomment-{gh.comments[0]['id']}" in body
    assert read_marker(body) == read_marker(gh.comments[0]["body"])
    assert read_marker(body)["status"] == status["status"] == "blocked"


@pytest.mark.parametrize("drift", [False, True])
def test_private_base_drift_with_explicit_next_actor(route, drift):
    gh, _, run = route
    run("claim")
    pr = gh.pull("Example/private", 1, True)
    report = result("accepted", pr) | {"next_actor": "ci"}
    if drift:
        pr["base"]["sha"] = "f" * 40
    status = run("report", report=report)
    assert status["next_actor"] == ("executor" if drift else "ci")
    assert status["status"] == ("incomplete" if drift else "accepted")
    assert not status.get("pull_requests")
    body = gh.comments[0]["body"]
    assert "Example/private" not in body and pr["base"]["sha"] not in body
    assert "**Next actor:** " + ("Executor" if drift else "CI") in body


@pytest.mark.parametrize(
    "github_fields,state",
    [
        ({"draft": False}, "ready"),
        ({"state": "closed"}, "closed"),
        ({"state": "closed", "merged_at": "2026-11-23T12:00:00Z"}, "merged"),
    ],
)
def test_structured_report_uses_github_pr_state_and_reported_revisions(
    route, github_fields, state
):
    gh, _, run = route
    run("claim")
    pr = gh.pull(REPO, 2)
    pr.update(github_fields)
    report = result("accepted", pr)
    item = report["pull_requests"][0]
    del item["base"]
    item.update(
        state="draft",
        reported_head="f" * 40,
        checks_url="https://private.example/checks",
    )
    status = run("report", report=report)
    reference = read_marker(gh.comments[0]["body"])["pull_requests"][0]
    assert status["pull_requests"] == [reference]
    assert reference["state"] == state
    assert reference["reported_head"] == pr["head"]["sha"]
    assert reference["base"] == pr["base"]["sha"] and "reported_base" not in reference
    assert reference["checks_url"] == f"{pr['html_url']}/checks?sha={pr['head']['sha']}"


@pytest.mark.parametrize(
    "path", ["actions/runs/12345", f"blob/{'a' * 40}/evidence/pass-fail.md"]
)
def test_customer_validation_link_is_explicit_and_text_stays_literal(route, path):
    gh, _, run = route
    run("claim")
    url = f"https://github.com/{REPO}/{path}"
    report = result("accepted") | {
        "validation_url": url,
        "next_action": "Review [controls](example.com).",
    }
    status = run("report", report=report)
    body = gh.comments[0]["body"]
    assert f"[Customer validation evidence]({url})" in body
    assert read_marker(body)["validation_url"] == status["validation_url"] == url
    assert status["next_action"] == report["next_action"]
    assert "**Next action:** " + markdown_text(report["next_action"]) in body
    assert "\n\n**Next action:** " + report["next_action"] not in body


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Example/private/actions/runs/1",
        f"https://github.com/{REPO}/actions/runs/1?token=hidden",
        f"https://github.com/{REPO}/actions/runs/1#section",
        f"https://github.com/{REPO}/blob/main/evidence.md",
        f"https://github.com/{REPO}/blob/{'a' * 40}/dir/../private",
        f"https://github.com/{REPO}/blob/{'a' * 40}/%2e%2e/evidence.md",
        f"https://github.com/{REPO}/blob/{'a' * 40}/evidence)[user](page",
        f"https://github.com@mirror.example/{REPO}/actions/runs/1",
    ],
)
def test_invalid_validation_link_leaves_previous_status_intact(route, url):
    gh, _, run = route
    run("claim")
    writes = gh.writes.copy()
    with pytest.raises(ValueError, match="validation_url"):
        run("report", report=result("accepted") | {"validation_url": url})
    assert gh.writes == writes


@pytest.mark.parametrize(
    "field,value",
    [
        ("next_actor", "ask-user"),
        ("next_actor_login", "@outside-team"),
    ],
)
def test_invalid_actor_leaves_previous_status_intact(route, field, value):
    gh, _, run = route
    run("claim")
    writes = gh.writes.copy()
    with pytest.raises(ValueError):
        run("report", report=result("accepted") | {field: value})
    assert gh.writes == writes


def test_orphaned_public_customer_claim_cannot_start_again(route):
    gh, _, run = route
    gh.comments = [
        {"user": {"login": gh.actor}, "body": customer.MARKER + "\nPrevious attempt."}
    ]
    with pytest.raises(ValueError, match="existing customer receipt"):
        run("claim")
    assert not gh.writes


@pytest.mark.parametrize(
    "ownership",
    ["customer_label", "factory_label", "customer_receipt", "factory_receipt"],
)
def test_native_excludes_explicit_external_routes_and_receipts(
    tmp_path, monkeypatch, ownership
):
    import backlog
    import backlog_dispatch

    cfg = {"repository": REPO, "project_id": "p", "signal_id": "s"}
    context = {
        "state_dir": str(tmp_path),
        "output": str(tmp_path),
        "repositories": {REPO: {}},
        "repair_policy": {"scope": "any_authorized"},
    }
    scope = backlog.namespace(cfg, tmp_path)
    identifier = "e" * 20
    labels = ["automation-improvement", backlog.TOPIC, READY, "priority:P2"]
    if ownership == "customer_label":
        labels.append(CUSTOMER)
    if ownership == "factory_label":
        labels.append(FACTORY)
    if ownership == "customer_receipt":
        replace_json(
            tmp_path / "customer-attempts" / REPO.casefold() / "42.json",
            {"attempt": "recorded"},
        )
    if ownership == "factory_receipt":
        replace_json(
            tmp_path / "factory-handoff.json", {ISSUE: {"status": "implementing"}}
        )
    row = {
        "number": 42,
        "html_url": URL,
        "state": "open",
        "title": "Existing owned work",
        "body": f"<!-- automation-improvement-backlog: {scope}/{identifier} -->",
        "labels": [{"name": label} for label in labels],
    }
    monkeypatch.setattr(backlog_dispatch, "github_pages", lambda *args: [row])
    queue, excluded = backlog_dispatch.selection(cfg, context, {})
    assert not queue and excluded[0]["status"] == "external_owner"
