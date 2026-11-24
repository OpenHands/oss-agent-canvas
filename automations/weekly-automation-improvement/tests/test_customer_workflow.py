import copy

import pytest

import customer_workflow as customer
from backlog import ACTIVE, BLOCKED, CUSTOMER, EXTERNAL, FACTORY, OUTCOME_LABELS, READY
from common import read_json, replace_json
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
    assert run("claim")["start"]
    assert not run("claim")["start"]
    assert customer.label_names(gh.issue) == {EXTERNAL, CUSTOMER, ACTIVE, "priority:P1"}
    public = gh.pull("Other/public", 1)
    private = gh.pull("Example/private", 2, True)
    private["head"]["sha"] = "9" * 40
    revised = result("revised", public, private)
    assert run("report", report=revised)["status"] == "revised"
    accepted = result("accepted", public, private)
    assert run("report", report=accepted)["status"] == "accepted"
    assert run("report", report=revised)["status"] == "accepted"
    body = gh.comments[0]["body"]
    assert public["html_url"] in body and public["head"]["sha"] in body
    assert "Example/private" not in body and private["head"]["sha"] not in body
    assert "recorded privately" in body and "limitations" in body.casefold()
    public["head"]["sha"] = "c" * 40
    assert run("reconcile")["status"] == "incomplete"
    body = gh.comments[0]["body"]
    assert "a" * 40 in body and "c" * 40 in body and "Revalidate" in body
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
    assert run("reconcile", "older-run")["status"] == "unrepaired"
    assert not run("claim", "older-run")["start"]
    assert read_json(receipt)["enrollment"] == gh.timeline[-1]["id"]


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
