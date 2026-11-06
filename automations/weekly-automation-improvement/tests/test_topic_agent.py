import copy
import json
from pathlib import Path

import pytest

from budget import RunBudget
from common import write_json
import topic_agent
from test_topic_issue import public_body


@pytest.fixture(autouse=True)
def public_repository_metadata(monkeypatch):
    monkeypatch.setattr(
        topic_agent, "repository_private", lambda repository, request: False
    )


def context(tmp_path):
    for name in ("snapshot", "cohort"):
        write_json(tmp_path / (name + ".json"), {})
    return {
        "topic": {
            "id": "topic",
            "key": "shared-bug",
            "title": "A shared bug",
            "representative_event_ids": ["event"],
        },
        "automation_ids": ["one", "two"],
        "repositories": {},
        "repair_policy": {"scope": "any_authorized", "repositories": []},
        "snapshot_path": str(tmp_path / "snapshot.json"),
        "cohort_path": str(tmp_path / "cohort.json"),
        "state_dir": str(tmp_path),
        "validation_sample_size": 5,
        "triage_issue": {"url": "https://github.com/Example/automation/issues/1"},
    }


def resources():
    issues, prs = [], []
    for repo in ("Example/automation", "Example/sdk"):
        issues.append(
            {
                "kind": "issues",
                "state": "open",
                "url": f"https://github.com/{repo}/issues/1",
                "repository": repo,
                "body": "",
                "number": 1,
            }
        )
        prs.append(
            {
                "kind": "pull",
                "state": "open",
                "url": f"https://github.com/{repo}/pull/2",
                "repository": repo,
                "body": "",
                "number": 2,
                "draft": True,
                "base": "a" * 40,
                "head": "b" * 40,
            }
        )
    for row in issues + prs:
        row["body"] = public_body("\n".join(r["url"] for r in issues + prs))
    return issues + prs


def finished_conversation(config, phase, context_path, output, *args):
    if phase == "review":
        write_json(
            output / "review-decision.json",
            {"decision": "accepted", "feedback": "Scoped improvement verified."},
        )
    return {"status": "finished"}


def test_issue_topology_allows_multiple_prs_and_reciprocal_repository_issues():
    rows = resources()
    extra = rows[-1] | {"url": "https://github.com/Example/sdk/pull/3", "number": 3}
    rows.append(extra)
    for row in rows:
        row["body"] += "\n" + extra["url"]
    assert topic_agent.link_errors(rows) == []
    assert topic_agent.link_errors([r for r in rows if r != rows[1]])
    rows[0]["body"] = "Missing reciprocal links"
    assert any("does not link" in error for error in topic_agent.link_errors(rows))


@pytest.mark.parametrize(
    "review_status,changed",
    [("finished", False), ("failed", False), ("finished", True)],
)
def test_independent_review_is_once_and_incomplete_or_stale_prs_stay_draft(
    tmp_path, monkeypatch, review_status, changed
):
    ctx = context(tmp_path)
    original = resources()
    updated = copy.deepcopy(original)
    for row in updated:
        if row["kind"] == "pull":
            row["draft"] = False
            if changed:
                row["head"] = "c" * 40
    calls, holds = [], []

    def converse(config, phase, context_path, output, *args):
        calls.append(phase)
        if phase == "review" and review_status == "finished":
            finished_conversation(config, phase, context_path, output)
        return {"status": "finished" if phase == "repair" else review_status}

    snapshots = iter([original, updated])
    monkeypatch.setattr(topic_agent, "run_conversation", converse)
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": next(snapshots), "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    monkeypatch.setattr(
        topic_agent, "command", lambda argv, *a: holds.append(argv) or ""
    )
    result = topic_agent.run({}, ctx, tmp_path / "topic", RunBudget({}))
    assert calls == ["repair", "review"]
    incomplete = changed or review_status == "failed"
    assert result["status"] == ("review_incomplete" if incomplete else "reviewed")
    assert len(holds) == (2 if incomplete else 0)
    assert all("--undo" in argv for argv in holds)
    assert len(result["resources"]) == 4


def test_repair_catalog_is_retried_after_reviewer_repairs_issue_anchors(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    phases = []

    def record(ctx, root, phase, rows):
        phases.append(phase)
        return [{"error": "missing anchors"}] if len(phases) == 1 else []

    monkeypatch.setattr(topic_agent, "record_evidence", record)
    monkeypatch.setattr(topic_agent, "run_conversation", finished_conversation)
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": resources(), "errors": []}
    )
    result = topic_agent.run({}, ctx, tmp_path / "topic", RunBudget({}))
    assert phases == ["repair", "repair", "review"]
    assert result["status"] == "reviewed" and result["errors"] == []


