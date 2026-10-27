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
    report = fix_report(
        PATCH if valid_syntax else PATCH.replace("+VALUE = 4", "+VALUE = (")
    )
    report["findings"][0].update(
        trace_urls=["https://example.test/trace"],
        signal_event_id=EVENT,
        automation_id=identifier,
    )
    original = patches.command
    pushed = []

    def run(argv, cwd=None, **kwargs):
        if argv[:2] == ["gh", "api"]:
            return config["revision"]
        if argv[:3] == ["gh", "pr", "list"]:
            return "[]"
        if argv[:2] == ["git", "ls-remote"]:
            return ""
        if argv[:2] == ["git", "push"]:
            pushed.append(argv)
            return ""
        if argv[:3] == ["gh", "pr", "create"]:
            assert "--draft" in argv
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
            patches.propose_fix(args, report, {"source": load_source(path)})
        assert pushed == []
        return
    result = patches.propose_fix(args, report, {"source": load_source(path)})
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
    monkeypatch.setattr(weekly, "existing_prs", lambda r: [])
    monkeypatch.setattr(weekly, "collect_events", lambda *a, **k: inventory(records))
    calls = []

    def rank(args, pending, prs):
        identifier = pending[0]["owner"]["automation_id"]
        calls.append(("luna", identifier))
        assert all(item["owner"]["automation_id"] == identifier for item in pending)
        return pending[:1], {
            "automation_id": identifier,
            "total_events": len(pending),
            "affected_conversations": len(pending),
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
        calls.append(("sol", case["automation_id"]))
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
        calls.append(("publish", identifier))
        return {
            "status": "unevaluated_draft",
            "pr_url": "https://github.com/Example/automation/pull/"
            + str(int(uuid.UUID(identifier))),
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
        item["publication"]["status"] == "unevaluated_draft"
        for item in result["automations"]
    )
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert len(state["automation_weeks"]) == 2
    assert set(state["backlog"]) == {records[1]["event"]["id"]}
    assert len([call for call in calls if call[0] == "sol"]) == 2
    again = weekly.run(config)
    assert len(calls) == 6
    assert (
        again["automations"][0]["status"]
        == "automation_week_already_completed_or_reserved"
    )


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
    assert result["automations"][0]["status"] == "failed"
    assert result["automations"][1]["publication"]["status"] == "unevaluated_draft"
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert sorted(item["status"] for item in state["automation_weeks"].values()) == [
        "completed",
        "reserved",
    ]
    again = weekly.run(config)
    assert (
        again["automations"][0]["status"]
        == "automation_week_already_completed_or_reserved"
    )
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
    assert result["automations"][0]["publication"] == "disabled"
    assert not (Path(config["state_dir"]) / "state.json").exists()
    assert [call[0] for call in calls] == ["luna", "sol", "check"]


def test_existing_pr_recovers_per_automation_cap_without_local_state(
    configured_source, tmp_path, monkeypatch
):
    records = [record(1), record(2, automation=2)]
    config, calls = prepare_grouped(configured_source, tmp_path, monkeypatch, records)
    monkeypatch.setattr(
        weekly,
        "existing_prs",
        lambda r: [
            {
                "body": "<!-- automation-improvement-automation: "
                + records[0]["owner"]["automation_id"]
                + " -->",
                "createdAt": weekly.dt.datetime.now(weekly.dt.UTC).isoformat(),
            }
        ],
    )
    result = weekly.run(config)
    assert (
        result["automations"][0]["status"]
        == "automation_week_already_completed_or_reserved"
    )
    assert [call[0] for call in calls] == ["luna", "sol", "publish"]
    assert all(call[1] == records[1]["owner"]["automation_id"] for call in calls)


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
