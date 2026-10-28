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
        return
    result = patches.propose_fix(
        args, report, {"source": load_source(path), "topic": args.publication_topic}
    )
    assert result["status"] == "unevaluated_draft"
    assert result["baseline"] == result["candidate"] == []
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
    _, _, source = configured_source
    config = configuration(tmp_path) | {"publish_drafts": True}
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test")
    monkeypatch.setattr(weekly, "github_auth", lambda c: None)
    monkeypatch.setattr(weekly, "existing_prs", lambda r, base="main": [])
    monkeypatch.setattr(weekly, "collect_events", lambda *a, **k: inventory(records))
    calls = []

    def rank(args, pending, prs, completed_topics=()):
        identifier = pending[0]["owner"]["automation_id"]
        calls.append(("luna", identifier))
        assert all(item["owner"]["automation_id"] == identifier for item in pending)
        cards, _ = triage.group_findings(pending)
        done = {t["id"] for t in completed_topics}
        plans = []
        for card in cards:
            key = "issue-" + card["group_id"]
            topic_id = triage.topic_id(identifier, key)
            if topic_id not in done:
                plans.append(
                    {
                        "id": topic_id,
                        "key": key,
                        "title": "One issue",
                        "reason": "Investigate one failure",
                        "group_ids": [card["group_id"]],
                        "representative_event_ids": [card["examples"][0]["event_id"]],
                    }
                )
        plans = plans[: args.max_topics]
        return plans, {
            "automation_id": identifier,
            "total_events": len(pending),
            "affected_conversations": len(pending),
            "groups": cards,
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
    assert again["automations"][0]["status"] == "no_promising_findings"
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
    with pytest.raises(ValueError, match="triage_batch_size"):
        weekly.run(configuration(tmp_path) | {"triage_batch_size": 0})


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
    assert result == {"status": "already_tracked", "pr_url": url}
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
    assert len([c for c in calls if c[0] == "luna"]) == 4
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
    assert len(state["backlog"]) == 2 and len(state["topic_weeks"]) == 1
