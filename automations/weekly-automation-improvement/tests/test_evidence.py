import hashlib
import json
import sys

import pytest

from test_automation import TARBALL

sys.path.insert(0, str(TARBALL))
import evidence


def request(tmp_path, **overrides):
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import hashlib,json,pathlib,sys,os\n"
        "assert 'GH_TOKEN' not in os.environ\n"
        "assert 'LMNR_PROJECT_API_KEY' not in os.environ\n"
        "p=pathlib.Path(sys.argv[2]); r=json.loads(p.read_text())\n"
        "o=pathlib.Path(sys.argv[4]); o.mkdir()\n"
        "v={k:r[k] for k in ('baseline','candidate')}\n"
        "v.update(status='improvement_on_development_cases', markdown='Observed comparison', request_sha256=hashlib.sha256(p.read_bytes()).hexdigest())\n"
        "(o/'evidence.json').write_text(json.dumps(v))\n"
    )
    value = {
        "repository": "example/repo",
        "baseline": "a" * 40,
        "candidate": "b" * 40,
        "pr_url": "https://github.com/example/repo/pull/1",
        "plan": {"command": [sys.executable, str(runner)]},
    } | overrides
    path = tmp_path / "comparison-request.json"
    path.write_text(json.dumps(value))
    return path, value


def github(request, edits, *, head=None, state="open"):
    def command(argv):
        if argv[:3] == ["gh", "api", "repos/example/repo/pulls/1"]:
            return json.dumps(
                {
                    "head": {"sha": head or request["candidate"]},
                    "body": "Maintainer notes\n"
                    + evidence.section("Pending")
                    + "\nTail",
                    "state": state,
                }
            )
        assert argv[:4] == ["gh", "api", "--method", "PATCH"]
        edits.append(argv)
        return ""

    return command


def test_real_runner_then_pr_update_and_resume_without_new_execution(
    tmp_path, monkeypatch
):
    path, value = request(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "private-token")
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "private-laminar-key")
    edits = []
    result = evidence.run(path, github(value, edits))
    assert result["published"]
    assert result["status"] == "improvement_on_development_cases"
    body = (tmp_path / "pr-body-with-evidence.md").read_text()
    payload = json.loads((tmp_path / "pr-evidence-update.json").read_text())
    assert payload == {"body": body}
    assert "Maintainer notes" in body and body.endswith("\nTail")
    assert "Before / After Evidence" in body and "Observed comparison" in body
    monkeypatch.setattr(
        evidence, "execute", lambda *a: pytest.fail("Do not rerun inference")
    )
    assert evidence.run(path, github(value, edits))["published"]
    assert len(edits) == 2


@pytest.mark.parametrize("state,head", [("closed", None), ("open", "c" * 40)])
def test_changed_or_closed_pr_never_receives_stale_evidence(tmp_path, state, head):
    path, value = request(tmp_path)
    edits = []
    result = evidence.run(path, github(value, edits, state=state, head=head))
    assert result["status"] == "stale_pr"
    assert edits == []


def test_comparison_cannot_update_another_repository(tmp_path, monkeypatch):
    path, value = request(tmp_path, repository="other/repo")
    monkeypatch.setattr(evidence, "execute", lambda *a: pytest.fail("Do not execute"))
    with pytest.raises(ValueError, match="source repository"):
        evidence.run(path, github(value, []))


