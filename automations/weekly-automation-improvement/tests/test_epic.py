import json
from pathlib import Path

import epic
import topic_issue
from backlog import block, namespace
from common import read_json
from github_io import IMPROVEMENT_LABEL


def test_epic_reuses_issue_and_tracks_open_work_across_repositories(
    tmp_path, monkeypatch
):
    home, upstream, private = "Example/customer", "Example/upstream", "Customer/private"
    config = {
        "repository": home,
        "project_id": "p",
        "signal_id": "s",
        "repositories": {upstream: {}, private: {}},
    }
    scope = namespace(config, tmp_path)

    def issue(repo, number, body="", labels=(), state="open", title=None):
        return {
            "number": number,
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "title": title or f"Example issue {number}",
            "body": body,
            "state": state,
            "labels": [{"name": n} for n in labels],
        }

    def topic(repo, number, owner=scope, **kwargs):
        return issue(
            repo,
            number,
            f"<!-- automation-improvement-backlog: {owner}/{'a' * 20} -->",
            ("improvement-topic", "improvement-ready", "priority:P1"),
            **kwargs,
        )

    weekly = issue(
        home,
        11,
        f"{epic.WEEKLY} 2026-W47 -->\n" + block("triage-epic", ""),
        [IMPROVEMENT_LABEL],
    )
    rows = {
        home: [topic(home, 1), weekly, topic(home, 2, "b" * 20), issue(home, 3)],
        upstream: [
            topic(upstream, 4),
            issue(upstream, 5, title="Related to Customer/private"),
            issue(upstream, 6, state="closed"),
            issue(upstream, 7) | {"pull_request": {}},
        ],
        private: [topic(private, 8)],
    }
    state = {
        "events": {"finding": {"existing_work_urls": [weekly["html_url"]]}},
        "topic_weeks": {
            "attempt": {
                "topic": {},
                "resources": [{"url": row["html_url"]} for row in rows[upstream][1:]]
                + [{"url": rows[private][0]["html_url"]}],
            },
        },
    }
    writes = []

    def pages(endpoint, *_):
        if "/comments" in endpoint:
            return []
        return rows[endpoint.split("/issues", 1)[0].removeprefix("repos/")]

    def request(argv):
        if "--method" in argv:
            method = argv[argv.index("--method") + 1]
            endpoint = argv[argv.index("--method") + 2]
            payload = read_json(Path(argv[-1]))
            writes.append(method)
            if method == "POST":
                row = issue(
                    home, 12, payload["body"], payload["labels"], title=payload["title"]
                )
                rows[home].append(row)
                return json.dumps(row)
        else:
            endpoint = argv[-1]
            payload = {}
        repo, number = endpoint.removeprefix("repos/").rsplit("/issues/", 1)
        row = next(item for item in rows[repo] if item["number"] == int(number))
        row.update(payload)
        return json.dumps(row)

    monkeypatch.setattr(topic_issue, "github_pages", pages)
    monkeypatch.setattr(topic_issue, "command", request)
    monkeypatch.setattr(epic, "github_pages", pages)
    monkeypatch.setattr(epic, "repository_private", lambda repo, _: repo == private)
    summary = {"url": weekly["html_url"], "body": weekly["body"]}
    result = epic.refresh(config, tmp_path, state, tmp_path / "first", summary)
    assert result == {
        "status": "refreshed",
        "url": f"https://github.com/{home}/issues/12",
        "open_issues": 3,
    }
    hub = rows[home][-1]
    assert f"[Open improvement issues]({hub['html_url']})" in weekly["body"]
    assert f"[Latest triage summary]({weekly['html_url']})" in hub["body"]
    assert "Example/customer#1" in hub["body"]
    assert "Example/upstream#4" in hub["body"]
    assert "Example/upstream#5" in hub["body"]
    assert "Example/customer#2" not in hub["body"]
    assert "Example/customer#3" not in hub["body"]
    assert "Example/upstream#6" not in hub["body"]
    assert "Example/upstream#7" not in hub["body"]
    assert private not in hub["body"]

    hub["body"] += "\n\nHuman notes stay here."
    rows[upstream][1]["state"] = "closed"
    rows[upstream][0]["labels"][-1]["name"] = "priority:P0"
    result = epic.refresh(config, tmp_path, state, tmp_path / "second")
    assert result["open_issues"] == 2
    assert writes == ["POST", "PATCH", "PATCH"]
    assert "Human notes stay here." in hub["body"]
    assert f"[Latest triage summary]({weekly['html_url']})" in hub["body"]
    assert "Example/upstream#5" not in hub["body"]
    assert hub["body"].index("Example/upstream#4") < hub["body"].index(
        "Example/customer#1"
    )

    epic.refresh(config, tmp_path, state, tmp_path / "unchanged")
    assert writes == ["POST", "PATCH", "PATCH"]

    def visibility(repo, _):
        if repo == upstream:
            raise ValueError("unavailable")
        return repo == private

    monkeypatch.setattr(epic, "repository_private", visibility)
    partial = epic.refresh(config, tmp_path, state, tmp_path / "partial")
    assert partial["status"] == "partially_refreshed"
    assert partial["unverified_repositories"] == [upstream]
    assert upstream not in hub["body"]
    assert writes == ["POST", "PATCH", "PATCH", "PATCH"]
    hub["state"] = "closed"
    assert (
        epic.refresh(config, tmp_path, state, tmp_path / "closed")["status"] == "closed"
    )
    assert writes == ["POST", "PATCH", "PATCH", "PATCH"]
