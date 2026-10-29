import copy
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tarball"))
import topic_issue
from topic_issue import TopicIssue, IssueReader, SECTIONS


class GitHub:
    def __init__(self, private=True):
        self.private = private
        self.issues, self.comments, self.calls = [], [], []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:3] == ["gh", "label", "create"]:
            return ""
        assert argv[:2] == ["gh", "api"]
        endpoint = next(item for item in argv if item.startswith("repos/"))
        payload = (
            json.loads(Path(argv[argv.index("--input") + 1]).read_text())
            if "--input" in argv
            else None
        )
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        repository = "/".join(endpoint.split("/")[1:3])
        if endpoint == "repos/" + repository:
            value = {"private": self.private}
        elif "/issues/comments/" in endpoint:
            identifier = int(endpoint.rsplit("/", 1)[1])
            value = next(item for item in self.comments if item["id"] == identifier)
        elif "/comments" in endpoint:
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            issue_url = f"https://api.github.com/repos/{repository}/issues/{number}"
            if method == "POST":
                assert len(payload["body"]) < 65536
                value = {
                    "id": len(self.comments) + 1,
                    "html_url": f"https://github.com/{repository}/issues/{number}#issuecomment-{len(self.comments) + 1}",
                    "issue_url": issue_url,
                    **payload,
                }
                self.comments.append(value)
            else:
                value = [
                    item for item in self.comments if item["issue_url"] == issue_url
                ]
        elif re.search(r"/issues/[0-9]+$", endpoint):
            number = int(endpoint.rsplit("/", 1)[1])
            value = next(item for item in self.issues if item["number"] == number)
            if method == "PATCH":
                value.update(payload)
        elif method == "POST":
            value = {
                "number": len(self.issues) + 1,
                "html_url": f"https://github.com/{repository}/issues/{len(self.issues) + 1}",
                "state": "open",
                **payload,
            }
            self.issues.append(value)
        else:
            value = self.issues
        return json.dumps(copy.deepcopy(value))


def snapshot(tmp_path):
    return {
        "project_id": "project",
        "app_url": None,
        "source": {
            "repository": "Example/source",
            "revision": "a" * 40,
            "automation_ids": ["automation"],
            "files": {"worker.py": "source"},
        },
        "issue_evidence_dir": str(tmp_path / "private-evidence"),
        "topic": {
            "id": "topic",
            "key": "one-failure",
            "title": "One failure",
            "reason": "Investigate evidence",
        },
        "samples": [
            {
                "case_id": "c1",
                "conversation_id": "conversation",
                "trace_ids": ["trace"],
                "selected_event_ids": ["event"],
                "initial_instructions": [{"role": "user", "content": "Original task"}],
            }
        ],
    }


def test_issue_sections_reuse_redaction_and_human_content(tmp_path, monkeypatch):
    github = GitHub()
    monkeypatch.setattr(topic_issue, "command", github)
    issue = TopicIssue.ensure(snapshot(tmp_path), tmp_path / "first")
    issue.frozen(snapshot(tmp_path))
    issue.value["body"] += "\nHuman clarification stays here.\n"
    github.issues[0]["body"] = issue.value["body"]
    issue.record(
        "Attempts and decisions — first",
        "Not proven",
        {"failure.json": {"api_key": "private-value", "outcome": "Wrong labels"}},
    )
    initial_count = len(github.comments)
    again = TopicIssue.ensure(snapshot(tmp_path), tmp_path / "second")
    again.record(
        "Attempts and decisions — first",
        "Not proven",
        {"failure.json": {"api_key": "private-value", "outcome": "Wrong labels"}},
    )
    assert len(github.issues) == 1 and len(github.comments) == initial_count
    assert "Human clarification stays here" in again.value["body"]
    assert all("## " + section in issue.value["body"] for section in SECTIONS)
    assert "private-value" not in json.dumps(github.comments)
    reader = IssueReader(SimpleNamespace(schemas=[]), again, tmp_path / "reads")
    result = reader.execute("read_issue", {"section": "Validation", "offset": 0})
    assert result["content"].startswith("## Validation\n")
    assert "## Evidence" not in result["content"]
    assert result["records"]
    result = reader.execute("read_issue", {"section": "Evidence", "offset": 0})
    assert "#issuecomment-" in result["content"]