@pytest.mark.parametrize("tamper", ["candidate", "request_sha256", "status"])
def test_mismatched_receipt_publishes_inconclusive_not_improvement(tmp_path, tamper):
    path, value = request(tmp_path)
    output = tmp_path / "comparison"
    output.mkdir()
    receipt = value | {
        "status": "improvement_on_development_cases",
        "markdown": "Do not publish this unsupported claim",
        "request_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    receipt[tamper] = "invalid"
    (output / "evidence.json").write_text(json.dumps(receipt))
    assert evidence.run(path, github(value, []))["status"] == "inconclusive"
    body = (tmp_path / "pr-body-with-evidence.md").read_text()
    assert "unsupported claim" not in body
    assert "Inconclusive" in body


def test_failed_comparison_is_retained_without_reroll(tmp_path):
    path, value = request(tmp_path)
    value["plan"]["command"] = [sys.executable, "-c", "raise SystemExit(1)"]
    path.write_text(json.dumps(value))
    assert evidence.run(path, github(value, []))["status"] == "inconclusive"
    first = (tmp_path / "comparison-failure.json").read_bytes()
    assert evidence.run(path, github(value, []))["status"] == "inconclusive"
    assert (tmp_path / "comparison-failure.json").read_bytes() == first


def test_inconclusive_comparison_replaces_only_owned_unevaluated_notice(tmp_path):
    path, value = request(tmp_path)
    output = tmp_path / "comparison"
    output.mkdir()
    (output / "evidence.json").write_text(
        json.dumps(
            value
            | {
                "status": "inconclusive",
                "markdown": "Target behavior was not reproduced.",
                "request_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    )
    notice = "**UNEVALUATED PROPOSAL:** behavioral evaluation has not been performed. The checks below only establish that the patch applies within the approved source scope."
    payloads = []

    def command(argv):
        if argv[:3] == ["gh", "api", "repos/example/repo/pulls/1"]:
            return json.dumps(
                {
                    "head": {"sha": value["candidate"]},
                    "body": "Maintainer notes\n\n" + notice,
                    "title": "[Unevaluated] Repair",
                    "state": "open",
                }
            )
        payloads.append(json.loads((tmp_path / "pr-evidence-update.json").read_text()))
        return ""

    evidence.run(path, command)
    assert payloads[0]["title"] == "[Inconclusive] Repair"
    assert "Maintainer notes" in payloads[0]["body"]
    assert notice not in payloads[0]["body"]
    assert "Improvement has not been demonstrated" in payloads[0]["body"]


def test_runner_timeout_interrupts_and_reaps_the_process(tmp_path):
    with pytest.raises(ValueError, match="timed out"):
        evidence.execute(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            tmp_path / "timeout.log",
            0.1,
        )


def test_only_comparison_section_is_replaced_and_duplicates_are_rejected():
    body = "before\n" + evidence.section("old") + "\nafter"
    assert (
        evidence.replace_section(body, "new")
        == "before\n" + evidence.section("new") + "\nafter"
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        evidence.replace_section(body + body, "new")


def test_hook_freezes_published_revisions_and_does_not_run_when_unconfigured(
    tmp_path, monkeypatch
):
    assert (
        evidence.after_publication({}, {}, {}, {}, tmp_path, None)["status"]
        == "not_configured"
    )
    calls = []
    monkeypatch.setattr(
        evidence,
        "run",
        lambda path, command: calls.append(json.loads(path.read_text()))
        or {"status": "inconclusive"},
    )
    result = evidence.after_publication(
        {"checkout": "/repo", "comparative_validation": {"command": [sys.executable]}},
        {"repository": "example/repo", "revision": "a" * 40},
        {"topic": {"key": "wrong-head"}},
        {
            "pr_url": "https://github.com/example/repo/pull/1",
            "candidate_revision": "b" * 40,
            "changed_files": ["x.py"],
        },
        tmp_path,
        None,
    )
    assert result["status"] == "inconclusive"
    assert calls[0]["baseline"] == "a" * 40
    assert calls[0]["candidate"] == "b" * 40
    assert calls[0]["topic"] == {"key": "wrong-head"}


@pytest.mark.parametrize("cohort", [None, {"sample_size": 2}])
def test_configured_n_requires_matching_prepublication_cohort(
    tmp_path, monkeypatch, cohort
):
    monkeypatch.setattr(
        evidence,
        "evaluate",
        lambda *a: pytest.fail("Missing cohort must block inference"),
    )
    result = evidence.before_publication(
        {
            "comparative_validation": {"command": ["runner"]},
            "validation_sample_size": 7,
        },
        {},
        {"validation_cohort": cohort},
        {},
        {},
        [],
        tmp_path,
    )
    assert result["status"] == "inconclusive"


def test_prepublication_forwards_the_frozen_user_sized_cohort(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(
        evidence,
        "evaluate",
        lambda path: captured.append(json.loads(path.read_text()))
        or {"status": "inconclusive"},
    )
    cohort = {"path": "/private/cohort.json", "sha256": "fixed", "sample_size": 7}
    evidence.before_publication(
        {
            "checkout": "/repo",
            "comparative_validation": {"command": ["runner"]},
            "validation_sample_size": 7,
        },
        {"repository": "example/repo", "revision": "a" * 40},
        {"validation_cohort": cohort},
        {"candidate_revision": "b" * 40, "changed_files": ["worker.py"]},
        {"hypothesis": "Targeted fix"},
        [],
        tmp_path,
    )
    assert captured[0]["cohort"] == cohort


def test_stronger_inconclusive_comparison_downgrades_old_evidence_title(tmp_path):
    path, value = request(tmp_path)
    output = tmp_path / "comparison"
    output.mkdir()
    (output / "evidence.json").write_text(
        json.dumps(
            value
            | {
                "status": "inconclusive",
                "markdown": "Target not reproduced",
                "request_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    )
    payloads = []

    def command(argv):
        if argv[:3] == ["gh", "api", "repos/example/repo/pulls/1"]:
            return json.dumps(
                {
                    "head": {"sha": value["candidate"]},
                    "body": "**Development comparison recorded:** see Before / After Evidence for executed checks, exact source revisions and limitations.",
                    "title": "[Evidence] Repair",
                    "state": "open",
                }
            )
        payloads.append(json.loads((tmp_path / "pr-evidence-update.json").read_text()))
        return ""

    evidence.run(path, command)
    assert payloads[0]["title"] == "[Inconclusive] Repair"
    assert "Improvement has not been demonstrated" in payloads[0]["body"]
