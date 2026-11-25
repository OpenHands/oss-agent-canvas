import copy

import pytest

from public_status import (
    SCHEMA,
    STATUS_END,
    STATUS_START,
    make_status,
    managed_block,
    read_marker,
    status_marker,
    update_issue_status,
)
from read_status import status_of

URL = "https://github.com/Example/widgets/issues/17"
ENDPOINT = "repos/Example/widgets/issues/17"
CUSTOMER = "<!-- automation-improvement-customer: 1 -->"
FACTORY = "<!-- automation-improvement-factory: 1 -->"
IDENTITY = "<!-- automation-improvement-backlog: " + "b" * 20 + "/" + "c" * 20 + " -->"


def read(issue, publishers, comments=(), node_changes=None):
    issue = issue | {"node_id": "issue"}
    comments = [row | {"node_id": str(row["id"])} for row in comments]
    rows = {row["node_id"]: row for row in [issue, *comments]}

    def lookup(node_id):
        source = rows[node_id]
        url = URL if node_id == "issue" else URL + "#issuecomment-" + node_id
        context = {
            "url": URL,
            "state": issue["state"].upper(),
            "labels": {"nodes": issue["labels"], "pageInfo": {"hasNextPage": False}},
        }
        return {
            **(context if node_id == "issue" else {"issue": context}),
            "author": {"__typename": "User", "login": source["user"]["login"]},
            "editor": None,
            "lastEditedAt": None,
            "body": source["body"],
            "url": url,
        } | (node_changes or {})

    def pages(endpoint):
        return issue["labels"] if "/labels?" in endpoint else comments

    return status_of(issue, ENDPOINT, publishers, pages, lookup)


class Issue:
    def __init__(self):
        self.row = {
            "html_url": URL,
            "state": "open",
            "body": f"{IDENTITY}\n\nBefore.\n{STATUS_START}\nWaiting.\n{STATUS_END}\nAfter.",
            "labels": [{"name": "improvement-topic"}],
            "user": {"login": "triage"},
        }
        self.writes = 0

    def api(self, endpoint, method="GET", fields=()):
        assert endpoint == ENDPOINT
        if method == "PATCH":
            self.writes += 1
            self.row["body"] = fields[0].removeprefix("body=")
        return copy.deepcopy(self.row)


def test_summary_links_real_comment_and_preserves_human_prose_across_retries():
    gh = Issue()
    record = make_status(
        "revised",
        "writer",
        "reviewer",
        "Review the published change.",
        next_actor_login="reviewer-bot",
        pull_requests=[
            {
                "url": f"https://github.com/Example/widgets/pull/{n}",
                "state": "draft",
                "head": "d" * 40,
            }
            for n in range(1, 5)
        ],
        validation_url="https://github.com/Example/widgets/actions/runs/99",
    )
    update_issue_status(gh, ENDPOINT, 273, record)
    gh.row["body"] += "\nA follow-up from a maintainer."
    update_issue_status(gh, ENDPOINT, 273, record)
    assert gh.writes == 1
    body = gh.row["body"]
    assert URL + "#issuecomment-273" in body
    assert "**Next actor:** Reviewer (`reviewer-bot`)" in body
    visible = body.split("<!-- automation-improvement-status:")[0]
    for n, pr in enumerate(record["pull_requests"][:3], 1):
        assert f"[PR #{n}]({pr['url']})" in visible
        assert f"({pr['checks_url']})" in visible
    assert record["pull_requests"][3]["url"] not in visible
    assert f"[Customer validation evidence]({record['validation_url']})" in visible
    assert "\nBefore.\n" in body and body.endswith(
        "After.\nA follow-up from a maintainer."
    )
    assert read_marker(body) == record


def test_unmanaged_human_issues_are_untouched_and_malformed_blocks_are_reported():
    gh = Issue()
    gh.row["body"] = "Filed by a customer."
    record = make_status(
        "blocked", "writer", "maintainer", "Provide a test installation."
    )
    update_issue_status(gh, ENDPOINT, 273, record)
    assert gh.writes == 0
    gh.row["body"] = IDENTITY + "\n" + STATUS_START + STATUS_START + STATUS_END
    with pytest.raises(ValueError, match="Ambiguous"):
        managed_block(gh.row)


def test_record_roundtrip_and_untrusted_status_cannot_replace_current_comment():
    issue = Issue().row
    issue["labels"].extend(
        [{"name": "improvement-external"}, {"name": "improvement-customer"}]
    )
    rec = make_status(
        "blocked", "writer", "maintainer", "Choose a fixture --> before retrying."
    )
    line = status_marker(rec)
    assert line.count("-->") == 1 and read_marker(line) == rec
    other = make_status("accepted", "impostor", "maintainer", "Merge.")
    rows = [
        {
            "id": 1,
            "user": {"login": "other"},
            "body": CUSTOMER + "\n" + status_marker(other),
        },
        {
            "id": 2,
            "user": {"login": "writer"},
            "body": FACTORY + "\n" + status_marker(other),
        },
        {"id": 3, "user": {"login": "WRITER"}, "body": CUSTOMER + "\n" + line},
    ]
    assert read(issue, {"writer"}, rows) == rec | {
        "issue_url": URL,
        "status_url": URL + "#issuecomment-3",
        "publisher": "writer",
    }
    with pytest.raises(ValueError, match="Multiple trusted"):
        read(issue, {"writer"}, rows + rows)
    assert read(issue, {"writer"}, rows[:2]) is None


