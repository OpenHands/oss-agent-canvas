import copy
import fcntl
import io
import json
import tarfile
import uuid
from pathlib import Path

import pytest

from test_automation import arguments, command, fix_report, load_source, PATCH
from test_automation import source as source_fixture
from test_flow1 import EVENT, SIGNAL, frozen
import patches
import sources
import weekly
import triage
from events import collect_events
from flow1 import findings


class EventClient:
    def __init__(self, tmp_path, rows, owners=1):
        self.args = arguments(tmp_path, signal_id=SIGNAL)
        self.value = frozen()
        self.rows = rows
        self.owners = owners
        self.measurements = []

    def request(self, url):
        return (
            self.value["flow1_signal"]
            if "/signals/" in url
            else self.value["flow1_profile"]
        )

    base = "https://example.test"

    def query(self, sql):
        if "FROM signal_runs" in sql:
            return self.rows
        case = self.value["samples"][0]
        return [
            {
                key: case[key]
                for key in (
                    "conversation_id",
                    "automation_id",
                    "run_id",
                    "automation_name",
                )
            }
        ] * self.owners

    def load_sample(self, case):
        case.update(copy.deepcopy(self.value["samples"][0]))

    def load_flow1(self, case, definition):
        pass


def test_events_select_findings_and_report_failed_and_clean_runs(tmp_path):
    run = frozen()["samples"][0]["flow1_runs"][0]
    clean = run | {
        "run_id": str(uuid.UUID(int=90)),
        "trace_id": str(uuid.UUID(int=91)),
        "event_id": str(uuid.UUID(int=0)),
    }
    failed = clean | {
        "run_id": str(uuid.UUID(int=92)),
        "trace_id": str(uuid.UUID(int=93)),
        "status": "FAILED",
    }
    client = EventClient(tmp_path, [run, clean, failed])
    value = collect_events(client, set())
    assert value["selection"]["selected_event_ids"] == [EVENT]
    assert value["selection"]["unit"] == "unhandled_signal_event"
    assert value["signal_run_summary"]["completed_no_event"] == 1
    assert value["signal_run_summary"]["statuses"]["FAILED"] == 1


def test_handled_or_superseded_events_are_not_selected(tmp_path):
    run = frozen()["samples"][0]["flow1_runs"][0]
    newer_failure = run | {
        "run_id": str(uuid.UUID(int=90)),
        "status": "FAILED",
        "updated_at": "2026-10-23T00:00:00Z",
    }
    value = collect_events(EventClient(tmp_path, [run, newer_failure]), set())
    assert value["samples"] == []
    second = tmp_path / "second"
    second.mkdir()
    value = collect_events(EventClient(second, [run]), {EVENT})
    assert value["selection"]["population_size"] == 0


def test_ambiguous_ownership_is_recorded_without_replacement(tmp_path):
    run = frozen()["samples"][0]["flow1_runs"][0]
    value = collect_events(EventClient(tmp_path, [run], owners=2), set())
    assert value["selection"]["selected_event_ids"] == [EVENT]
    assert value["samples"] == []
    assert value["collection_errors"][0]["event_id"] == EVENT


def test_selected_event_does_not_require_analysis_of_unselected_context_trace():
    value = frozen()
    case = value["samples"][0]
    original = case["trace_ids"][0]
    other = str(uuid.UUID(int=80))
    case["flow1_required_trace_ids"] = [original]
    case["selected_event_ids"] = [EVENT]
    case["trace_ids"].append(other)
    case["spans"].append(
        case["spans"][0]
        | {
            "ref": "s2",
            "span_id": str(uuid.UUID(int=81)),
            "trace_id": other,
            "end_time": "2026-10-24T00:00:00Z",
        }
    )
    case["flow1_trace_sessions"].append(
        {"trace_id": other, "conversation_ids": [case["conversation_id"]]}
    )
    result, _ = findings(value, case)
    assert result["finding_event_ids"] == [EVENT]


@pytest.fixture
def configured_source(tmp_path):
    return source_fixture.__wrapped__(tmp_path)


@pytest.mark.parametrize("valid_syntax", [True, False])
def test_unevaluated_draft_publishes_without_behavioral_checks(
    configured_source, tmp_path, monkeypatch, valid_syntax
):
    checkout, path, config = configured_source
    config["checks"] = []
    identifier = str(uuid.UUID(int=1))
    config["automation_ids"] = [identifier]
    path.write_text(json.dumps(config))
    command(
        ["git", "remote", "add", "origin", "https://github.com/Example/automation.git"],
        checkout,
    )
    args = arguments(tmp_path / "review", source_config=path, publish_draft=True)
    args.output.mkdir()
    args.publication_automation_id, args.publication_week = identifier, "2026-W44"
    args.publication_topic = {
        "id": triage.topic_id(identifier, "payload-validation"),
        "key": "payload-validation",
        "title": "Validate review payloads",
        "representative_event_ids": [EVENT],
    }
    report = fix_report(
        PATCH if valid_syntax else PATCH.replace("+VALUE = 4", "+VALUE = (")
    )
    report["findings"][0].update(
        trace_urls=["https://example.test/trace"],
        signal_event_id=EVENT,
        automation_id=identifier,
        case_id="c1",
    )
    original = patches.command
    pushed = []
    comparisons = []

    def compare(config, source, snapshot, results, output, command, budget=None):
        assert results["pr_url"] == "https://github.com/Example/automation/pull/1"
        assert len(results["candidate_revision"]) == 40
        assert len(pushed) == 1
        comparisons.append(results["candidate_revision"])
        return {"status": "inconclusive", "published": True}

    def run(argv, cwd=None, **kwargs):
        if argv[:2] == ["gh", "api"]:
            return config["revision"]
        if argv[:3] == ["gh", "pr", "list"]:
            if (
                "--base" not in argv
                or argv[argv.index("--base") + 1] != config["base_branch"]
            ):
                return (
                    '[{"url":"https://github.com/Example/automation/pull/wrong-base"}]'
                )
            return "[]"
        if argv[:2] == ["git", "ls-remote"]:
            return ""
        if argv[:2] == ["git", "push"]:
            pushed.append(argv)
            return ""
        if argv[:3] == ["gh", "pr", "create"]:
            assert "--draft" in argv
            assert argv[argv.index("--label") + 1] == "automation-improvement"
            assert "[Unevaluated]" in argv[argv.index("--title") + 1]
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            assert "behavioral evaluation has not been performed" in body
            assert "automation-improvement-event: " + EVENT in body
            assert "automation-improvement-automation: " + identifier in body[:500]
            assert "automation-improvement-week: 2026-W44" in body[:500]
            return "https://github.com/Example/automation/pull/1"
        return original(argv, cwd, **kwargs)

    monkeypatch.setattr(patches, "command", run)
    monkeypatch.setattr(patches, "after_publication", compare)
    monkeypatch.setattr(
        patches, "run_checks", lambda *a: pytest.fail("Behavioral checks are deferred")
    )
    if not valid_syntax:
        with pytest.raises(ValueError, match="invalid Python syntax"):
            patches.propose_fix(
                args,
                report,
                {"source": load_source(path), "topic": args.publication_topic},
            )
        assert pushed == []
        assert comparisons == []
        return
    result = patches.propose_fix(
        args, report, {"source": load_source(path), "topic": args.publication_topic}
    )
    assert result["status"] == "unevaluated_draft"
    assert result["baseline"] == result["candidate"] == []
    assert comparisons == [result["candidate_revision"]]
    assert result["comparison"]["status"] == "inconclusive"
    assert len(pushed) == 1
    assert pushed[0][-1].startswith("HEAD:refs/heads/automation/improve-")
    assert (checkout / "value.py").read_text() == "VALUE = 3\n"
    assert not (args.output / "baseline").exists()