def test_normal_prompt_delegates_full_workflow_without_patch_or_repo_restriction(
    tmp_path,
):
    ctx = context(tmp_path)
    repair = topic_agent.prompt(ctx, tmp_path / "context.json", tmp_path)
    review = topic_agent.prompt(ctx, tmp_path / "context.json", tmp_path, review=True)
    assert "no separate publisher" in repair
    assert "not a destination restriction" in repair
    assert "several PRs" in repair and "N=5" in repair
    assert "ONE structured issue brief in each repository" in repair
    assert "initially leave it draft" in repair
    assert "Treat the fixer's report as a claim" in review
    assert "Do not edit/push repair source" in review
    assert "task tool is blocking" in repair and "task tool is blocking" in review
    assert "freeze candidate source before delegating" in repair
    for text in (repair, review):
        assert "search open issues and PRs before creating duplicates" in text
        assert (
            "Existing human-authored and previous-run issue descriptions must remain unchanged"
            in text
        )
        assert "BOTH this topic's marker and this run's marker" in text
        assert "context.json beside the previous_attempts" in text
        assert "Before every public GitHub write" in text
    isolated = topic_agent.prompt(
        ctx | {"isolated_validation": True}, tmp_path / "context.json", tmp_path
    )
    assert (
        "Do not search, read, reuse, comment on or cite earlier prototype" in isolated
    )
    assert "search open issues and PRs before creating duplicates" not in isolated


def test_discovery_recovers_links_outside_catalog_from_actual_native_events(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    url = "https://github.com/Example/sdk/pull/12"
    write_json(tmp_path / "observed-links.json", [url])
    monkeypatch.setattr(topic_agent, "github_pages", lambda *a: [])
    monkeypatch.setattr(
        topic_agent,
        "command",
        lambda *a: json.dumps(
            {
                "body": topic_agent.marker(ctx["topic"]),
                "state": "open",
                "title": "SDK fix",
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40},
                "draft": True,
            }
        ),
    )
    result = topic_agent.discover(ctx, tmp_path)
    assert result["resources"][0]["repository"] == "Example/sdk"
    assert result["resources"][0]["url"] == url
    assert result["errors"] == []