def test_large_private_evidence_is_complete_paged_and_scoped(tmp_path, monkeypatch):
    github = GitHub(private=False)
    monkeypatch.setattr(topic_issue, "command", github)
    issue = TopicIssue.ensure(snapshot(tmp_path), tmp_path / "first")
    content = {"events": [f"Tool {i}: actual observed output\n" for i in range(9000)]}
    artifact = issue.artifact("events.json", content)
    reader = IssueReader(SimpleNamespace(schemas=[]), issue, tmp_path / "reads")
    offset, pages = 0, []
    while True:
        page = reader.execute(
            "read_issue_artifact",
            {"reference": artifact["reference"], "offset": offset},
        )
        pages.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(pages)) == content
    issue.record(
        "Attempts and decisions — test", "Not proven", {"events.json": content}
    )
    public = json.dumps(github.issues + github.comments)
    assert "actual observed output" not in public
    assert artifact["reference"] in public
    assert artifact["reference"] in topic_issue.redact(public)
    other = snapshot(tmp_path)
    other["issue_evidence_dir"] = str(tmp_path / "other-private-evidence")
    isolated = TopicIssue.ensure(other, tmp_path / "other")
    with pytest.raises(ValueError, match="missing"):
        isolated.read_artifact(artifact["reference"])
    with pytest.raises(ValueError):
        issue.read_artifact("../../outside")
    path = issue.evidence_root / artifact["reference"]
    path.write_text("tampered")
    with pytest.raises(ValueError, match="modified"):
        issue.read_artifact(artifact["reference"])


def test_public_issue_contains_references_only_and_comment_reads_are_scoped(
    tmp_path, monkeypatch
):
    github = GitHub(private=False)
    monkeypatch.setattr(topic_issue, "command", github)
    data = snapshot(tmp_path)
    data["topic"]["reason"] = "Private customer detail"
    issue = TopicIssue.ensure(data, tmp_path / "first")
    issue.frozen(data)
    assert issue.repository == data["source"]["repository"]
    assert "Private customer detail" not in json.dumps(github.issues + github.comments)
    reader = IssueReader(SimpleNamespace(schemas=[]), issue, tmp_path / "reads")
    identifier = github.comments[0]["id"]
    assert reader.execute(
        "read_issue_comment", {"comment_id": identifier, "offset": 0}
    )["content"]
    github.comments[0]["issue_url"] = (
        "https://api.github.com/repos/Other/project/issues/1"
    )
    with pytest.raises(ValueError, match="outside"):
        reader.execute("read_issue_comment", {"comment_id": identifier, "offset": 0})
    with pytest.raises(ValueError):
        reader.execute("read_issue_comment", {"comment_id": True, "offset": 0})


def test_preview_never_calls_github(tmp_path, monkeypatch):
    monkeypatch.setattr(
        topic_issue, "command", lambda *a: pytest.fail("Preview called GitHub")
    )
    issue = TopicIssue.ensure(snapshot(tmp_path), tmp_path, preview=True)
    issue.frozen(snapshot(tmp_path))
    issue.attempt(tmp_path / "analysis", {"proposals": [], "errors": []})
    assert issue.context()["url"] is None
    assert len(issue.context()["records"]) == 2


def test_issue_reader_keeps_existing_production_evidence_tools(tmp_path):
    calls = []
    original = SimpleNamespace(
        schemas=[{"name": "read_trace"}],
        execute=lambda name, args: calls.append((name, args))
        or {"raw": "production evidence"},
    )
    issue = TopicIssue.ensure(snapshot(tmp_path), tmp_path / "preview", preview=True)
    reader = IssueReader(original, issue, tmp_path / "reads")
    assert {item["name"] for item in reader.schemas} == {
        "read_trace",
        "read_issue",
        "read_issue_comment",
        "read_issue_artifact",
    }
    assert reader.execute("read_trace", {"offset": 0}) == {"raw": "production evidence"}
    assert calls == [("read_trace", {"offset": 0})]


def test_failed_execution_context_is_published_without_a_receipt(tmp_path, monkeypatch):
    github = GitHub()
    monkeypatch.setattr(topic_issue, "command", github)
    issue = TopicIssue.ensure(snapshot(tmp_path), tmp_path / "issue")
    directory = tmp_path / "analysis/comparison/runs/negative-1-1-candidate"
    directory.mkdir(parents=True)
    (directory / "events.json").write_text(
        json.dumps([{"tool": "rejected write", "error": "Actual API error"}])
    )
    (directory / "prompt.json").write_text(
        json.dumps({"prompt": "Original task and exact policy"})
    )
    requests = tmp_path / "analysis/requests"
    requests.mkdir()
    (requests / "coordinator.json").write_text(
        json.dumps({"input": "Exact private repair prompt"})
    )
    issue.attempt(tmp_path / "analysis", {"proposals": [], "errors": []})
    bodies = "\n".join(item["body"] for item in github.comments)
    assert (
        "Actual API error" not in bodies
        and "Original task and exact policy" not in bodies
    )
    assert "negative-1-1-candidate/events.json" in bodies
    assert (
        "requests/coordinator.json" in bodies
        and "Exact private repair prompt" not in bodies
    )
    private = "\n".join(path.read_text() for path in issue.evidence_root.glob("*.json"))
    assert "Actual API error" in private and "Original task and exact policy" in private
    assert "Exact private repair prompt" in private