def bundle(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            data = content.encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def test_source_matches_deployed_code_and_rejects_ambiguous_copies(
    tmp_path, monkeypatch
):
    command(["git", "init", "-q"], tmp_path)
    command(["git", "config", "user.name", "Test"], tmp_path)
    command(["git", "config", "user.email", "test@example.test"], tmp_path)
    directory = tmp_path / "automations/one/tarball"
    directory.mkdir(parents=True)
    (directory / "main.py").write_text("print(42)\n")
    command(["git", "add", "."], tmp_path)
    command(["git", "commit", "-qm", "source"], tmp_path)
    raw = bundle({"main.py": "print(42)\n"})
    monkeypatch.setattr(
        sources,
        "native_request",
        lambda base, path: raw
        if path.endswith("/tarball")
        else json.dumps(
            {"entrypoint": "python3 main.py", "name": "Renamed automation"}
        ).encode(),
    )
    config = {"automation_api_url": "http://127.0.0.1"}
    sample = {"automation_id": str(uuid.UUID(int=1))}
    result = sources.resolve_source(
        config, sample, sources.repository_index(tmp_path, "HEAD")
    )
    assert result["directory"] == "automations/one"
    other = tmp_path / "automations/two/tarball"
    other.mkdir(parents=True)
    (other / "main.py").write_text("print(42)\n")
    command(["git", "add", "."], tmp_path)
    command(["git", "commit", "-qm", "ambiguous"], tmp_path)
    with pytest.raises(ValueError, match="unique approved source"):
        sources.resolve_source(
            config, sample, sources.repository_index(tmp_path, "HEAD")
        )


def configuration(tmp_path):
    return {
        "collect_validation_inputs": False,
        "state_dir": str(tmp_path / "state"),
        "repository": "Example/automation",
        "laminar_base_url": "https://example.test",
        "llm_base_url": "https://proxy.example.test",
        "project_id": str(uuid.UUID(int=1)),
        "signal_id": SIGNAL,
    }


def record(number=1, automation=1, conversation=None):
    value = frozen()
    case = value["samples"][0]
    event = copy.deepcopy(case["flow1_events"][0])
    event["id"] = str(uuid.UUID(int=100 + number))
    event["trace_id"] = str(uuid.UUID(int=200 + number))
    event["run_id"] = str(uuid.UUID(int=300 + number))
    event["severity"] = 1
    event["leaf_clusters"] = []
    payload = json.loads(event["payload"])
    payload["signature"] = "same problem"
    event["payload"] = json.dumps(payload)
    owner = {
        "automation_id": str(uuid.UUID(int=400 + automation)),
        "automation_name": "Automation " + str(automation),
        "conversation_id": str(uuid.UUID(int=500 + (conversation or number))),
        "run_id": str(uuid.UUID(int=600 + number)),
        "trace_id": event["trace_id"],
    }
    run = copy.deepcopy(case["flow1_runs"][0]) | {
        "event_id": event["id"],
        "trace_id": event["trace_id"],
        "run_id": event["run_id"],
    }
    return {
        "event": event,
        "owner": owner,
        "run": run,
        "definition": value["flow1_signal"],
    }


def inventory(records):
    value = frozen()
    value.update(
        events=copy.deepcopy(records),
        samples=[],
        latest_runs=[item["run"] for item in records],
        signal_run_summary={"attempts": len(records)},
        collection_errors=[],
    )
    value["selection"]["selected_event_ids"] = [item["event"]["id"] for item in records]
    return value


def prepare_grouped(configured_source, tmp_path, monkeypatch, records):
    import topic_issue
    from test_topic_issue import GitHub

    monkeypatch.setattr(topic_issue, "command", GitHub())
    _, _, source = configured_source
    config = configuration(tmp_path) | {
        "publish_drafts": True,
        "repair_concurrency": 1,
        "checkout": source["checkout"],
    }
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test")
    monkeypatch.setattr(weekly, "github_auth", lambda c: None)
    monkeypatch.setattr(weekly, "existing_prs", lambda r, base="main": [])
    monkeypatch.setattr(weekly, "command", lambda *a: "remote-branch-exists")
    monkeypatch.setattr(weekly, "collect_events", lambda *a, **k: inventory(records))
    calls = []

    def rank(args, pending, prs, completed_topics=()):
        calls.append(("luna", "global"))
        grouped = {}
        for row in pending:
            grouped.setdefault(row["owner"]["automation_id"], []).append(row)
        plans, all_cards = [], []
        for identifier, rows in sorted(grouped.items()):
            cards, _ = triage.group_findings(rows)
            done = {
                t["id"]
                for t in completed_topics
                if t.get("automation_id", identifier) == identifier
                and t.get("status", "completed")
                not in (
                    "failed",
                    "deferred_validation",
                    "waiting_for_new_evidence",
                    "unrepaired",
                    "deferred_budget",
                    "retryable",
                )
            }
            for card in cards:
                all_cards.append(card | {"automation_id": identifier})
                key = "issue-" + card["group_id"]
                topic_id = triage.topic_id(identifier, key)
                if topic_id not in done:
                    plans.append(
                        {
                            "id": topic_id,
                            "automation_id": identifier,
                            "priority": 2,
                            "key": key,
                            "title": "One issue",
                            "reason": "Investigate one failure",
                            "group_ids": [card["group_id"]],
                            "representative_event_ids": [
                                card["examples"][0]["event_id"]
                            ],
                        }
                    )
        return plans, {
            "total_events": len(pending),
            "affected_conversations": len(pending),
            "groups": all_cards,
        }

    def hydrate(client, frame, chosen):
        value = frozen()
        value["samples"][0].update(chosen[0]["owner"])
        value["samples"][0]["selected_event_ids"] = [chosen[0]["event"]["id"]]
        return value

    def resolve(config, snapshot, output):
        identifier = snapshot["samples"][0]["automation_id"]
        path = output / "source-config.json"
        path.write_text(
            json.dumps(source | {"automation_ids": [identifier], "checks": []})
        )
        return path, snapshot["samples"], []

    def analyze(args):
        value = json.loads(args.input.read_text())
        case = value["samples"][0]
        assert value["aggregation"]["luna_triage"]["topics"] == [value["topic"]]
        assert set(value["aggregation"]["groups"][0])
        assert set(case["selected_event_ids"]) <= set(
            value["topic"]["representative_event_ids"]
        )
        calls.append(("sol", case["automation_id"], value["topic"]["key"]))
        assert args.coordinator == "openai/gpt-6.1-sol" and not args.publish_draft
        args.output.mkdir()
        report = fix_report(PATCH)
        report["findings"][0].update(
            signal_event_id=case["selected_event_ids"][0],
            automation_id=case["automation_id"],
        )
        report.update(rejected_findings=[], measurements=[], errors=[])
        (args.output / "report.json").write_text(json.dumps(report))

    def publish(args, report, snapshot):
        identifier = args.publication_automation_id
        assert args.publish_draft and not args.publish_fix
        assert snapshot["source"]["automation_ids"] == [identifier]
        calls.append(("publish", identifier, args.publication_topic["key"]))
        return {
            "status": "unevaluated_draft",
            "pr_url": "https://github.com/Example/automation/pull/"
            + str(int(args.publication_topic["id"], 16)),
        }

    monkeypatch.setattr(weekly, "triage", rank)
    monkeypatch.setattr(weekly, "hydrate_events", hydrate)
    monkeypatch.setattr(weekly, "source_config", resolve)
    monkeypatch.setattr(weekly.analysis, "run", analyze)
    monkeypatch.setattr(weekly, "propose_fix", publish)
    return config, calls


def test_two_automations_get_independent_repairs_and_pending_findings_survive(
    configured_source, tmp_path, monkeypatch
):
    records = [record(1), record(2), record(3, automation=2)]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    result = weekly.run(config)
    assert result["status"] == "completed" and len(result["automations"]) == 2
    assert all(
        item["topics"][0]["publication"]["status"] == "unevaluated_draft"
        for item in result["automations"]
    )
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert len(state["topic_weeks"]) == 2
    assert set(state["backlog"]) == {records[1]["event"]["id"]}
    assert len([call for call in calls if call[0] == "sol"]) == 2
    again = weekly.run(config)
    assert len([call for call in calls if call[0] == "sol"]) == 2
    assert again["automations"][0]["status"] == "no_promising_findings"


def test_uncertain_publication_blocks_only_its_automation(
    configured_source, tmp_path, monkeypatch
):
    records = [record(1), record(2, automation=2)]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    original = weekly.propose_fix

    def publish(args, report, snapshot):
        if args.publication_automation_id == records[0]["owner"]["automation_id"]:
            args.reserve_publication("automation/improve-" + "0" * 20)
            raise RuntimeError("connection lost during publication")
        return original(args, report, snapshot)

    monkeypatch.setattr(weekly, "propose_fix", publish)
    result = weekly.run(config)
    assert result["status"] == "partial_failure"
    assert result["automations"][0]["topics"][0]["status"] == "failed"
    assert (
        result["automations"][1]["topics"][0]["publication"]["status"]
        == "unevaluated_draft"
    )
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert sorted(item["status"] for item in state["topic_weeks"].values()) == [
        "completed",
        "reserved",
    ]
    again = weekly.run(config)
    assert again["status"] == "validation_incomplete"
    assert again["pending_publications"]
    assert len([call for call in calls if call[0] == "sol"]) == 2


def test_dry_run_does_not_publish_or_change_backlog(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_grouped(
        configured_source, tmp_path, monkeypatch, [record()]
    )

    def check(args, report, snapshot):
        assert args.check_draft and not args.publish_draft and not args.publish_fix
        calls.append(("check", snapshot["samples"][0]["automation_id"]))
        return {"status": "mechanical_checks_passed"}

    monkeypatch.setattr(weekly, "propose_fix", check)
    result = weekly.run(config, dry_run=True)
    assert result["automations"][0]["topics"][0]["publication"] == "disabled"
    assert not (Path(config["state_dir"]) / "state.json").exists()
    assert [call[0] for call in calls] == ["luna", "sol", "check"]


@pytest.mark.parametrize("collection_gap", [False, True])
def test_unproven_repair_stays_pending_without_a_publication_reservation(
    configured_source, tmp_path, monkeypatch, collection_gap
):
    records = [record()]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    if collection_gap:
        collected = inventory(records)
        collected["collection_errors"] = [{"error": "A different trace is missing"}]
        monkeypatch.setattr(weekly, "collect_events", lambda *a, **k: collected)
    monkeypatch.setattr(
        weekly,
        "propose_fix",
        lambda *args: {
            "status": "improvement_not_demonstrated",
            "comparison": {"status": "inconclusive"},
        },
    )
    result = weekly.run(config)
    assert result["status"] == "validation_incomplete"
    assert result["automations"][0]["status"] == "validation_incomplete"
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert records[0]["event"]["id"] in state["backlog"]
    assert {row["status"] for row in state["topic_weeks"].values()} == {"retryable"}
    assert not state["events"]


@pytest.mark.parametrize(
    "provider_error,expected", [(None, True), ("Provider unavailable", False)]
)
def test_repair_feedback_requires_a_completed_behavioral_failure(
    tmp_path, provider_error, expected
):
    path = tmp_path / "comparison/runs/comparison.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "case_id": "reported_harm",
                        "variant": "candidate",
                        "score": {
                            "passed": False,
                            "finished": provider_error is None,
                            "error": provider_error,
                            "violations": ["incorrect_priority"],
                            "final_priority": "priority:low",
                        },
                    }
                ]
            }
        )
    )
    feedback = weekly.comparison_feedback(tmp_path)
    assert bool(feedback) == expected
    if expected:
        assert feedback["failed_cases"][0]["final_priority"] == "priority:low"


