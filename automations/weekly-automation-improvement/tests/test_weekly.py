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
    path.write_text(json.dumps(config))
    command(
        ["git", "remote", "add", "origin", "https://github.com/Example/automation.git"],
        checkout,
    )
    args = arguments(tmp_path / "review", source_config=path, publish_draft=True)
    args.output.mkdir()
    report = fix_report(
        PATCH if valid_syntax else PATCH.replace("+VALUE = 4", "+VALUE = (")
    )
    report["findings"][0].update(
        trace_urls=["https://example.test/trace"], signal_event_id=EVENT
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


def no_events(client, handled):
    return {
        "samples": [],
        "selection": {"selected_event_ids": []},
        "signal_run_summary": {"attempts": 0},
        "collection_errors": [],
    }


def test_no_events_is_success_and_completed_week_is_not_repeated(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test")
    monkeypatch.setattr(weekly, "github_auth", lambda c: None)
    monkeypatch.setattr(weekly, "existing_prs", lambda r: [])
    monkeypatch.setattr(weekly, "collect_events", no_events)
    monkeypatch.setattr(
        weekly.analysis, "run", lambda a: pytest.fail("No finding needs no model")
    )
    first = weekly.run(config)
    assert first["status"] == "no_unhandled_findings"
    monkeypatch.setattr(
        weekly,
        "collect_events",
        lambda *a: pytest.fail("Completed week must not rescan"),
    )
    second = weekly.run(config)
    assert second["status"] == "weekly_batch_already_completed"


def test_existing_pr_markers_recover_handled_event_ids(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test")
    monkeypatch.setattr(weekly, "github_auth", lambda c: None)
    monkeypatch.setattr(
        weekly,
        "existing_prs",
        lambda r: [
            {
                "body": "<!-- automation-improvement-event: " + EVENT + " -->",
                "createdAt": "2025-01-01T00:00:00Z",
            }
        ],
    )

    def collect(client, handled):
        assert EVENT in handled
        return no_events(client, handled)

    monkeypatch.setattr(weekly, "collect_events", collect)
    assert weekly.run(config)["status"] == "no_unhandled_findings"


def test_concurrent_weekly_runner_does_not_enter_pipeline(tmp_path):
    config = configuration(tmp_path)
    directory = Path(config["state_dir"])
    directory.mkdir()
    with (directory / "weekly.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert weekly.run(config) == {"status": "skipped_overlap"}


def test_existing_pr_in_current_week_enforces_limit_without_local_state(
    tmp_path, monkeypatch
):
    config = configuration(tmp_path)
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test")
    monkeypatch.setattr(weekly, "github_auth", lambda c: None)
    monkeypatch.setattr(
        weekly,
        "existing_prs",
        lambda r: [
            {"body": "", "createdAt": weekly.dt.datetime.now(weekly.dt.UTC).isoformat()}
        ],
    )
    monkeypatch.setattr(weekly, "collect_events", no_events)
    monkeypatch.setattr(
        weekly,
        "source_config",
        lambda *a: pytest.fail("Do not investigate past weekly PR cap"),
    )
    assert weekly.run(config)["status"] == "weekly_pr_limit_reached"


def prepare_weekly_proposal(configured_source, tmp_path, monkeypatch):
    _, path, _ = configured_source
    config = configuration(tmp_path) | {"publish_drafts": True}
    value = frozen()
    value["samples"][0]["selected_event_ids"] = [EVENT]
    value["selection"]["selected_event_ids"] = [EVENT]
    value["signal_run_summary"] = {"attempts": 1}
    value["collection_errors"] = []
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "test")
    monkeypatch.setattr(weekly, "github_auth", lambda c: None)
    monkeypatch.setattr(weekly, "existing_prs", lambda r: [])
    monkeypatch.setattr(weekly, "collect_events", lambda *a: copy.deepcopy(value))
    monkeypatch.setattr(
        weekly, "source_config", lambda c, s, o: (path, s["samples"], [])
    )
    calls = []

    def analyze(args):
        calls.append("analysis")
        args.output.mkdir()
        report = fix_report(PATCH)
        report["findings"][0]["signal_event_id"] = EVENT
        report.update(rejected_findings=[], measurements=[], errors=[])
        (args.output / "report.json").write_text(json.dumps(report))

    monkeypatch.setattr(weekly.analysis, "run", analyze)
    return config, calls


def test_weekly_publication_persists_event_and_week(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_weekly_proposal(configured_source, tmp_path, monkeypatch)

    def publish(args, report, snapshot):
        assert args.publish_draft and not args.publish_fix
        calls.append("publish")
        return {
            "status": "unevaluated_draft",
            "pr_url": "https://github.com/Example/automation/pull/1",
        }

    monkeypatch.setattr(weekly, "propose_fix", publish)
    first = weekly.run(config)
    assert first["publication"]["pr_url"].endswith("/1")
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert state["events"][EVENT]["status"] == "draft_pr"
    assert len(state["publications"]) == 1
    assert weekly.run(config)["status"] == "weekly_batch_already_completed"
    assert calls == ["analysis", "publish"]


def test_dry_run_does_not_publish_or_change_ledger(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_weekly_proposal(configured_source, tmp_path, monkeypatch)
    monkeypatch.setattr(
        weekly, "propose_fix", lambda *a: pytest.fail("Dry run cannot publish")
    )
    assert weekly.run(config, dry_run=True)["publication"] == "disabled"
    assert not (Path(config["state_dir"]) / "state.json").exists()
    assert calls == ["analysis"]


def test_uncertain_publication_reserves_week_and_prevents_second_attempt(
    configured_source, tmp_path, monkeypatch
):
    config, calls = prepare_weekly_proposal(configured_source, tmp_path, monkeypatch)
    monkeypatch.setattr(
        weekly,
        "propose_fix",
        lambda *a: (_ for _ in ()).throw(
            RuntimeError("connection lost during publication")
        ),
    )
    with pytest.raises(RuntimeError):
        weekly.run(config)
    state = json.loads((Path(config["state_dir"]) / "state.json").read_text())
    assert next(iter(state["publications"].values()))["status"] == "reserved"
    assert weekly.run(config)["status"] == "weekly_pr_limit_reached"
    assert calls == ["analysis"]


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
