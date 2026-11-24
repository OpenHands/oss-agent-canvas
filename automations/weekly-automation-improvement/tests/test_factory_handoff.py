import copy
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import factory_handoff as handoff
from backlog import ACTIVE, BLOCKED, EXTERNAL, FACTORY, OUTCOME_LABELS, READY
from factory_linear import Linear

REPO = "Example/project"
NUMBER = 42
ISSUE = f"repos/{REPO}/issues/{NUMBER}"
PULL = f"repos/{REPO}/pulls/13"
URL = f"https://github.com/{REPO}/issues/{NUMBER}"
SUMMARY = Path(handoff.__file__)


class GitHub:
    def __init__(self):
        self.actor = "factory-dispatcher"
        self.issue = {
            "number": NUMBER,
            "html_url": URL,
            "state": "open",
            "assignees": [],
            "body": "Human issue body. Never rewrite this.",
            "labels": [{"name": EXTERNAL}, {"name": FACTORY}, {"name": READY}],
        }
        self.timeline = [
            {
                "event": "labeled",
                "label": {"name": FACTORY},
                "actor": {"login": "maintainer"},
            }
        ]
        self.comments = []
        self.writes = []
        self.pr = None
        self.reviews = []
        self.check_state = "pending"
        self.files = [{"filename": "e2e_tests/smoke.py"}]
        self.fail_once = None

    def api(self, endpoint, method="GET", fields=()):
        if method != "GET":
            self.writes.append((endpoint, method, tuple(fields)))
        if endpoint == "user":
            return {"login": self.actor}
        if endpoint == f"repos/{REPO}":
            return {"visibility": "public", "default_branch": "main"}
        if endpoint == ISSUE and method == "GET":
            return copy.deepcopy(self.issue)
        if endpoint == ISSUE + "/assignees" and method == "POST":
            assignee = {"login": fields[0].split("=")[1]}
            self.issue["assignees"].append(assignee)
            self.timeline.append(
                {
                    "event": "assigned",
                    "actor": {"login": self.actor},
                    "assignee": assignee,
                }
            )
        elif endpoint == ISSUE + "/labels" and method == "POST":
            self.issue["labels"].extend(
                {"name": field.split("=")[1]} for field in fields
            )
        elif endpoint.startswith(ISSUE + "/labels/") and method == "DELETE":
            self.issue["labels"].remove({"name": endpoint.split("/")[-1]})
        elif endpoint == ISSUE + "/comments" and method == "POST":
            self.comments.append(
                {"id": 1, "user": {"login": self.actor}, "body": fields[0][5:]}
            )
        elif endpoint == f"repos/{REPO}/issues/comments/1" and method == "PATCH":
            self.comments[0]["body"] = fields[0][5:]
        elif endpoint == PULL + "/requested_reviewers" and method == "POST":
            self.pr["requested_reviewers"] = [{"login": fields[0].split("=")[1]}]
        elif endpoint == PULL and method == "GET":
            return copy.deepcopy(self.pr)
        else:
            raise AssertionError((endpoint, method, fields))
        if endpoint == self.fail_once:
            self.fail_once = None
            raise OSError("write succeeded, response was lost")

    def pages(self, endpoint):
        if endpoint == ISSUE + "/timeline?per_page=100":
            return self.timeline
        if endpoint == ISSUE + "/comments?per_page=100":
            return self.comments
        if endpoint.startswith(f"repos/{REPO}/issues?state=open&labels="):
            return [self.issue]
        if endpoint.startswith(f"repos/{REPO}/pulls?state=all&head="):
            return [self.pr] if self.pr and self.pr["head"]["ref"] in endpoint else []
        if endpoint == PULL + "/reviews?per_page=100":
            return self.reviews
        if endpoint == PULL + "/files?per_page=100":
            return self.files
        raise AssertionError(endpoint)

    def checks(self, repo, sha, required):
        assert repo == REPO and sha == self.pr["head"]["sha"] and required == ["test"]
        return self.check_state

    def make_pr(self, *, branch="openhands/issue-42", body="Closes #42", sha="a" * 40):
        self.pr = {
            "number": 13,
            "state": "open",
            "user": {"login": "resolver-writer"},
            "head": {"sha": sha, "ref": branch, "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "draft": True,
            "body": body,
            "requested_reviewers": [],
        }

    def review(self, state, *, sha=None, user="all-hands-bot"):
        self.pr["requested_reviewers"] = []
        self.reviews.append(
            {
                "commit_id": sha or self.pr["head"]["sha"],
                "state": state,
                "user": {"login": user},
            }
        )

    write_comment = handoff.GitHub.write_comment
    sync_labels = handoff.GitHub.sync_labels


@pytest.fixture
def setup(tmp_path):
    config = {
        "state_dir": str(tmp_path),
        "factory_handoff": {
            "enabled": True,
            "dispatcher_login": "factory-dispatcher",
            "trusted_enrollers": ["maintainer"],
            "timeout_seconds": 15,
            "repositories": {
                REPO: {
                    "assignee": "all-hands-bot",
                    "resolver_author": "resolver-writer",
                    "reviewer": "all-hands-bot",
                    "required_checks": ["test"],
                }
            },
        },
    }
    github = GitHub()

    def tick(now=1, **kwargs):
        result = handoff.run(config, github=github, now=now, **kwargs)
        return result["issues"].get(ISSUE.casefold(), result["status"])

    return github, config, tick


def test_dispatch_requires_trusted_enrollment_and_no_existing_claim(setup, tmp_path):
    github, config, tick = setup
    github.timeline[0]["actor"]["login"] = "external-reporter"
    assert tick() == "untrusted_enrollment"
    assert github.writes == []
    github.timeline[0]["actor"]["login"] = "maintainer"
    github.issue["labels"].append({"name": ACTIVE})
    assert tick() == "ineligible"
    github.issue["labels"].remove({"name": ACTIVE})
    assert tick(dry_run=True, issue_url=URL) == "eligible"
    assert github.writes == []
    assert not (tmp_path / "factory-handoff.json").exists()
    github.actor = "another-account"
    with pytest.raises(ValueError, match="configured dispatcher"):
        tick()


def test_resolve_review_and_revision_survive_retries(setup):
    github, _, tick = setup
    github.fail_once = ISSUE + "/assignees"
    assert tick() == "readback_or_write_failed"
    assert tick() == "implementing"
    assert sum(endpoint.endswith("/assignees") for endpoint, *_ in github.writes) == 1
    assert handoff.label_names(github.issue) == {EXTERNAL, FACTORY, ACTIVE}
    github.make_pr()
    assert tick() == "checks"
    github.check_state = "green"
    github.fail_once = PULL + "/requested_reviewers"
    assert tick() == "readback_or_write_failed"
    assert tick() == "reviewing"
    github.review("CHANGES_REQUESTED")
    assert tick() == "revision-needed"
    assert OUTCOME_LABELS["revision-needed"] in handoff.label_names(github.issue)
    github.pr["head"]["sha"] = "b" * 40
    github.review("APPROVED", sha="a" * 40)
    assert tick() == "reviewing"
    github.review("APPROVED", user="resolver-writer")
    assert tick() == "reviewing"
    assert (
        sum(endpoint.endswith("/requested_reviewers") for endpoint, *_ in github.writes)
        == 2
    )
    github.review("APPROVED")
    assert tick() == "accepted"
    assert handoff.label_names(github.issue) == {
        EXTERNAL,
        FACTORY,
        OUTCOME_LABELS["accepted"],
    }
    assert len(github.comments) == 1
    assert "**Next actor:** Maintainer" in github.comments[0]["body"]
    assert github.issue["body"] == "Human issue body. Never rewrite this."


def test_scope_and_resolver_comments_fail_closed(setup):
    github, config, tick = setup
    config["factory_handoff"]["repositories"][REPO]["path_scopes"] = ["e2e_tests/*"]
    assert tick() == "implementing"
    github.comments.append(
        {
            "id": 2,
            "user": {"login": "external-reporter"},
            "body": handoff.RESULT.format("blocked") + "\nprivate string",
        }
    )
    assert tick() == "implementing"
    github.make_pr()
    github.files[0]["previous_filename"] = "server/outside.py"
    assert tick() == "blocked"
    assert BLOCKED in handoff.label_names(github.issue)
    assert "scope" in github.comments[0]["body"]
    assert "server/outside.py" not in github.comments[0]["body"]
    assert all(
        not endpoint.endswith("/requested_reviewers") for endpoint, *_ in github.writes
    )


class LinearFixture(Linear):
    def __init__(self):
        self.created = None
        self.writes = []

    def gql(self, query, **variables):
        if "query FactoryTicket" in query:
            return {"issues": {"nodes": [self.created] if self.created else []}}
        if "query FactoryRoute" in query:
            return {
                "workflowStates": {
                    "nodes": [{"id": "state", "team": {"id": "team", "key": "OHE"}}]
                },
                "issueLabels": {"nodes": [{"id": "label", "team": None}]},
            }
        assert "mutation FactoryCreate" in query
        payload = variables["input"]
        self.writes.append(payload)
        assert uuid.UUID(payload["id"]).version == 4
        self.created = {
            "id": payload["id"],
            "identifier": "OHE-123",
            "description": payload["description"],
            "url": "https://linear.app/private-space/issue/OHE-123/investigate",
            "team": {"key": "OHE"},
            "state": {"type": "started"},
            "labels": {"nodes": [{"name": "ohe-beta:implement"}]},
            "comments": {"nodes": []},
            "inverseRelations": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }
        raise OSError("lost Linear mutation response")


def test_linear_ticket_writeback_correlation_and_privacy(setup):
    github, config, tick = setup
    config["factory_handoff"]["repositories"][REPO]["mode"] = "linear"
    linear = LinearFixture()
    assert tick(linear=linear) == "implementing"
    assert tick(linear=linear) == "implementing"
    assert len(linear.writes) == 1
    assert f"GitHub issue: {URL}" in linear.writes[0]["description"]
    assert f"Repo: {REPO}" in linear.writes[0]["description"]
    assert "Human issue body" not in linear.writes[0]["description"]
    assert all(not endpoint.endswith("/assignees") for endpoint, *_ in github.writes)
    github.make_pr(branch="openhands/factory-issue-42", body="Refs #42")
    assert tick(linear=linear) == "checks"
    assert "private-space" not in github.comments[0]["body"]
    assert "OHE-123" not in github.comments[0]["body"]
    assert f"https://github.com/{REPO}/pull/13" in github.comments[0]["body"]


def test_real_cli_is_disabled_without_opt_in(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{}")
    run = subprocess.run(
        [sys.executable, str(SUMMARY), "--config", str(config)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(run.stdout) == {"status": "disabled", "issues": {}}


def test_competing_pull_after_review_and_maintainer_holds(setup):
    gh, _, tick = setup
    assert tick() == "implementing"
    gh.make_pr()
    gh.check_state = "green"
    gh.review("APPROVED")
    assert tick() == "accepted"
    gh.timeline.append(
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 14,
                    "state": "open",
                    "body": "Fixes #42",
                    "pull_request": {"url": "unused"},
                    "html_url": f"https://github.com/{REPO}/pull/14",
                }
            },
        }
    )
    assert tick() == "blocked"
    assert (
        "/pull/14)" in gh.comments[0]["body"] and "/pull/13)" in gh.comments[0]["body"]
    )
    gh.timeline.append(
        {
            "event": "labeled",
            "label": {"name": BLOCKED},
            "actor": {"login": "maintainer"},
        }
    )
    writes = len(gh.writes)
    assert tick() == "maintainer_hold"
    assert len(gh.writes) == writes


def test_existing_pull_or_customer_owner_never_dispatches(setup):
    gh, _, tick = setup
    gh.issue["labels"].append({"name": handoff.CUSTOMER})
    assert tick() == "ownership_conflict"
    assert not gh.writes
    gh.issue["labels"].pop()
    gh.make_pr()
    assert tick() == "blocked"
    assert not gh.issue["assignees"]
    gh.issue["labels"].append({"name": handoff.CUSTOMER})
    assert tick() == "ownership_conflict"
    assert "select one owner" in gh.comments[0]["body"]


def test_comment_verdict_and_promotion_readback(setup):
    gh, _, tick = setup
    assert tick() == "implementing"
    gh.make_pr()
    gh.check_state = "green"
    assert tick() == "reviewing"
    gh.review("COMMENTED")
    gh.reviews[-1]["body"] = (
        "Code assessment: material_findings\n\n🔄 CHANGES REQUESTED"
    )
    assert tick() == "revision-needed"
    gh.review("COMMENTED")
    gh.reviews[-1]["body"] = "⏸ INCONCLUSIVE"
    assert tick() == "incomplete"
    gh.review("APPROVED")
    assert tick() == "accepted"
    assert "(draft)" in gh.comments[0]["body"]
    gh.pr["draft"] = False
    assert tick() == "accepted"
    assert "(ready for review)" in gh.comments[0]["body"]
    assert "mark the draft ready" not in gh.comments[0]["body"]


def test_linear_dependencies_and_changed_target(setup):
    gh, config, tick = setup
    config["factory_handoff"]["repositories"][REPO]["mode"] = "linear"
    linear = LinearFixture()
    assert tick(linear=linear) == "implementing"
    relations = linear.created["inverseRelations"]["nodes"]
    relations.append({"type": "blocks", "issue": {"state": {"type": "started"}}})
    assert tick(linear=linear) == "blocked"
    relations.clear()
    assert tick(linear=linear) == "implementing"
    linear.created["description"] += "\nRepo: elsewhere/another"
    assert tick(linear=linear) == "blocked"
    assert len(linear.writes) == 1


def test_concurrent_work_on_a_different_branch_blocks_before_dispatch(setup):
    gh, _, tick = setup
    gh.timeline.append(
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 88,
                    "state": "open",
                    "body": "Closes #42",
                    "pull_request": {"url": "unused"},
                    "html_url": f"https://github.com/{REPO}/pull/88",
                }
            },
        }
    )
    assert tick() == "blocked"
    assert "/pull/88)" in gh.comments[0]["body"]
    assert not gh.issue["assignees"]