@pytest.mark.parametrize("revised_passes", [False, True])
@pytest.mark.parametrize("semantic_failure", [False, True])
def test_one_repair_revision_keeps_failed_evidence_and_only_consumes_proven_findings(
    configured_source, tmp_path, monkeypatch, revised_passes, semantic_failure
):
    records = [record()]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    config["revise_failed_repair"] = True
    original = weekly.analysis.run
    attempts = []

    def analyze(args):
        original(args)
        if args.output.name == "revision":
            snapshot = json.loads(args.input.read_text())
            previous = Path(snapshot["repair_feedback"]["previous_attempt"])
            assert previous.exists()
            assert snapshot["repair_feedback"]["previous_patch"] == PATCH
            central = snapshot["repair_issue"]
            assert central["url"].startswith(
                "https://github.com/Example/automation/issues/"
            )
            catalog = args.evidence_reader.execute(
                "read_issue_comment",
                {"comment_id": central["records"][-1]["id"], "offset": 0},
            )["content"]
            reference = next(
                line.split("`")[1]
                for line in catalog.splitlines()
                if "reported_harm-1-candidate/events.json" in line
            )
            replay = args.evidence_reader.execute(
                "read_issue_artifact", {"reference": reference, "offset": 0}
            )
            assert "Actual rejected write from the failed attempt" in replay["content"]
            failure = snapshot["repair_feedback"]["failed_cases"][0]
            if semantic_failure:
                assert failure["assessment"]["reason"] == "Priority ignores real loss"
                assert snapshot["repair_feedback"]["unresolved_pairs"] == [
                    {"case_id": "control", "repeat": 1}
                ]
            else:
                assert failure["violations"] == ["incorrect_priority"]
            path = args.output / "report.json"
            value = json.loads(path.read_text())
            value["proposals"][0]["patch"] += "\n"
            path.write_text(json.dumps(value))

    def publish(args, report, snapshot):
        attempts.append(args.output)
        if len(attempts) == 2 and revised_passes:
            return {
                "status": "evidence_supported_draft",
                "pr_url": "https://github.com/Example/automation/pull/1",
                "comparison": {"status": "improvement_on_development_cases"},
            }
        path = args.output / "comparison/runs/comparison.json"
        path.parent.mkdir(parents=True)
        replay = path.parent / "reported_harm-1-candidate"
        replay.mkdir()
        (replay / "events.json").write_text(
            json.dumps([{"result": "Actual rejected write from the failed attempt"}])
        )
        path.write_text(
            json.dumps(
                {
                    "runs": [
                        {
                            "case_id": "reported_harm",
                            "variant": "candidate",
                            "score": {
                                "passed": False,
                                "finished": True,
                                "error": None,
                                "violations": ["incorrect_priority"],
                                "final_priority": "priority:low",
                            },
                        }
                    ]
                }
            )
        )
        if semantic_failure:
            comparison = json.loads(path.read_text())
            comparison["runs"][0]["score"].update(passed=True, violations=[])
            comparison["runs"].append(
                {
                    "case_id": "control",
                    "variant": "candidate",
                    "score": {
                        "finished": False,
                        "error": "Step limit",
                        "passed": False,
                    },
                }
            )
            comparison["cases"] = [
                {"case_id": "reported_harm", "kind": "regression"},
                {"case_id": "control", "kind": "control"},
            ]
            comparison["assessment"] = {
                "invalid_pairs": [{"case_id": "control", "repeat": 1}],
                "observations": [
                    {
                        "case_id": "reported_harm",
                        "repeat": 1,
                        "valid": True,
                        "baseline_conformance": True,
                        "candidate_conformance": True,
                        "new_violations": [],
                        "semantic": {
                            "baseline_success": False,
                            "candidate_success": False,
                            "regression": False,
                            "reason": "Priority ignores real loss",
                        },
                    }
                ],
            }
            path.write_text(json.dumps(comparison))
        return {
            "status": "improvement_not_demonstrated",
            "comparison": {"status": "inconclusive"},
        }

    monkeypatch.setattr(weekly.analysis, "run", analyze)
    monkeypatch.setattr(weekly, "propose_fix", publish)
    result = weekly.run(config)
    assert len(attempts) == 2
    assert attempts[0].name == "analysis" and attempts[1].name == "revision"
    assert (attempts[0] / "comparison/runs/comparison.json").exists()
    assert len([call for call in calls if call[0] == "sol"]) == 2
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    if revised_passes:
        assert result["status"] == "completed"
        assert state["events"][records[0]["event"]["id"]]["status"] == "draft_pr"
        assert not state["backlog"]
        assert {row["status"] for row in state["topic_weeks"].values()} == {"completed"}
    else:
        assert result["status"] == "validation_incomplete"
        assert records[0]["event"]["id"] in state["backlog"]
        assert not state["events"]
        assert {row["status"] for row in state["topic_weeks"].values()} == {"retryable"}