def test_native_runtime_captures_events_links_and_cost_on_failure(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from test_coding_agent import install_sdk

    ctx = context(tmp_path)
    root = tmp_path / "topic"
    root.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    context_path = root / "context.json"
    write_json(context_path, ctx)

    def execute(_):
        callback = captured["conversation"]["callbacks"][0]
        for n in (1, 2):
            callback(
                SimpleNamespace(
                    model_dump=lambda **kwargs: {
                        "url": f"https://github.com/Example/sdk/pull/{n}"
                    }
                )
            )
        raise RuntimeError("tool failure after remote work")

    captured = install_sdk(monkeypatch, execute)
    monkeypatch.setattr(topic_agent, "version", lambda name: "test-sdk")
    budget = RunBudget({"max_inference_cost_usd": 5})
    result = topic_agent.run_conversation(
        {}, "repair", context_path, root, workspace, "Repair this topic", budget
    )
    assert result["status"] == "failed"
    assert "tool failure" in result["error"]
    assert budget.cost == 0.12 and captured["closed"]
    links = json.loads((root / "observed-links.json").read_text())
    assert len(links) == 2
    assert len(json.loads((root / "repair-agent/events.json").read_text())) == 2
    assert (root / "repair-agent/events.json").stat().st_mode & 0o077 == 0


def test_private_catalog_keeps_evidence_and_omits_environments_and_checkouts(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    ctx = context(tmp_path)
    output = tmp_path / "topic"
    validation = output / "validation"
    files = {
        "results/paired.txt": "baseline fails, candidate passes",
        "harness/replay.py": "assert True",
        ".venv/lib/dependency.py": "not evidence",
        "baseline-env/pyvenv.cfg": "home = /python",
        "baseline-env/lib/dependency.py": "not evidence",
        "baseline-source/source.py": "tracked elsewhere",
        "candidate-source/source.py": "tracked elsewhere",
        "checkout/.git/HEAD": "main",
        "checkout/source.py": "tracked elsewhere",
        "private-home/settings.json": "not evidence",
        "results/__pycache__/cache.py": "not evidence",
    }
    for relative, content in files.items():
        path = validation / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    captured = []

    def reference(ctx, url, directory):
        return SimpleNamespace(
            value={"body": ""},
            update_body=lambda body: None,
            related=lambda urls: None,
            record=lambda title, summary, artifacts: captured.append(artifacts)
            or {"url": url},
        )

    monkeypatch.setattr(topic_agent, "issue_reference", reference)
    resource = {
        "kind": "issues",
        "repository": "Example/repo",
        "url": "https://github.com/Example/repo/issues/1",
        "state": "open",
    }
    assert topic_agent.record_evidence(ctx, output, "repair", [resource]) == []
    assert topic_agent.record_evidence(ctx, output, "repair", [resource]) == []
    assert len(captured) == 2
    catalog = captured[-1]
    assert (
        catalog["validation/results/paired.txt"]["content"]
        == files["results/paired.txt"]
    )
    assert "validation/harness/replay.py" in catalog
    assert not any(
        "dependency.py" in name or name.endswith("source.py") or "settings.json" in name
        for name in catalog
    )
    assert (
        "baseline-env"
        in catalog["validation-catalog.json"][
            "omitted_environment_or_checkout_directories"
        ]
    )


def test_saved_repair_can_finish_without_repeating_coding(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    phases = []

    def converse(config, phase, context_path, output, *a):
        phases.append(phase)
        return finished_conversation(config, phase, context_path, output)

    monkeypatch.setattr(topic_agent, "run_conversation", converse)
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": resources(), "errors": []}
    )
    output = tmp_path / "topic"
    write_json(output / "topic-report.json", {"status": "review_incomplete"})
    report = topic_agent.finish(
        {},
        ctx,
        output,
        RunBudget({}),
        {"status": "finished", "sdk_estimated_cost_usd": 2},
    )
    assert phases == ["review"]
    assert report["status"] == "reviewed"
    assert report["repair"]["sdk_estimated_cost_usd"] == 2
    assert (
        json.loads((output / "topic-report.json").read_text())["status"] == "reviewed"
    )


def test_deadline_escapes_tool_exception_shield(tmp_path, monkeypatch):
    import signal
    from test_coding_agent import install_sdk

    output = tmp_path / "topic"
    output.mkdir()
    workspace = output / "workspace"
    workspace.mkdir()
    swallowed = []

    def execute(_):
        try:
            signal.getsignal(signal.SIGALRM)(signal.SIGALRM, None)
        except Exception:
            swallowed.append(True)

    captured = install_sdk(monkeypatch, execute)
    monkeypatch.setattr(topic_agent, "version", lambda name: "test-sdk")
    report = topic_agent.run_conversation(
        {},
        "repair",
        output / "context.json",
        output,
        workspace,
        "Repair",
        RunBudget({}),
    )
    assert not swallowed
    assert report["status"] == "failed"
    assert "reached its time limit" in report["error"]
    assert captured["closed"]


def test_evidence_index_links_all_repository_issues_and_prs_without_losing_notes(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    rows = resources()
    issues = {}

    class Issue:
        def __init__(self, url):
            self.value = {"body": "Human note stays here."}
            self.url = url

        def update_body(self, body):
            self.value["body"] = body

        def related(self, urls):
            self.value["body"] += "\n" + "\n".join(urls)

        def record(self, *a):
            return {"url": self.url}

    def reference(ctx, url, directory):
        return issues.setdefault(url, Issue(url))

    for row in rows:
        if row["kind"] == "issues":
            row["body"] = "Human note stays here."
    monkeypatch.setattr(topic_agent, "issue_reference", reference)
    assert topic_agent.record_evidence(ctx, tmp_path / "topic", "repair", rows) == []
    for row in rows:
        if row["kind"] == "issues":
            assert row["body"].startswith("Human note stays here.")
    assert topic_agent.link_errors(rows) == []


def test_completed_review_cannot_publish_an_unstructured_issue_as_ready(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    rows = resources()
    for row in rows:
        if row["kind"] == "issues":
            row["body"] = "Generic investigation.\n" + "\n".join(r["url"] for r in rows)
        else:
            row["draft"] = False
    held = []
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": rows, "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    monkeypatch.setattr(
        topic_agent, "run_conversation", lambda *a: {"status": "finished"}
    )
    monkeypatch.setattr(topic_agent, "command", lambda args: held.append(args) or "")
    outcome = topic_agent.finish(
        {}, ctx, tmp_path / "result", RunBudget({}), {"status": "finished"}
    )
    assert outcome["status"] == "review_incomplete"
    assert any("public section" in error for error in outcome["link_errors"])
    assert len(held) == 2


def test_run_identity_never_adopts_a_previous_prototype_issue(tmp_path):
    ctx = context(tmp_path) | {"run_id": "new-run"}
    old = topic_agent.marker(ctx["topic"])
    assert not topic_agent.owns(ctx, old)
    assert not topic_agent.owns(
        ctx, old + "\n<!-- automation-improvement-run: old-run -->"
    )
    assert topic_agent.owns(ctx, old + "\n<!-- automation-improvement-run: new-run -->")


@pytest.mark.parametrize("kind,field", [("issues", "title"), ("pull", "body")])
def test_known_credential_in_any_owned_description_prevents_readiness(
    tmp_path, monkeypatch, kind, field
):
    ctx = context(tmp_path)
    rows = resources()
    row = next(row for row in rows if row["kind"] == kind)
    row[field] = row.get(field, "") + " sk-testcredential123456789"
    for row in rows:
        if row["kind"] == "pull":
            row["draft"] = False
    held = []
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": rows, "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    monkeypatch.setattr(
        topic_agent, "run_conversation", lambda *a: {"status": "finished"}
    )
    monkeypatch.setattr(topic_agent, "command", lambda args: held.append(args) or "")
    result = topic_agent.finish(
        {}, ctx, tmp_path / "result", RunBudget({}), {"status": "finished"}
    )
    assert result["status"] == "review_incomplete"
    assert any(
        "possible credential in public " + field in e for e in result["link_errors"]
    )
    assert len(held) == 2


def test_prior_context_reads_its_catalog_without_adopting_the_issue(
    tmp_path, monkeypatch
):
    import hashlib

    current = context(tmp_path) | {"run_id": "current"}
    prior = current | {"run_id": "prior", "state_dir": str(tmp_path / "old-state")}
    url = "https://github.com/Example/sdk/issues/9"
    body = (
        topic_agent.marker(prior["topic"])
        + "\n<!-- automation-improvement-run: prior -->"
    )
    value = {"number": 9, "body": body, "html_url": url}
    root = tmp_path / "old-state/issue-evidence/Example/sdk/topic"
    root.mkdir(parents=True)
    artifact = '{"evidence":"previous run"}'
    reference = hashlib.sha256(artifact.encode()).hexdigest() + ".json"
    (root / reference).write_text(artifact)
    monkeypatch.setattr(topic_agent, "command", lambda *a: json.dumps(value))
    monkeypatch.setattr(topic_agent, "github_pages", lambda *a: [])
    with pytest.raises(ValueError, match="does not reference"):
        topic_agent.issue_reference(current, url, tmp_path / "current-read")
    issue = topic_agent.issue_reference(prior, url, tmp_path / "prior-read")
    assert issue.read_artifact(reference) == artifact
    assert not topic_agent.owns(current, body)


def test_existing_issue_uses_a_current_run_comment_without_editing_its_description(
    tmp_path, monkeypatch
):
    import topic_issue
    from test_topic_issue import GitHub, snapshot

    github = GitHub(private=False)
    monkeypatch.setattr(topic_issue, "command", github)
    monkeypatch.setattr(topic_agent, "command", github)
    original = topic_issue.TopicIssue.ensure(snapshot(tmp_path), tmp_path / "initial")
    original_body = "A human reported the same problem. Preserve this description."
    github.issues[0]["body"] = original_body
    ctx = context(tmp_path) | {"run_id": "new-run"}
    ctx["repositories"] = {"Example/source": {}}
    body = (
        topic_agent.marker(ctx["topic"])
        + "\n<!-- automation-improvement-run: new-run -->\n"
        + public_body()
    )
    github.comments.append(
        {
            "id": 1,
            "body": body,
            "html_url": original.value["html_url"] + "#issuecomment-1",
            "issue_url": "https://api.github.com/repos/Example/source/issues/1",
        }
    )
    issue = topic_agent.issue_reference(
        ctx, original.value["html_url"], tmp_path / "read"
    )
    assert issue.brief_comment_id == 1
    issue.record(
        "Evidence — current run", "Sanitized finding.", {"run.json": {"raw": "PRIVATE"}}
    )
    issue.update_body(
        issue.value["body"].replace("Investigation underway", "Review finished")
    )
    issue.refresh()
    assert "Review finished" in issue.value["body"]
    assert "#issuecomment-2" in issue.value["body"]
    assert github.issues[0]["body"] == original_body
    assert "PRIVATE" not in json.dumps(github.comments)
    assert not any(
        "PATCH" in call and "repos/Example/source/issues/1" in call
        for call in github.calls
    )
    found = topic_agent.discover(ctx, tmp_path)["resources"]
    assert len(found) == 1 and found[0]["brief_comment_id"] == 1
    assert "Review finished" in found[0]["body"]
    isolated = ctx | {"isolated_validation": True}
    with pytest.raises(ValueError, match="fresh issue"):
        topic_agent.issue_reference(
            isolated, original.value["html_url"], tmp_path / "qc-read"
        )
    write_json(tmp_path / "observed-links.json", [original.value["html_url"]])
    receipt = topic_agent.discover(isolated, tmp_path)
    assert not receipt["resources"]
    assert "fresh issue" in receipt["errors"][0]["error"]


def test_duplicate_current_run_briefs_are_rejected(tmp_path):
    ctx = context(tmp_path) | {"run_id": "run"}
    body = (
        topic_agent.marker(ctx["topic"]) + "\n<!-- automation-improvement-run: run -->"
    )
    with pytest.raises(ValueError, match="one current-run brief"):
        topic_agent.current_brief(ctx, {"body": body}, [{"id": 1, "body": body}])


def test_isolated_discovery_ignores_weekly_and_source_issues(tmp_path, monkeypatch):
    ctx = context(tmp_path) | {"run_id": "current", "isolated_validation": True}
    urls = [ctx["triage_issue"]["url"], "https://github.com/Example/upstream/issues/8"]
    write_json(tmp_path / "observed-links.json", urls)
    monkeypatch.setattr(topic_agent, "github_pages", lambda *a: [])
    monkeypatch.setattr(
        topic_agent, "command", lambda *a: json.dumps({"body": "Original source issue"})
    )
    assert topic_agent.discover(ctx, tmp_path) == {"resources": [], "errors": []}


def test_public_image_identifiers_do_not_bypass_credential_checks(monkeypatch):
    head = "a" * 40
    branch = "fix/terminal-redact-cli-credentials-before-observation-clipping"
    image = "ghcr.io/openhands/agent-server:"
    resource = {
        "url": "https://github.com/Example/sdk/pull/2",
        "head": head,
        "head_ref": branch,
        "body": "\n".join(
            image + tag
            for tag in (
                head + "-python-amd64",
                head[:7] + "-python-amd64",
                branch.replace("/", "-") + "-python-arm64",
                "pr-" + branch.replace("/", "-") + "-python-arm64",
            )
        ),
    }
    assert topic_agent.public_credential_errors(resource) == []
    opaque = "Az9QwxELn5RPvz2Y" * 4
    for tag in (opaque, head + "-" + opaque, head + "-sk-testcredential123456789"):
        assert topic_agent.public_credential_errors(resource | {"body": image + tag})
    for branch_with_credential in (
        "feature-sk-testcredential123456789",
        "feature-with-a-long-public-looking-branch-name-sk-testcredential123456789",
    ):
        assert topic_agent.public_credential_errors(
            resource
            | {
                "head_ref": branch_with_credential,
                "body": image + "pr-" + branch_with_credential + "-python",
            }
        )
    branch_with_separators = "fix/mixed__separators//for-public-image-identifiers"
    assert (
        topic_agent.public_credential_errors(
            resource
            | {
                "head_ref": branch_with_separators,
                "body": image
                + "fix-mixed-separators-for-public-image-identifiers-python-arm64",
            }
        )
        == []
    )
    monkeypatch.setenv("CUSTOMER_API_KEY", head)
    assert topic_agent.public_credential_errors(resource)
    monkeypatch.delenv("CUSTOMER_API_KEY")
    monkeypatch.setenv("CUSTOMER_API_KEY", branch.replace("/", "-"))
    assert topic_agent.public_credential_errors(resource)


def test_fixture_urls_do_not_hide_missing_declared_outputs(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    url = "https://github.com/owner/repo/pull/42"
    write_json(tmp_path / "observed-links.json", [url])
    monkeypatch.setattr(topic_agent, "github_pages", lambda *a: [])

    def missing(*args):
        raise ValueError("Not found")

    monkeypatch.setattr(topic_agent, "command", missing)
    assert topic_agent.discover(ctx, tmp_path) == {"resources": [], "errors": []}
    assert json.loads((tmp_path / "reference-errors.json").read_text())[0]["url"] == url
    (tmp_path / "progress.md").write_text("Repair PR: " + url)
    receipt = topic_agent.discover(ctx, tmp_path)
    assert receipt["errors"][0]["url"] == url


def test_previously_owned_resources_remain_required_after_a_fetch_failure(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    url = "https://github.com/Example/other/pull/9"
    write_json(
        tmp_path / "resources.json",
        {
            "resources": [
                {
                    "url": url,
                    "body": topic_agent.marker(ctx["topic"]),
                }
            ]
        },
    )
    monkeypatch.setattr(topic_agent, "github_pages", lambda *a: [])

    def missing(*args):
        raise ValueError("Not found")

    monkeypatch.setattr(topic_agent, "command", missing)
    receipt = topic_agent.discover(ctx, tmp_path)
    assert receipt["errors"][0]["url"] == url


def test_shared_weekly_issue_is_readable_but_never_a_topic_output(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path) | {
        "run_id": "current",
        "isolated_validation": True,
        "triage_evidence_dir": str(tmp_path / "weekly-evidence"),
    }
    url = ctx["triage_issue"]["url"]
    value = {"body": "Shared weekly context", "number": 1, "html_url": url}
    comment = {
        "id": 9,
        "body": topic_agent.marker(ctx["topic"])
        + "\n<!-- automation-improvement-run: current -->",
    }
    monkeypatch.setattr(topic_agent, "command", lambda *a: json.dumps(value))
    monkeypatch.setattr(topic_agent, "github_pages", lambda *a: [comment])
    write_json(tmp_path / "observed-links.json", [url])
    (tmp_path / "progress.md").write_text("Weekly source: " + url)
    assert topic_agent.discover(ctx, tmp_path) == {"resources": [], "errors": []}
    issue = topic_agent.issue_reference(ctx, url, tmp_path / "read")
    assert issue.value["body"] == "Shared weekly context"
    assert issue.brief_comment_id is None


@pytest.mark.parametrize(
    "decisions,limit,stop",
    [
        (["accepted"], 2, "accepted"),
        (["revise", "accepted"], 2, "accepted"),
        (["revise", "revise"], 2, "repair_attempt_limit"),
        (["revise"], 1, "repair_attempt_limit"),
        (["blocked"], 3, "blocked"),
        (["revise", "revise", "accepted"], 3, "accepted"),
    ],
)
def test_bounded_repair_review_feedback_loop_keeps_inputs_and_prior_evidence(
    tmp_path, monkeypatch, decisions, limit, stop
):
    ctx = context(tmp_path) | {"run_id": "one-run", "isolated_validation": True}
    rows = resources()
    calls, snapshots = [], []
    budget = RunBudget({"max_inference_cost_usd": 10})

    def converse(
        config,
        phase,
        context_path,
        output,
        workspace,
        prompt,
        active_budget,
        session=None,
    ):
        current = json.loads(context_path.read_text())
        number = current["repair_attempt"]
        calls.append((phase, number, workspace, id(active_budget)))
        snapshots.append(current)
        active_budget.record(0.1)
        if phase == "repair":
            if number > 1:
                prior = current["review_feedback"]
                assert prior["decision"]["decision"] == "revise"
                assert "Address duplicate publication" in prior["decision"]["feedback"]
                assert (
                    Path(prior["review_report"]).read_text()
                    == "Reviewer evidence and actionable feedback."
                )
                assert Path(prior["report"]).exists()
                assert "original baseline and frozen comparison cohort" in prompt
                assert "same topic, not a new proposal" in prompt
            for row in rows:
                if row["kind"] == "pull":
                    row["head"] = str(number) * 40
                    row["draft"] = True
            (output / "repair.md").write_text("Repair attempt " + str(number))
        else:
            decision = decisions[number - 1]
            (output / "review.md").write_text(
                "Reviewer evidence and actionable feedback."
            )
            write_json(
                output / "review-decision.json",
                {
                    "decision": decision,
                    "feedback": "Address duplicate publication using the cited execution records.",
                },
            )
            if decision == "accepted":
                for row in rows:
                    if row["kind"] == "pull":
                        row["draft"] = False
        return {"status": "finished"}

    monkeypatch.setattr(topic_agent, "run_conversation", converse)
    monkeypatch.setattr(
        topic_agent,
        "discover",
        lambda *a: {"resources": copy.deepcopy(rows), "errors": []},
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    result = topic_agent.run(
        {"max_repair_attempts": limit}, ctx, tmp_path / "topic", budget
    )
    assert [(phase, n) for phase, n, _, _ in calls] == [
        (phase, number)
        for number in range(1, len(decisions) + 1)
        for phase in ("repair", "review")
    ]
    assert len({str(workspace) for _, _, workspace, _ in calls}) == 1
    assert {identifier for _, _, _, identifier in calls} == {id(budget)}
    assert {s["snapshot_path"] for s in snapshots} == {ctx["snapshot_path"]}
    assert {s["cohort_path"] for s in snapshots} == {ctx["cohort_path"]}
    assert {s["run_id"] for s in snapshots} == {"one-run"}
    assert len(result["attempts"]) == len(decisions)
    assert result["stop_reason"] == stop
    assert result["status"] == "reviewed"
    assert budget.cost == pytest.approx(0.2 * len(decisions))
    for attempt in result["attempts"]:
        archived = json.loads(Path(attempt["report"]).read_text())
        assert (
            archived["review_decision"]["decision"] == decisions[attempt["attempt"] - 1]
        )
    assert all(
        row["draft"] == (decisions[-1] != "accepted")
        for row in result["resources"]
        if row["kind"] == "pull"
    )


@pytest.mark.parametrize("during_revision", [False, True])
def test_repair_rounds_share_budget_and_never_reuse_an_old_acceptance(
    tmp_path, monkeypatch, during_revision
):
    ctx = context(tmp_path)
    rows, calls = resources(), []
    budget = RunBudget({"max_inference_cost_usd": 3 if during_revision else 2})

    def converse(config, phase, context_path, output, *args):
        calls.append(phase)
        budget.record(1)
        if phase == "review":
            write_json(
                output / "review-decision.json",
                {"decision": "revise", "feedback": "Fix the failed replay."},
            )
        return {"status": "finished"}

    monkeypatch.setattr(topic_agent, "run_conversation", converse)
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": rows, "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    result = topic_agent.run({}, ctx, tmp_path / "topic", budget)
    assert calls == (
        ["repair", "review", "repair"] if during_revision else ["repair", "review"]
    )
    assert result["status"] == ("review_incomplete" if during_revision else "reviewed")
    assert result["stop_reason"] == (
        "review_incomplete" if during_revision else "inference_cost_threshold"
    )
    assert all(row["draft"] for row in result["resources"] if row["kind"] == "pull")


@pytest.mark.parametrize(
    "decision",
    [
        None,
        {"decision": "revise", "feedback": ""},
        {"decision": "try again", "feedback": "More work"},
    ],
)
def test_missing_or_invalid_review_decision_stops_without_another_repair(
    tmp_path, monkeypatch, decision
):
    calls = []
    rows = resources()
    for row in rows:
        if row["kind"] == "pull":
            row["draft"] = False

    def converse(config, phase, context_path, output, *args):
        calls.append(phase)
        if phase == "review" and decision is not None:
            write_json(output / "review-decision.json", decision)
        return {"status": "finished"}

    monkeypatch.setattr(topic_agent, "run_conversation", converse)
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": rows, "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    monkeypatch.setattr(topic_agent, "command", lambda *a: "")
    result = topic_agent.run({}, context(tmp_path), tmp_path / "topic", RunBudget({}))
    assert calls == ["repair", "review"]
    assert result["status"] == "review_incomplete"
    assert any("structured review decision" in error for error in result["link_errors"])
    assert all(row["draft"] for row in result["resources"] if row["kind"] == "pull")


@pytest.mark.parametrize("attempt_count", [2, 3])
def test_revisions_resume_native_conversation_and_preserve_each_round(
    tmp_path, monkeypatch, attempt_count
):
    from test_coding_agent import install_sdk

    ctx, rows = context(tmp_path), resources()
    calls = []

    def execute(workspace):
        conversation = captured["instance"]
        phase_root = Path(conversation.kwargs["persistence_dir"]).parent
        phase = phase_root.name.removesuffix("-agent")
        output = phase_root.parent
        if phase == "repair" and len(conversation.messages) > 1:
            output = output / "attempts" / f"{len(conversation.messages):03d}"
        current = json.loads((output / "attempt-context.json").read_text())
        number = current["repair_attempt"]
        calls.append((phase, number, workspace))
        callback = conversation.kwargs["callbacks"][0]
        from types import SimpleNamespace

        callback(
            SimpleNamespace(
                model_dump=lambda **kwargs: {"phase": phase, "attempt": number}
            )
        )
        write_json(
            phase_root / "completions" / f"{number:03d}.json",
            {"phase": phase, "attempt": number},
        )
        candidate = workspace / "candidate.txt"
        if phase == "repair":
            if number > 1:
                assert candidate.read_text() == "Candidate " + str(number - 1)
                feedback = current["review_feedback"]
                assert (
                    Path(feedback["review_report"]).read_text()
                    == "Fix the replay's duplicate submission."
                )
                seed = json.loads((output / "resources.json").read_text())
                assert all(r["draft"] for r in seed["resources"] if r["kind"] == "pull")
            for row in rows:
                if row["kind"] == "pull":
                    row.update(head=str(number) * 40, draft=True)
            (output / "repair.md").write_text("Candidate " + str(number))
            candidate.write_text("Candidate " + str(number))
        else:
            assert candidate.read_text() == "Candidate " + str(number)
            write_json(
                output / "review-decision.json",
                {
                    "decision": "revise" if number < attempt_count else "accepted",
                    "feedback": "Fix duplicate submission."
                    if number < attempt_count
                    else "Revised behavior verified.",
                },
            )
            (output / "review.md").write_text("Fix the replay's duplicate submission.")
            for row in rows:
                if row["kind"] == "pull":
                    row["draft"] = False

    captured = install_sdk(monkeypatch, execute)
    monkeypatch.setattr(topic_agent, "version", lambda name: "test-sdk")
    monkeypatch.setattr(
        topic_agent,
        "discover",
        lambda *a: {"resources": copy.deepcopy(rows), "errors": []},
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    monkeypatch.setattr(topic_agent, "command", lambda *a: "")
    budget = RunBudget({"max_inference_cost_usd": 5})
    root = tmp_path / "topic"
    result = topic_agent.run({"max_repair_attempts": attempt_count}, ctx, root, budget)
    assert [(phase, number) for phase, number, _ in calls] == [
        (phase, number)
        for number in range(1, attempt_count + 1)
        for phase in ("repair", "review")
    ]
    assert budget.cost == pytest.approx(0.24 * attempt_count)
    assert result["stop_reason"] == "accepted"
    repair, *reviews = captured["instances"]
    assert len(captured["instances"]) == 1 + attempt_count
    assert len(repair.messages) == attempt_count
    for message in repair.messages[1:]:
        assert "A full independent review" in message
        assert len(message) < len(repair.messages[0]) / 2
    assert len({review.id for review in reviews}) == attempt_count
    assert all(len(review.messages) == 1 for review in reviews)
    assert all(conversation.closed for conversation in captured["instances"])
    assert repair.max_budget_per_run == pytest.approx(5 - 0.12 * (attempt_count - 1))
    assert result["repair"]["conversation_id"] == repair.id
    assert result["review"]["conversation_id"] == reviews[-1].id
    for number in range(1, attempt_count + 1):
        directory = root if number == 1 else root / "attempts" / f"{number:03d}"
        for phase in ("repair", "review"):
            assert (directory / (phase + "-agent") / "prompt.json").exists()
            assert json.loads(
                (directory / (phase + "-agent") / "events.json").read_text()
            ) == [{"phase": phase, "attempt": number}]
            assert (
                json.loads(
                    (
                        directory
                        / (phase + "-agent")
                        / "completions"
                        / f"{number:03d}.json"
                    ).read_text()
                )["attempt"]
                == number
            )
        measurement = json.loads(
            (directory / "repair-agent/measurement.json").read_text()
        )
        assert measurement["conversation_id"] == repair.id
        assert measurement["sdk_estimated_cost_usd"] == pytest.approx(0.12)
        assert (root / "attempts" / f"{number:03d}" / "outcome.json").exists()
    assert result["latest_attempt_dir"] == str(
        root / "attempts" / f"{attempt_count:03d}"
    )


def test_later_attempt_links_are_recovered_after_an_interrupted_worker(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    url = "https://github.com/Example/other/pull/18"
    write_json(tmp_path / "attempts/002/observed-links.json", [url])
    monkeypatch.setattr(topic_agent, "github_pages", lambda *a: [])
    monkeypatch.setattr(
        topic_agent,
        "command",
        lambda *a: json.dumps(
            {
                "body": topic_agent.marker(ctx["topic"]),
                "state": "open",
                "title": "Revised fix",
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40},
                "draft": True,
            }
        ),
    )
    receipt = topic_agent.discover(ctx, tmp_path)
    assert receipt["resources"][0]["url"] == url and not receipt["errors"]


def test_retained_repair_conversation_closes_when_review_bookkeeping_raises(
    tmp_path, monkeypatch
):
    from test_coding_agent import install_sdk

    captured = install_sdk(monkeypatch, lambda workspace: None)
    monkeypatch.setattr(topic_agent, "version", lambda name: "test-sdk")

    def interrupted(*args):
        assert not captured["instances"][0].closed
        raise RuntimeError("Review bookkeeping interrupted")

    monkeypatch.setattr(topic_agent, "finish", interrupted)
    with pytest.raises(RuntimeError, match="bookkeeping interrupted"):
        topic_agent.run({}, context(tmp_path), tmp_path / "topic", RunBudget({}))
    assert len(captured["instances"]) == 1 and captured["instances"][0].closed


def test_resumed_cost_counter_reset_is_unknown_instead_of_negative_spend(
    tmp_path, monkeypatch
):
    from test_coding_agent import install_sdk
    from budget import BudgetExhausted

    def execute(workspace):
        if len(captured["instance"].messages) > 1:
            captured["instance"].cost = 0

    captured = install_sdk(monkeypatch, execute)
    monkeypatch.setattr(topic_agent, "version", lambda name: "test-sdk")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    budget, session = RunBudget({}), {}
    try:
        for number in (1, 2):
            directory = tmp_path / str(number)
            directory.mkdir()
            result = topic_agent.run_conversation(
                {},
                "repair",
                directory / "context.json",
                directory,
                workspace,
                "Continue",
                budget,
                session,
            )
        assert result["sdk_estimated_cost_usd"] is None
        assert budget.cost == pytest.approx(0.12) and budget.unknown == 1
        with pytest.raises(BudgetExhausted, match="inference_cost_unknown"):
            budget.check()
    finally:
        session["conversation"].close()


def test_failed_draft_hold_prevents_revision_dispatch(tmp_path, monkeypatch):
    calls = []
    rows = resources()
    for row in rows:
        if row["kind"] == "pull":
            row["draft"] = False

    def converse(config, phase, context_path, output, *args):
        calls.append(phase)
        if phase == "review":
            write_json(
                output / "review-decision.json",
                {"decision": "revise", "feedback": "Fix duplicate submission."},
            )
        return {"status": "finished"}

    def denied(*args):
        raise ValueError("Draft update denied")

    monkeypatch.setattr(topic_agent, "run_conversation", converse)
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": rows, "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    monkeypatch.setattr(topic_agent, "command", denied)
    result = topic_agent.run({}, context(tmp_path), tmp_path / "topic", RunBudget({}))
    assert calls == ["repair", "review"]
    assert result["status"] == "review_incomplete"
    assert any("Draft update denied" in error["error"] for error in result["errors"])


@pytest.mark.parametrize("repair_status", ["failed", "incomplete"])
def test_unfinished_repair_does_not_dispatch_again_on_revise_feedback(
    tmp_path, monkeypatch, repair_status
):
    calls = []

    def converse(config, phase, context_path, output, *args):
        calls.append(phase)
        if phase == "review":
            write_json(
                output / "review-decision.json",
                {"decision": "revise", "feedback": "Fix the incomplete result."},
            )
        return {"status": repair_status if phase == "repair" else "finished"}

    monkeypatch.setattr(topic_agent, "run_conversation", converse)
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": resources(), "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    result = topic_agent.run({}, context(tmp_path), tmp_path / "topic", RunBudget({}))
    assert calls == ["repair", "review"]
    assert result["stop_reason"] == "repair_incomplete"
    assert len(result["attempts"]) == 1
    assert all(row["draft"] for row in result["resources"] if row["kind"] == "pull")


def test_review_feedback_character_limit_accepts_escaped_unicode(tmp_path, monkeypatch):
    def converse(config, phase, context_path, output, *args):
        if phase == "review":
            (output / "review-decision.json").write_text(
                json.dumps({"decision": "accepted", "feedback": "😀" * 12000})
            )
        return {"status": "finished"}

    monkeypatch.setattr(topic_agent, "run_conversation", converse)
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": resources(), "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    result = topic_agent.run({}, context(tmp_path), tmp_path / "topic", RunBudget({}))
    assert result["stop_reason"] == "accepted"
    assert result["status"] == "reviewed"