def test_prior_customer_claim_prevents_factory_transfer(setup, tmp_path):
    gh, _, tick = setup
    receipt = tmp_path / "customer-attempts" / REPO.casefold() / f"{NUMBER}.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"attempt":"customer-A"}')
    assert tick() == "customer_owned"
    assert not gh.writes


def test_transferred_issue_uses_no_factory_credential(setup):
    gh, _, tick = setup
    gh.issue["html_url"] = "https://github.com/Example/elsewhere/issues/42"
    assert tick() == "issue_identity_changed"
    assert not gh.writes


def test_native_journal_and_conflicting_receipts_survive_label_removal(setup, tmp_path):
    gh, _, tick = setup
    journal = tmp_path / "native-claims" / "topic.json"
    journal.parent.mkdir()
    journal.write_text(json.dumps({"topic": {"issue_url": URL}}))
    assert tick() == "native_owned"
    assert not gh.writes
    journal.unlink()
    assert tick() == "implementing"
    receipt = tmp_path / "customer-attempts" / REPO.casefold() / f"{NUMBER}.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"attempt":"customer-A"}')
    gh.issue["labels"] = []
    assert tick(issue_url=URL) == "ownership_conflict"
    assert "select one owner" in gh.comments[0]["body"]


def test_discovery_readiness_alone_never_authorizes_factory(setup):
    gh, _, tick = setup
    gh.issue["labels"].remove({"name": FACTORY})
    gh.timeline[:] = [
        {
            "event": "labeled",
            "label": {"name": EXTERNAL},
            "actor": {"login": "maintainer"},
        }
    ]
    assert tick(issue_url=URL) == "not_enrolled"
    assert not gh.writes
    gh.issue["labels"].append({"name": FACTORY})
    assert tick() == "untrusted_enrollment"
    gh.timeline.append(
        {"event": "labeled", "label": {"name": FACTORY}, "actor": {"login": "reporter"}}
    )
    assert tick() == "untrusted_enrollment"
    assert not gh.writes
    gh.timeline[-1]["actor"]["login"] = "maintainer"
    assert tick() == "implementing"


def test_removing_factory_enrollment_surfaces_existing_attempt(setup):
    gh, _, tick = setup
    assert tick() == "implementing"
    gh.issue["labels"].remove({"name": FACTORY})
    assert tick() == "enrollment_removed"
    assert "Reconcile the existing work" in gh.comments[0]["body"]
    assert sum(endpoint.endswith("/assignees") for endpoint, *_ in gh.writes) == 1