def test_dry_run_cannot_backfill_or_publish_existing_pr_evidence(
    configured_source, tmp_path, monkeypatch
):
    import validate_repairs

    config, calls = prepare_grouped(
        configured_source, tmp_path, monkeypatch, [record()]
    )
    config["validate_existing_repairs"] = True
    monkeypatch.setattr(
        validate_repairs,
        "validate",
        lambda *args: pytest.fail("Dry run must not update PRs"),
    )
    monkeypatch.setattr(
        weekly, "propose_fix", lambda *args: {"status": "mechanical_checks_passed"}
    )
    weekly.run(config, dry_run=True)


def test_old_automation_marker_does_not_block_new_topics(
    configured_source, tmp_path, monkeypatch
):
    records = [record(1), record(2, automation=2)]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    monkeypatch.setattr(
        weekly,
        "existing_prs",
        lambda r, base="main": [
            {
                "body": "<!-- automation-improvement-automation: "
                + records[0]["owner"]["automation_id"]
                + " -->",
                "createdAt": weekly.dt.datetime.now(weekly.dt.UTC).isoformat(),
            }
        ],
    )
    result = weekly.run(config)
    assert result["status"] == "completed"
    assert len([call for call in calls if call[0] == "publish"]) == 2


def test_backlog_survives_window_and_sampling_only_version_changes():
    old = record()
    old["definition"].update(prompt="same instruction", sampleRate=5)
    state = {"events": {}, "backlog": {old["event"]["id"]: old}}
    current = inventory([])
    current["flow1_signal"].update(prompt="same instruction", sampleRate=50, version=2)
    assert weekly.update_backlog(state, current, []) == [old]
    current["flow1_signal"]["prompt"] = "a different detector"
    assert weekly.update_backlog(state, current, []) == []
    assert len(state["backlog"]) == 1


def test_handled_and_superseded_findings_leave_backlog():
    old = record()
    identifier = old["event"]["id"]
    state = {
        "events": {identifier: {"status": "rejected"}},
        "backlog": {identifier: old},
    }
    assert weekly.update_backlog(state, inventory([old]), []) == []
    state["events"] = {}
    state["backlog"][identifier] = old
    current = inventory([])
    current["latest_runs"] = [old["run"] | {"event_id": str(uuid.UUID(int=0))}]
    assert weekly.update_backlog(state, current, []) == []
    assert not state["backlog"]


def test_no_events_skips_models(configured_source, tmp_path, monkeypatch):
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, [])
    assert weekly.run(config)["status"] == "no_unhandled_findings"
    assert calls == []