def test_managed_native_status_and_legacy_or_unknown_records():
    issue = Issue().row
    assert read(issue, {"triage"}) is None
    rec = make_status("ready", "native", "dispatcher", "Claim this investigation.")
    issue["body"] = issue["body"].replace("Waiting.", status_marker(rec))
    assert read(issue, {"triage"})["status"] == "ready"
    assert read(issue, {"impostor"}) is None
    with pytest.raises(ValueError, match="Unsupported"):
        read_marker(status_marker(rec).replace(SCHEMA, "unrecognized"))


def test_removed_enrollment_still_exposes_the_trusted_maintenance_notice():
    row = Issue().row
    row["labels"] = []
    row["body"] = "Original issue."
    info = make_status(
        "enrollment_removed", "writer", "maintainer", "Reconcile existing work."
    )
    comments = [
        {
            "id": 4,
            "body": FACTORY + "\n" + status_marker(info),
            "user": {"login": "dispatcher"},
        }
    ]
    found = read(row, {"dispatcher"}, comments)
    assert found["status"] == "enrollment_removed"
    assert found["status_url"].endswith("#issuecomment-4")


def test_reader_uses_active_route_even_with_damaged_body_and_old_route():
    issue = Issue().row
    issue["body"] += STATUS_START
    issue["labels"].extend(
        [{"name": "improvement-external"}, {"name": "improvement-customer"}]
    )
    rec = make_status("blocked", "writer", "maintainer", "Supply an isolated fixture.")
    rows = [
        {
            "id": 1,
            "user": {"login": "writer"},
            "body": FACTORY + "\n" + status_marker(rec).replace(SCHEMA, "old"),
        },
        {
            "id": 2,
            "user": {"login": "writer"},
            "body": CUSTOMER + "\n" + status_marker(rec),
        },
    ]
    assert read(issue, {"writer", "triage"}, rows)["status_url"].endswith(
        "#issuecomment-2"
    )


@pytest.mark.parametrize("source", ["body", "comment"])
def test_reader_requires_both_author_and_latest_editor_for_the_exact_content(source):
    issue = Issue().row
    rec = make_status("ready", "native", "dispatcher", "Claim this investigation.")
    comments = []
    if source == "body":
        issue["body"] = issue["body"].replace("Waiting.", status_marker(rec))
    else:
        issue["labels"] = [
            {"name": "improvement-external"},
            {"name": "improvement-customer"},
        ]
        comments = [
            {
                "id": 1,
                "user": issue["user"],
                "body": CUSTOMER + "\n" + status_marker(rec),
            }
        ]
    edited = {
        "editor": {"__typename": "Bot", "login": "status"},
        "lastEditedAt": "2026-11-24T14:00:00Z",
    }
    trusted = {"triage", "status[bot]"}
    assert read(issue, trusted, comments, edited)["publisher"] == "status[bot]"
    assert read(issue, {"status[bot]"}, comments, edited) is None
    with pytest.raises(ValueError, match="untrusted"):
        read(issue, {"triage"}, comments, edited)
    with pytest.raises(ValueError, match="unknown"):
        read(issue, trusted, comments, edited | {"editor": None})
    for mismatch in ({"body": "Edited during lookup."}, {"url": URL + "/other"}):
        with pytest.raises(ValueError, match="verify"):
            read(issue, trusted, comments, edited | mismatch)


def test_removed_external_label_hides_previous_customer_handoff():
    issue = Issue().row
    issue["labels"] = [{"name": "improvement-customer"}]
    rec = make_status("accepted", "writer", "maintainer", "Review the change.")
    comment = {
        "id": 5,
        "user": {"login": "writer"},
        "body": CUSTOMER + "\n" + status_marker(rec),
    }
    assert read(issue, {"writer"}, [comment]) is None


def test_reader_rechecks_route_while_verifying_the_comment():
    issue = Issue().row
    issue["labels"] = [
        {"name": "improvement-external"},
        {"name": "improvement-factory"},
    ]
    rec = make_status("accepted", "writer", "maintainer", "Review the change.")
    comments = [
        {
            "id": 3,
            "user": {"login": "writer"},
            "body": FACTORY + "\n" + status_marker(rec),
        }
    ]
    given = [{"name": "improvement-external"}, {"name": "improvement-customer"}]
    snapshot = {
        "url": URL,
        "state": "OPEN",
        "labels": {"nodes": given, "pageInfo": {"hasNextPage": False}},
    }
    with pytest.raises(ValueError, match="labels changed"):
        read(issue, {"writer"}, comments, {"issue": snapshot})
    snapshot["labels"] = {
        "nodes": issue["labels"][:1],
        "pageInfo": {"hasNextPage": True},
    }
    assert (
        read(issue, {"writer"}, comments, {"issue": snapshot})["status"] == "accepted"
    )


def test_pr_records_preserve_overflow_count_and_reject_inconsistent_checks():
    pulls = [
        {
            "url": f"https://github.com/Example/widgets/pull/{n}",
            "state": "draft",
            "head": "d" * 40,
        }
        for n in range(1, 35)
    ]
    rec = make_status(
        "reviewing", "native", "reviewer", "Review the changes.", pull_requests=pulls
    )
    assert len(rec["pull_requests"]) == 32 and rec["additional_pull_requests"] == 2
    assert read_marker(status_marker(rec)) == rec
    rec["pull_requests"][0]["checks_url"] = rec["pull_requests"][1]["checks_url"]
    with pytest.raises(ValueError, match="Checks URL"):
        read_marker(status_marker(rec))