def test_concurrent_weekly_runner_does_not_enter_pipeline(tmp_path):
    config = configuration(tmp_path)
    directory = Path(config["state_dir"])
    directory.mkdir()
    with (directory / "weekly.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert weekly.run(config) == {"status": "skipped_overlap"}


def test_plugin_source_uses_declared_scope_without_tarball(tmp_path, monkeypatch):
    plugin = {
        "source": "github:Example/automation",
        "ref": "main",
        "repo_path": "plugins/reviewer",
    }
    metadata = {
        "name": "Reviewer",
        "timeout": 1200,
        "trigger": {"type": "event"},
        "plugin_spec": {
            "plugin": plugin,
            "config": {"review_tone": "concise", "api_key": "private"},
        },
    }
    calls = []

    def request(base, path):
        calls.append(path)
        assert not path.endswith("/tarball")
        return json.dumps(metadata).encode()

    monkeypatch.setattr(sources, "native_request", request)
    index = {
        "plugins/reviewer": {
            "scripts/worker.py": ("plugins/reviewer/scripts/worker.py", "hash")
        }
    }
    config = {
        "automation_api_url": "http://127.0.0.1",
        "repository": "Example/automation",
    }
    sample = {"automation_id": str(uuid.UUID(int=1))}
    result = sources.resolve_source(config, sample, index)
    assert result["files"] == ["plugins/reviewer/scripts/worker.py"]
    assert result["runtime"] == {
        "timeout": 1200,
        "keep_alive": None,
        "trigger": {"type": "event"},
    }
    assert "historical executed revision not verified" in result["provenance"]
    assert result["configuration"] == {
        "review_tone": "concise",
        "api_key": "[REDACTED]",
    }
    for key, value in (
        ("source", "github:Other/repo"),
        ("ref", "foreign"),
        ("repo_path", "plugins/reviewer/../../other"),
    ):
        original = plugin[key]
        plugin[key] = value
        with pytest.raises(ValueError, match="outside the approved"):
            sources.resolve_source(config, sample, index)
        plugin[key] = original
    assert len(calls) == 4


def test_repository_index_includes_plugins_without_symlinks(tmp_path):
    command(["git", "init", "-q"], tmp_path)
    command(["git", "config", "user.name", "Test"], tmp_path)
    command(["git", "config", "user.email", "test@example.test"], tmp_path)
    directory = tmp_path / "plugins/reviewer/scripts"
    directory.mkdir(parents=True)
    (directory / "worker.py").write_text("print(42)\n")
    (directory / "linked.py").symlink_to("worker.py")
    command(["git", "add", "."], tmp_path)
    command(["git", "commit", "-qm", "plugin"], tmp_path)
    index = sources.repository_index(tmp_path, "HEAD")
    assert set(index["plugins/reviewer"]) == {"scripts/worker.py"}


@pytest.mark.parametrize(
    "previous,rate,patch_count",
    [(5, 50, 1), (50, 50, 0), (50, None, 1), (None, None, 0)],
)
def test_configure_signal_rate_only_and_idempotency(previous, rate, patch_count):
    from configure_signal import configure_rate

    definition = frozen()["flow1_signal"] | {"sampleRate": previous, "disabled": False}

    class Client:
        base = "https://example.test"
        patches = []

        def request(self, url, body=None, method=None):
            assert url == self.base + "/v1/signals/" + SIGNAL
            if body is not None:
                assert method == "PATCH" and body == {"sampleRate": rate}
                self.patches.append(body)
                definition.update(body)
                definition["version"] += 1
            return copy.deepcopy(definition)

    client = Client()
    result = configure_rate(client, definition["projectId"], SIGNAL, rate)
    assert result["sample_rate"] == rate and result["disabled"] is False
    assert len(client.patches) == patch_count


def test_configure_signal_rejects_invalid_percentage_without_network():
    from configure_signal import configure_rate

    for rate in (-1, 0, 96, 100, 101, 0.5, True):
        with pytest.raises(ValueError, match="integer percentage"):
            configure_rate(None, "", SIGNAL, rate)


def test_check_draft_applies_and_checks_patch_without_network(
    configured_source, tmp_path, monkeypatch
):
    checkout, path, config = configured_source
    config["checks"] = []
    path.write_text(json.dumps(config))
    args = arguments(tmp_path / "mechanical", source_config=path, check_draft=True)
    args.output.mkdir()
    original = patches.command

    def run(argv, cwd=None, **kwargs):
        if argv[0] == "gh" or argv[:2] == ["git", "push"]:
            pytest.fail("Mechanical validation cannot publish")
        return original(argv, cwd, **kwargs)

    monkeypatch.setattr(patches, "command", run)
    result = patches.propose_fix(args, fix_report(PATCH), {"source": load_source(path)})
    assert result["status"] == "mechanical_checks_passed"
    assert result["behavioral_evaluation"] == "not_performed"
    assert (checkout / "value.py").read_text() == "VALUE = 3\n"


def test_legacy_event_marker_does_not_block_other_findings(
    configured_source, tmp_path, monkeypatch
):
    records = [record(1), record(2), record(3, automation=2)]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    monkeypatch.setattr(
        weekly,
        "existing_prs",
        lambda r, base="main": [
            {
                "body": "<!-- automation-improvement-event: "
                + records[0]["event"]["id"]
                + " -->",
                "createdAt": weekly.dt.datetime.now(weekly.dt.UTC).isoformat(),
            }
        ],
    )
    result = weekly.run(config)
    assert result["status"] == "completed"
    assert len([call for call in calls if call[0] == "sol"]) == 2
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert records[0]["event"]["id"] not in state["backlog"]


def test_failed_reanalysis_does_not_reuse_older_finding():
    old = record()
    identifier = old["event"]["id"]
    state = {"events": {}, "backlog": {identifier: old}}
    current = inventory([])
    current["latest_runs"] = [old["run"] | {"status": "FAILED"}]
    assert weekly.update_backlog(state, current, []) == []
    assert not state["backlog"]


def test_discovery_is_scoped_to_publication_base(monkeypatch):
    def query(argv):
        if "--base" in argv and argv[argv.index("--base") + 1] == "main":
            return "[]"
        return (
            '[{"headRefName":"automation/improve-validation","body":"validation only"}]'
        )

    monkeypatch.setattr(weekly, "command", query)
    assert weekly.existing_prs("Example/automation", "main") == []


def test_collection_errors_are_not_reported_as_no_findings(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, [])
    frame = inventory([])
    frame["collection_errors"] = [{"event_id": EVENT, "error": "Ambiguous ownership"}]
    monkeypatch.setattr(weekly, "collect_events", lambda *a, **k: frame)
    result = weekly.run(config)
    assert result["status"] == "collection_incomplete"
    assert result["collection_errors"] == frame["collection_errors"]
    assert calls == []


def distinct_records(count=3):
    records = [record(i) for i in range(1, count + 1)]
    for i, item in enumerate(records):
        payload = json.loads(item["event"]["payload"])
        payload["signature"] = "independent issue " + str(i)
        item["event"]["payload"] = json.dumps(payload)
    return records


def test_one_automation_creates_separate_topic_repairs_and_reservations(
    configured_source, tmp_path, monkeypatch
):
    records = distinct_records()
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    result = weekly.run(config)
    topics = result["automations"][0]["topics"]
    assert len(topics) == 3
    assert len({t["publication"]["pr_url"] for t in topics}) == 3
    assert len([c for c in calls if c[0] == "luna"]) == 1
    assert len([c for c in calls if c[0] == "sol"]) == 3
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert len(state["topic_weeks"]) == 3 and not state["backlog"]
    monkeypatch.setattr(
        weekly, "collect_events", lambda *a, **k: inventory(records + [record(4)])
    )
    again = weekly.run(config)
    assert again["automations"][0]["status"] == "completed"
    assert len([call for call in calls if call[0] == "publish"]) == 4


def test_failed_topic_does_not_block_other_topics_in_same_automation(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_grouped(
        configured_source, tmp_path, monkeypatch, distinct_records()
    )
    original = weekly.propose_fix
    count = []

    def publish(args, report, snapshot):
        count.append(args.publication_topic["id"])
        if len(count) == 1:
            args.reserve_publication("automation/improve-" + "0" * 20)
            raise RuntimeError("uncertain write")
        return original(args, report, snapshot)

    monkeypatch.setattr(weekly, "propose_fix", publish)
    result = weekly.run(config)
    assert result["status"] == "partial_failure"
    assert [t["status"] for t in result["automations"][0]["topics"]] == [
        "failed",
        "analyzed",
        "analyzed",
    ]
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert sorted(t["status"] for t in state["topic_weeks"].values()) == [
        "completed",
        "completed",
        "reserved",
    ]
    weekly.run(config)
    assert len(count) == 3


def test_invalid_resource_budget_fails_before_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(
        weekly,
        "load_environment",
        lambda c: pytest.fail("Invalid budget must fail before access"),
    )
    for name in ("max_run_seconds", "max_inference_cost_usd"):
        for value in (0, -1, True, "3", float("inf")):
            with pytest.raises(ValueError, match=name):
                weekly.run(configuration(tmp_path) | {name: value})


def test_completed_legacy_automation_does_not_block_new_topic(
    configured_source, tmp_path, monkeypatch
):
    records = [record()]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    state_dir = Path(config["state_dir"])
    state_dir.mkdir()
    key = (
        weekly.week_key(weekly.dt.datetime.now(weekly.dt.UTC))
        + "/"
        + records[0]["owner"]["automation_id"]
    )
    (state_dir / "state.json").write_text(
        json.dumps({"events": {}, "automation_weeks": {key: {"status": "completed"}}})
    )
    assert weekly.run(config)["status"] == "completed"
    assert len([c for c in calls if c[0] == "publish"]) == 1


def test_uncertain_legacy_automation_publication_is_preserved(
    configured_source, tmp_path, monkeypatch
):
    records = [record()]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    state_dir = Path(config["state_dir"])
    state_dir.mkdir()
    key = (
        weekly.week_key(weekly.dt.datetime.now(weekly.dt.UTC))
        + "/"
        + records[0]["owner"]["automation_id"]
    )
    (state_dir / "state.json").write_text(
        json.dumps({"events": {}, "automation_weeks": {key: {"status": "reserved"}}})
    )
    assert (
        weekly.run(config)["automations"][0]["status"] == "legacy_publication_reserved"
    )
    assert not calls


def test_topic_deduplication_is_scoped_by_automation_and_open_across_weeks():
    owner = str(uuid.UUID(int=1))
    topic = {
        "id": triage.topic_id(owner, "one-issue"),
        "key": "one-issue",
        "representative_event_ids": [EVENT],
    }
    pr = {
        "url": "https://example.test/pr/1",
        "body": f"<!-- automation-improvement-automation: {owner} -->\n<!-- automation-improvement-topic: {topic['id']} -->",
        "state": "OPEN",
        "createdAt": "2026-10-01T00:00:00Z",
    }
    assert weekly.published_for([pr], owner, "2026-W44", topic) == pr
    assert (
        weekly.published_for([pr | {"state": "MERGED"}], owner, "2026-W44", topic)
        is None
    )
    assert weekly.published_for([pr], str(uuid.UUID(int=2)), "2026-W44", topic) is None
    other = topic | {"id": triage.topic_id(owner, "other-issue")}
    assert weekly.published_for([pr], owner, "2026-W44", other) is None


def test_topic_markers_recover_individual_slots_without_blocking_other_topics(
    configured_source, tmp_path, monkeypatch
):
    records = distinct_records()[:2]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    cards, _ = triage.group_findings(records)
    key = "issue-" + cards[0]["group_id"]
    owner = records[0]["owner"]["automation_id"]
    pr = {
        "url": "https://example.test/pr/1",
        "body": f"<!-- automation-improvement-automation: {owner} -->\n<!-- automation-improvement-topic: {triage.topic_id(owner, key)} -->\n<!-- automation-improvement-topic-key: {key} -->",
        "state": "OPEN",
        "createdAt": weekly.dt.datetime.now(weekly.dt.UTC).isoformat(),
    }
    monkeypatch.setattr(weekly, "existing_prs", lambda *a: [pr])
    result = weekly.run(config)
    assert result["status"] == "completed"
    assert len([c for c in calls if c[0] == "publish"]) == 1
    assert [c[2] for c in calls if c[0] == "sol"] != [key]


def test_out_of_topic_finding_is_deferred_in_backlog(
    configured_source, tmp_path, monkeypatch
):
    records = [record()]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    original = weekly.analysis.run

    def analyze(args):
        original(args)
        path = args.output / "report.json"
        report = json.loads(path.read_text())
        report["proposals"] = []
        report["rejected_findings"] = [
            {
                "finding_id": report["findings"][0]["id"],
                "reason": "A different independent issue",
                "disposition": "other_topic",
            }
        ]
        path.write_text(json.dumps(report))

    monkeypatch.setattr(weekly.analysis, "run", analyze)
    weekly.run(config)
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert records[0]["event"]["id"] in state["backlog"]
    assert records[0]["event"]["id"] not in state["events"]


def test_real_publisher_uses_distinct_topic_branches(
    configured_source, tmp_path, monkeypatch
):
    checkout, path, config = configured_source
    owner = str(uuid.UUID(int=1))
    config.update(checks=[], automation_ids=[owner])
    path.write_text(json.dumps(config))
    command(
        ["git", "remote", "add", "origin", "https://github.com/Example/automation.git"],
        checkout,
    )
    original = patches.command
    branches = []

    def transport(argv, cwd=None, **kwargs):
        if argv[:2] == ["gh", "api"]:
            return config["revision"]
        if argv[:3] == ["gh", "pr", "list"]:
            return "[]"
        if argv[:2] == ["git", "ls-remote"]:
            return ""
        if argv[:2] == ["git", "push"]:
            branches.append(argv[-1])
            return ""
        if argv[:3] == ["gh", "pr", "create"]:
            assert argv[argv.index("--label") + 1] == "automation-improvement"
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            assert "automation-improvement-topic:" in body
            assert "Repair topic:" in body
            return "https://github.com/Example/automation/pull/" + str(len(branches))
        return original(argv, cwd, **kwargs)

    monkeypatch.setattr(patches, "command", transport)
    for i, key in enumerate(["head-check", "payload-check"]):
        event = str(uuid.UUID(int=900 + i))
        topic = {
            "key": key,
            "id": triage.topic_id(owner, key),
            "title": key,
            "representative_event_ids": [event],
        }
        args = arguments(
            tmp_path / key,
            source_config=path,
            publish_draft=True,
            publication_automation_id=owner,
            publication_week="2026-W44",
            publication_topic=topic,
        )
        args.output.mkdir()
        report = fix_report(PATCH)
        report["findings"][0].update(
            automation_id=owner, signal_event_id=event, trace_urls=[], case_id="c1"
        )
        result = patches.propose_fix(
            args, report, {"source": load_source(path), "topic": topic}
        )
        assert result["status"] == "unevaluated_draft"
    assert len(branches) == len(set(branches)) == 2


@pytest.mark.parametrize(
    "mutation", ["foreign_event", "wrong_identity", "wrong_snapshot"]
)
def test_publisher_rejects_cross_topic_binding_before_any_write(
    configured_source, tmp_path, monkeypatch, mutation
):
    checkout, path, config = configured_source
    owner = str(uuid.UUID(int=1))
    config.update(checks=[], automation_ids=[owner])
    path.write_text(json.dumps(config))
    topic = {
        "key": "head-check",
        "id": triage.topic_id(owner, "head-check"),
        "title": "Head check",
        "representative_event_ids": [EVENT],
    }
    args = arguments(
        tmp_path / "topic",
        source_config=path,
        publish_draft=True,
        publication_automation_id=owner,
        publication_week="2026-W44",
        publication_topic=copy.deepcopy(topic),
    )
    report = fix_report(PATCH)
    report["findings"][0].update(automation_id=owner, signal_event_id=EVENT)
    if mutation == "foreign_event":
        report["findings"][0]["signal_event_id"] = str(uuid.UUID(int=99))
    elif mutation == "wrong_identity":
        args.publication_topic["id"] = "0" * 20
    else:
        topic["key"] = "other-topic"
    original = patches.command

    def run(argv, cwd=None, **kwargs):
        if argv[0] == "gh" or argv[:2] == ["git", "push"]:
            pytest.fail("Cannot publish a foreign topic")
        return original(argv, cwd, **kwargs)

    monkeypatch.setattr(patches, "command", run)
    with pytest.raises(ValueError):
        patches.propose_fix(args, report, {"source": load_source(path), "topic": topic})


def test_reusing_existing_pr_applies_model_agnostic_label(
    configured_source, tmp_path, monkeypatch
):
    checkout, path, config = configured_source
    args = arguments(
        tmp_path / "reuse",
        source_config=path,
        publish_draft=True,
        coordinator="future-model",
    )
    original = patches.command
    edits = []
    url = "https://github.com/Example/automation/pull/7"

    def commands(argv, cwd=None, **kwargs):
        if argv[:2] == ["gh", "api"]:
            return config["revision"]
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps([{"url": url}])
        if argv[:3] == ["gh", "pr", "edit"]:
            edits.append(argv)
            return ""
        if argv[:2] == ["git", "push"] or argv[:3] == ["gh", "pr", "create"]:
            pytest.fail("Reuse cannot publish a new PR")
        return original(argv, cwd, **kwargs)

    monkeypatch.setattr(patches, "command", commands)
    result = patches.propose_fix(args, fix_report(PATCH), {"source": load_source(path)})
    assert result == {"status": "already_tracked", "pr_url": url, "created": False}
    assert edits == [
        [
            "gh",
            "pr",
            "edit",
            url,
            "--repo",
            config["repository"],
            "--add-label",
            "automation-improvement",
        ]
    ]


def test_more_than_three_topics_publish_across_batches(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_grouped(
        configured_source, tmp_path, monkeypatch, distinct_records(7)
    )
    config.update(triage_batch_size=2, max_topics_per_automation=1)
    result = weekly.run(config)
    topics = result["automations"][0]["topics"]
    assert len(topics) == 7
    assert len({t["publication"]["pr_url"] for t in topics}) == 7
    assert len([c for c in calls if c[0] == "luna"]) == 1
    assert len([c for c in calls if c[0] == "publish"]) == 7


def test_budget_defers_unstarted_topics_and_preserves_backlog(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_grouped(
        configured_source, tmp_path, monkeypatch, distinct_records()
    )
    original = weekly.propose_fix

    def publish(args, report, snapshot):
        result = original(args, report, snapshot)
        args.budget.record(2)
        return result

    monkeypatch.setattr(weekly, "propose_fix", publish)
    result = weekly.run(config | {"max_inference_cost_usd": 1})
    assert result["status"] == "budget_exhausted"
    assert result["budget"]["stop_reason"] == "inference_cost_threshold"
    assert len([c for c in calls if c[0] == "publish"]) == 1
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert len(state["backlog"]) == 2
    assert [row["status"] for row in state["topic_weeks"].values()] == ["completed"]


def test_default_collects_validation_inputs_without_replay_adapter(
    configured_source, tmp_path, monkeypatch
):
    import cohorts

    rows = [record()]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, rows)
    config.pop("collect_validation_inputs")
    selected = []

    def collect(client, frame, automation, records, size, seed):
        selected.append((automation, size, [r["event"]["id"] for r in records]))
        return {
            "scope": "original inputs",
            "selection": {},
            "errors": [],
            "samples": [],
            "sample_size": size,
            "cohort_sha256": "frozen",
            "complete": False,
        }

    monkeypatch.setattr(cohorts, "collect", collect)
    monkeypatch.setattr(cohorts, "prepare", lambda cohort, source, output: cohort)
    weekly.run(config)
    assert selected == [
        (rows[0]["owner"]["automation_id"], 5, [rows[0]["event"]["id"]])
    ]
    assert any(call[0] == "sol" for call in calls)


def test_analysis_failure_is_indexed_on_the_central_issue(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    report = {"errors": [{"stage": "coordinator", "error": "SDK failed"}]}
    (tmp_path / "report.json").write_text(json.dumps(report))

    def fail(args):
        raise ValueError("Partial analysis")

    attempts = []
    from types import SimpleNamespace

    issue = SimpleNamespace(
        attempt=lambda output, value: attempts.append((output, value))
    )
    monkeypatch.setattr(weekly.analysis, "run", fail)
    with pytest.raises(ValueError, match="Partial analysis"):
        weekly.analyze_with_evidence(args, issue)
    assert attempts == [(tmp_path, report)]


def test_laminar_sdk_uses_reachable_http_transport(monkeypatch):
    monkeypatch.delenv("LMNR_FORCE_HTTP", raising=False)
    weekly.load_environment(
        {
            "laminar_base_url": "https://example.test",
            "llm_base_url": "https://proxy.test",
        }
    )
    import os

    assert os.environ["LMNR_FORCE_HTTP"] == "true"


@pytest.mark.parametrize(
    "error",
    [
        ValueError("allowlist rejection"),
        RuntimeError("reviewer HTTP failure"),
        weekly.BudgetExhausted("run_time_threshold"),
    ],
)
def test_failure_before_external_write_remains_retryable(
    configured_source, tmp_path, monkeypatch, error
):
    config, calls = prepare_grouped(
        configured_source, tmp_path, monkeypatch, [record()]
    )
    original = weekly.propose_fix
    attempts = []

    def publish(args, report, snapshot):
        attempts.append(args.output)
        if len(attempts) == 1:
            raise error
        return original(args, report, snapshot)

    monkeypatch.setattr(weekly, "propose_fix", publish)
    first = weekly.run(config)
    state_path = Path(config["state_dir"]) / "state.json"
    state = json.loads(state_path.read_text())
    assert {row["status"] for row in state["topic_weeks"].values()} == {"retryable"}
    assert state["backlog"]
    assert first["status"] in ("partial_failure", "budget_exhausted")
    original_triage = weekly.triage

    def rename(args, pending, prs, completed_topics=()):
        topics, context = original_triage(args, pending, prs, completed_topics)
        return [
            topic
            | {
                "key": "renamed",
                "id": triage.topic_id(pending[0]["owner"]["automation_id"], "renamed"),
            }
            for topic in topics
        ], context

    monkeypatch.setattr(weekly, "triage", rename)
    second = weekly.run(config)
    assert second["automations"][0]["topics"][0]["status"] == "waiting_for_new_evidence"
    assert len(attempts) == 1
    state = json.loads(state_path.read_text())
    config["retry_topic_ids"] = [
        next(iter(state["topic_weeks"].values()))["topic"]["id"]
    ]
    third = weekly.run(config)
    assert third["status"] == "completed" and len(attempts) == 2
    assert attempts[0].parent.name == attempts[1].parent.name
    assert not json.loads(state_path.read_text())["backlog"]


def test_uncertain_publication_recovers_matching_pr_or_absent_branch(monkeypatch):
    from triage import topic_id

    week = weekly.week_key(weekly.dt.datetime.now(weekly.dt.UTC))
    automation = str(uuid.UUID(int=1))
    identifier = topic_id(automation, "topic")
    key = f"{week}/{automation}/{identifier}"
    slot = {
        "status": "reserved",
        "topic": {"id": identifier},
        "branch": "automation/improve-" + "a" * 20,
    }
    state = {"topic_weeks": {key: slot}}
    pr = {
        "createdAt": weekly.dt.datetime.now(weekly.dt.UTC).isoformat(),
        "url": "https://github.com/example/repo/pull/1",
        "body": f"<!-- automation-improvement-automation: {automation} -->\n<!-- automation-improvement-topic: {identifier} -->",
    }
    weekly.recover_topic_slots(state, [pr], week)
    assert state["topic_weeks"][key]["status"] == "completed"
    assert state["topic_weeks"][key]["pr_url"] == pr["url"]
    state["topic_weeks"][key] = slot
    monkeypatch.setattr(weekly, "command", lambda *a: "")
    weekly.recover_topic_slots(state, [], week, {"checkout": "/existing"})
    assert not state["topic_weeks"]
    state["topic_weeks"][key] = slot
    monkeypatch.setattr(
        weekly, "command", lambda *a: "a" * 40 + "\trefs/heads/" + slot["branch"]
    )
    weekly.recover_topic_slots(state, [], week, {"checkout": "/existing"})
    assert state["topic_weeks"][key]["status"] == "reserved"


def test_retry_preserves_issue_namespace_when_luna_renames_overlapping_clusters():
    previous = {
        "id": "original-id",
        "key": "retry-provider-outages",
        "group_ids": ["a", "b", "c"],
    }
    current = {
        "id": "renamed-id",
        "key": "retry-provider-failures",
        "group_ids": ["a", "c", "d"],
        "representative_event_ids": ["new-evidence"],
    }
    reused = weekly.reuse_topic(current, [previous])
    assert reused["id"] == previous["id"] and reused["key"] == previous["key"]
    assert reused["group_ids"] == current["group_ids"] and reused[
        "representative_event_ids"
    ] == ["new-evidence"]
    unrelated = current | {"group_ids": ["c", "d", "e"]}
    assert weekly.reuse_topic(unrelated, [previous]) == unrelated


@pytest.mark.parametrize("revision", ["unsupported", "unrepaired", "same", "checks"])
def test_revision_adopts_conclusions_and_evidence_without_requiring_new_code(
    configured_source, tmp_path, monkeypatch, revision
):
    config, calls = prepare_grouped(
        configured_source, tmp_path, monkeypatch, [record()]
    )
    config["revise_failed_repair"] = True
    original = weekly.analysis.run
    publications = []

    def analyze(args):
        original(args)
        if args.output.name != "revision":
            return
        path = args.output / "report.json"
        report = json.loads(path.read_text())
        if revision in ("unsupported", "unrepaired"):
            report["proposals"] = []
            report["rejected_findings"] = [
                {
                    "finding_id": "c1f1",
                    "disposition": revision,
                    "reason": "The available experiment does not support a fix",
                }
            ]
        elif revision == "checks":
            report["proposals"][0]["candidate_checks"] = [
                {"name": "Safety suite", "argv": ["python", "tests/test_safety.py"]}
            ]
        path.write_text(json.dumps(report))

    def publish(args, report, snapshot):
        publications.append(report["proposals"][0])
        if len(publications) == 2:
            assert revision == "checks"
            assert publications[0]["patch"] == publications[1]["patch"]
            return {
                "status": "evidence_supported_draft",
                "pr_url": "https://example.test/pr/1",
            }
        failed = {
            "status": "rejected",
            "baseline": [],
            "candidate": [],
            "evidence_review": {
                "supported": False,
                "reason": "Missing executed safety tests",
            },
        }
        (args.output / "paired-review-failure.json").write_text(json.dumps(failed))
        return failed

    monkeypatch.setattr(weekly.analysis, "run", analyze)
    monkeypatch.setattr(weekly, "propose_fix", publish)
    result = weekly.run(config)
    topic = result["automations"][0]["topics"][0]
    assert len(publications) == (2 if revision == "checks" else 1)
    assert topic["analysis_report"].endswith("revision/report.json")
    if revision in ("unsupported", "unrepaired"):
        assert topic["publication"]["status"] == "not_proposed"
        assert topic["proposals"] == 0
        assert topic["rejections"][0]["disposition"] == revision
    weekly.run(config)
    assert len([c for c in calls if c[0] == "sol"]) == 2
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert bool(state["backlog"]) == (revision in ("same", "unrepaired"))


@pytest.mark.parametrize("change", ["source", "signal", "explicit", "legacy"])
def test_failed_attempt_waits_across_weeks_until_inputs_change(
    configured_source, tmp_path, monkeypatch, change
):
    rows = [record()]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, rows)
    original = weekly.propose_fix
    publications = []

    def publish(args, report, snapshot):
        publications.append(args.output)
        if len(publications) == 1:
            failure = {
                "status": "rejected",
                "baseline": [],
                "candidate": [],
                "evidence_review": {
                    "supported": False,
                    "reason": "No behavioral improvement",
                },
            }
            (args.output / "paired-review-failure.json").write_text(json.dumps(failure))
            return failure
        return original(args, report, snapshot)

    monkeypatch.setattr(weekly, "propose_fix", publish)
    weekly.run(config)
    assert len([c for c in calls if c[0] == "sol"]) == 1
    state_path = Path(config["state_dir"]) / "state.json"
    state = json.loads(state_path.read_text())
    key, value = next(iter(state["topic_weeks"].items()))
    if change == "legacy":
        value.pop("attempt")
        value.pop("conclusion")
    state["topic_weeks"] = {"2026-W01/" + key.split("/", 1)[1]: value}
    state_path.write_text(json.dumps(state))
    again = weekly.run(config)
    assert again["budget"]["stop_reason"] is None
    assert again["automations"][0]["topics"][0]["status"] == "waiting_for_new_evidence"
    assert len([c for c in calls if c[0] == "sol"]) == 1
    if change == "legacy":
        return
    if change == "signal":
        rows[0]["run"]["updated_at"] = "2026-10-31T12:00:00Z"
    elif change == "source":
        checkout, _, source = configured_source
        (checkout / "value.py").write_text("VALUE = 3\nEXTRA = 1\n")
        command(["git", "add", "."], checkout)
        command(["git", "commit", "-qm", "new source"], checkout)
        source["revision"] = command(["git", "rev-parse", "HEAD"], checkout)
    else:
        config["retry_topic_ids"] = [value["topic"]["id"]]
    weekly.run(config)
    assert len([c for c in calls if c[0] == "sol"]) == 2
    assert len(publications) == 2


def test_repeated_triage_without_new_representatives_stops(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_grouped(
        configured_source, tmp_path, monkeypatch, [record(1), record(2)]
    )
    rank = weekly.triage
    selected = []
    contexts = []

    def repeat(args, pending, prs, completed_topics=()):
        if not selected:
            plans, context = rank(args, pending, prs, completed_topics)
            selected.extend(plans)
            contexts.append(context)
        return selected, contexts[0]

    monkeypatch.setattr(weekly, "triage", repeat)
    result = weekly.run(config)
    assert len([c for c in calls if c[0] == "sol"]) == 1
    assert len(result["automations"][0]["topics"]) == 1


def test_high_level_profile_reaches_the_coding_attempt(
    configured_source, tmp_path, monkeypatch
):
    config, _ = prepare_grouped(configured_source, tmp_path, monkeypatch, [record()])
    config["repair_agent_profile"] = "factory-developer"
    analyze = weekly.analysis.run
    selected = []

    def run(args):
        selected.append(args.repair_agent_profile)
        return analyze(args)

    monkeypatch.setattr(weekly.analysis, "run", run)
    weekly.run(config)
    assert selected == ["factory-developer"]


@pytest.mark.parametrize("profile", [True, {}, "", "../profile"])
def test_invalid_profile_fails_before_collection(monkeypatch, profile):
    monkeypatch.setattr(
        weekly, "collect_events", lambda *a, **k: pytest.fail("Must not collect")
    )
    with pytest.raises(ValueError, match="repair_agent_profile"):
        weekly.run({"repair_agent_profile": profile})


def test_global_priority_reaches_highest_impact_automation_before_budget_exhaustion(
    configured_source, tmp_path, monkeypatch
):
    rows = [record(1), record(2, automation=2)]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, rows)
    rank, publish = weekly.triage, weekly.propose_fix

    def priorities(*args):
        topics, context = rank(*args)
        for topic in topics:
            topic["priority"] = (
                0 if topic["automation_id"] == rows[1]["owner"]["automation_id"] else 3
            )
        return topics, context

    def spend(args, report, snapshot):
        result = publish(args, report, snapshot)
        args.budget.record(2)
        return result

    monkeypatch.setattr(weekly, "triage", priorities)
    monkeypatch.setattr(weekly, "propose_fix", spend)
    result = weekly.run(config | {"max_inference_cost_usd": 1})
    assert result["status"] == "budget_exhausted"
    assert [c[1] for c in calls if c[0] == "sol"] == [rows[1]["owner"]["automation_id"]]
    assert len([c for c in calls if c[0] == "luna"]) == 1
    assert result["priority_queue"][0]["priority"] == 0
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert set(state["backlog"]) == {rows[0]["event"]["id"]}


def test_triage_profile_and_limits_reach_the_single_global_planning_agent(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_grouped(
        configured_source, tmp_path, monkeypatch, [record(1), record(2, automation=2)]
    )
    rank = weekly.triage
    inspected = []

    def inspect(args, *rest):
        inspected.append((args.triage_agent_profile, args.triage_agent_config))
        return rank(args, *rest)

    monkeypatch.setattr(weekly, "triage", inspect)
    config.update(triage_agent_profile="my-luna", triage_agent={"max_run_seconds": 120})
    weekly.run(config)
    assert inspected == [("my-luna", {"max_run_seconds": 120})]
    assert len([c for c in calls if c[0] == "luna"]) == 1


def test_sequential_dispatch_respects_top_level_pr_limit(
    configured_source, tmp_path, monkeypatch
):
    rows = [record(n, automation=n) for n in range(1, 4)]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, rows)
    result = weekly.run(config | {"max_prs_per_run": 1})
    assert result["max_prs_per_run"] == 1
    assert result["created_prs"] == 1
    assert result["pr_limit_reached"]
    assert result["status"] == "pr_limit_reached"
    assert len([call for call in calls if call[0] == "sol"]) == 1
    assert (
        sum(
            outcome["status"] == "deferred_pr_limit"
            for automation in result["automations"]
            for outcome in automation["topics"]
        )
        == 2
    )
    saved = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert len(saved["backlog"]) == 2
    assert len(saved["topic_weeks"]) == 1
