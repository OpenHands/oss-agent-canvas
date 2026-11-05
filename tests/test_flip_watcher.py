import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The deployed automation tarball and the plugin scripts carry the same file;
# every behavioral test runs against both so the mirror cannot silently drift.
MODULES = sorted(
    ROOT.glob("automations/automation-flip-watcher-*/tarball/worker.py")
) + sorted(ROOT.glob("plugins/automation-flip-watcher/scripts/worker.py"))


def load_module(path):
    spec = importlib.util.spec_from_file_location("flip_watcher", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def utc(year=2026, month=11, day=5, hour=13, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class SlackSpy:
    """In-memory Slack boundary; tests must never post a real message."""

    def __init__(self):
        self.posts = []

    def post(self, token, channel, text, thread_ts=None):
        self.posts.append(
            {"channel": channel, "text": text, "thread_ts": thread_ts}
        )
        return f"1700000000.{len(self.posts):06d}"


class WorkerTestBase(unittest.TestCase):
    """Shared harness; subclasses set MODULE_INDEX to pick a worker copy."""

    MODULE_INDEX = 0
    AID = "aaaaaaaa-0000-0000-0000-000000000001"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_workspace = os.environ.get("WORKSPACE_BASE")
        # Three segments so _state_file_path's parent.parent lands in the
        # per-test temp directory, mirroring the VM's workspaces/ layout.
        os.environ["WORKSPACE_BASE"] = str(
            Path(self.tmp.name) / "automation-runs" / "run-1"
        )
        self.addCleanup(self._restore_workspace)
        self.module = load_module(MODULES[self.MODULE_INDEX])
        self.module.CONFIG.update(self.module.DEFAULT_CONFIG)
        self.module.CONFIG["db_path"] = str(Path(self.tmp.name) / "automations.db")
        self.module.CONFIG["conversations_dir"] = str(
            Path(self.tmp.name) / "conversations"
        )
        self.module.CONFIG["heartbeat_enabled"] = False
        self.module.CONFIG["stale_check_enabled"] = False
        self.module.CONFIG["silent_failure_check"] = False
        self.slack = SlackSpy()
        self.module.post_message = self.slack.post
        self.now = utc()
        saved_utcnow = self.module.utcnow
        self.module.utcnow = lambda: self.now
        self.addCleanup(lambda: setattr(self.module, "utcnow", saved_utcnow))

    def _restore_workspace(self):
        if self._old_workspace is None:
            os.environ.pop("WORKSPACE_BASE", None)
        else:
            os.environ["WORKSPACE_BASE"] = self._old_workspace

    def ts(self, hours_ago=0, minutes_ago=0):
        return (self.now - timedelta(hours=hours_ago, minutes=minutes_ago)).isoformat()

    def make_db(self, automations, runs):
        path = Path(self.module.CONFIG["db_path"])
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE automations (id TEXT PRIMARY KEY, name TEXT,"
            " trigger TEXT, enabled INTEGER, state TEXT, timeout INTEGER)"
        )
        conn.execute(
            "CREATE TABLE automation_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " automation_id TEXT, status TEXT, error_detail TEXT,"
            " started_at TEXT, completed_at TEXT, conversation_id TEXT,"
            " created_at TEXT)"
        )
        for automation_id, name, schedule, enabled, state in automations:
            conn.execute(
                "INSERT INTO automations VALUES (?, ?, ?, ?, ?, ?)",
                (
                    automation_id,
                    name,
                    json.dumps(
                        {"type": "cron", "schedule": schedule, "timezone": "UTC"}
                    ),
                    enabled,
                    state,
                    300,
                ),
            )
        for automation_id, status, error, started, conversation_id in runs:
            conn.execute(
                "INSERT INTO automation_runs"
                " (automation_id, status, error_detail, started_at, completed_at,"
                "  conversation_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    automation_id,
                    status,
                    error,
                    started,
                    started,
                    conversation_id,
                    started,
                ),
            )
        conn.commit()
        conn.close()

    def run_main(self):
        self.module.main()


class ConfigTest(WorkerTestBase):
    def test_defaults_when_no_config_file(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        config = self.module.load_config(directory=empty)
        self.assertEqual(config["fail_threshold"], 2)
        self.assertEqual(config["alert_channel"], "C0BGK8NRYUX")
        self.assertEqual(config["heartbeat_hour_utc"], 8)

    def test_unknown_keys_are_ignored(self):
        (Path(self.tmp.name) / "config.json").write_text(
            '{"source_repository": "OpenHands/oss-agent-canvas", "source_branch": "main"}'
        )
        config = self.module.load_config(directory=Path(self.tmp.name))
        self.assertEqual(config["fail_threshold"], 2)

    def test_wrong_type_is_a_hard_error(self):
        (Path(self.tmp.name) / "config.json").write_text('{"fail_threshold": "2"}')
        with self.assertRaises(SystemExit):
            self.module.load_config(directory=Path(self.tmp.name))

    def test_invalid_thresholds_are_a_hard_error(self):
        (Path(self.tmp.name) / "config.json").write_text('{"fail_threshold": 0}')
        with self.assertRaises(SystemExit):
            self.module.load_config(directory=Path(self.tmp.name))

    def test_invalid_heartbeat_hour_is_a_hard_error(self):
        (Path(self.tmp.name) / "config.json").write_text('{"heartbeat_hour_utc": 24}')
        with self.assertRaises(SystemExit):
            self.module.load_config(directory=Path(self.tmp.name))


class FlipDetectionTest(WorkerTestBase):
    def test_flip_alerts_after_two_failures_following_healthy(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=5), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=4), ""),
            (self.AID, "FAILED", "RuntimeError: boom", self.ts(hours_ago=2), ""),
            (self.AID, "FAILED", "RuntimeError: boom", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        post = self.slack.posts[0]
        self.assertEqual(post["channel"], "C0BGK8NRYUX")
        self.assertIn("flipped to failing", post["text"])
        self.assertIn("PR Reviewer", post["text"])
        self.assertIn("RuntimeError: boom", post["text"])
        self.assertIn("2 consecutive failed runs", post["text"])
        self.assertIn("Sent by OpenHands on behalf of the user.", post["text"])

    def test_single_failure_does_not_alert(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), ""),
            (self.AID, "FAILED", "transient", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_no_alert_while_healthy(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_single_pass_baseline_then_two_failures_is_observed(self):
        # One pass is not "consistently passing", so without a prior healthy
        # state this is an observation, not a flip.
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=3), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        self.assertIn("observed on first watch", self.slack.posts[0]["text"])

    def test_realert_threads_on_original_after_realert_hours(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=30), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=3), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        original = self.slack.posts[0]
        self.assertIsNone(original["thread_ts"])
        thread_ts = f"1700000000.{len(self.slack.posts):06d}"
        self.slack.posts.clear()

        # 25 hours later the automation is still failing: re-alert, threaded.
        later = utc() + timedelta(hours=25)
        saved_utcnow = self.module.utcnow
        self.module.utcnow = lambda: later
        try:
            self.run_main()
        finally:
            self.module.utcnow = saved_utcnow
        self.assertEqual(len(self.slack.posts), 1)
        self.assertIn("still failing", self.slack.posts[0]["text"])
        self.assertEqual(self.slack.posts[0]["thread_ts"], thread_ts)

    def test_no_realert_before_realert_hours(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=30), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=3), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.slack.posts.clear()
        later = utc() + timedelta(hours=2)
        saved_utcnow = self.module.utcnow
        self.module.utcnow = lambda: later
        try:
            self.run_main()
        finally:
            self.module.utcnow = saved_utcnow
        self.assertEqual(self.slack.posts, [])

    def test_recovery_posts_threaded_and_unalerts(self):
        runs = [
            (self.AID, "FAILED", "boom", self.ts(hours_ago=30), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        # Seed the watcher state as already alerted.
        state_path = self.module._state_file_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "heartbeat_date": "",
                    "automations": {
                        "PR Reviewer": {
                            "status": "failing",
                            "in_alert": True,
                            "first_alert_at": "2026-11-04 10:00 UTC",
                            "last_alert_at": "2026-11-04 10:00 UTC",
                            "thread_ts": "1700000000.000001",
                        }
                    },
                }
            )
        )
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        post = self.slack.posts[0]
        self.assertIn("back to green", post["text"])
        self.assertEqual(post["thread_ts"], "1700000000.000001")

        # The follow-up run stays quiet: recovery happened once.
        self.slack.posts.clear()
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_observed_on_first_watch_when_already_failing(self):
        runs = [
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "Memory Curator", "0 21 * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        self.assertIn("observed on first watch", self.slack.posts[0]["text"])

    def test_non_terminal_statuses_are_not_outcomes(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=3), ""),
            (self.AID, "RUNNING", "", self.ts(minutes_ago=10), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])


class SilentFailureTest(WorkerTestBase):
    AID = "bbbbbbbb-0000-0000-0000-000000000002"

    def setUp(self):
        super().setUp()
        self.module.CONFIG["silent_failure_check"] = True

    def _make_conversation(self, execution_status):
        conv_id = "4f0a6c90-1111-2222-3333-444444444444"
        conv_dir = (
            Path(self.module.CONFIG["conversations_dir"]) / conv_id.replace("-", "")
        )
        conv_dir.mkdir(parents=True)
        (conv_dir / "base_state.json").write_text(
            json.dumps({"execution_status": execution_status})
        )
        return conv_id

    def test_completed_run_with_erroring_conversation_alerts(self):
        conv_id = self._make_conversation("error")
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), conv_id),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), conv_id),
        ]
        self.make_db([(self.AID, "Memory Curator", "0 21 * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        self.assertIn("completed but actually failed", self.slack.posts[0]["text"])
        self.assertIn("Memory Curator", self.slack.posts[0]["text"])

    def test_completed_run_with_stuck_conversation_alerts(self):
        conv_id = self._make_conversation("stuck")
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), conv_id),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), conv_id),
        ]
        self.make_db([(self.AID, "Memory Curator", "0 21 * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)

    def test_healthy_conversation_does_not_alert(self):
        conv_id = self._make_conversation("finished")
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), conv_id),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), conv_id),
        ]
        self.make_db([(self.AID, "Memory Curator", "0 21 * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_missing_conversation_directory_is_not_an_alert(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "Memory Curator", "0 21 * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_same_silent_conversation_does_not_realert(self):
        conv_id = self._make_conversation("error")
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), conv_id),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), conv_id),
        ]
        self.make_db([(self.AID, "Memory Curator", "0 21 * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        self.slack.posts.clear()
        self.run_main()
        self.assertEqual(self.slack.posts, [])


class StaleTest(WorkerTestBase):
    AID = "cccccccc-0000-0000-0000-000000000003"

    def setUp(self):
        super().setUp()
        self.module.CONFIG["stale_check_enabled"] = True

    def test_stale_alert_when_no_run_for_far_longer_than_cadence(self):
        runs = [(self.AID, "COMPLETED", "", self.ts(hours_ago=30), "")]
        self.make_db([(self.AID, "Hourly Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        self.assertIn("has not run", self.slack.posts[0]["text"])
        self.assertIn("Hourly Reviewer", self.slack.posts[0]["text"])

    def test_no_stale_alert_within_cadence(self):
        runs = [(self.AID, "COMPLETED", "", self.ts(hours_ago=1), "")]
        self.make_db([(self.AID, "Hourly Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_no_stale_alert_while_run_in_flight(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=30), ""),
            (self.AID, "RUNNING", "", self.ts(minutes_ago=10), ""),
        ]
        self.make_db([(self.AID, "Hourly Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_stale_alerts_once(self):
        runs = [(self.AID, "COMPLETED", "", self.ts(hours_ago=30), "")]
        self.make_db([(self.AID, "Hourly Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        self.slack.posts.clear()
        self.run_main()
        self.assertEqual(self.slack.posts, [])


class ScopeTest(WorkerTestBase):
    def test_disabled_automation_is_not_watched(self):
        runs = [
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "Disabled One", "0 * * * *", 0, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_inactive_state_is_not_watched(self):
        runs = [
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "Draft One", "0 * * * *", 1, "DRAFT")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def _make_db_with_triggers(self, automations, runs):
        path = Path(self.module.CONFIG["db_path"])
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE automations (id TEXT PRIMARY KEY, name TEXT,"
            " trigger TEXT, enabled INTEGER, state TEXT, timeout INTEGER)"
        )
        for automation_id, name, trigger, enabled, state in automations:
            conn.execute(
                "INSERT INTO automations VALUES (?, ?, ?, ?, ?, ?)",
                (automation_id, name, trigger, enabled, state, 300),
            )
        conn.execute(
            "CREATE TABLE automation_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " automation_id TEXT, status TEXT, error_detail TEXT,"
            " started_at TEXT, completed_at TEXT, conversation_id TEXT,"
            " created_at TEXT)"
        )
        for automation_id, status, error, started, conversation_id in runs:
            conn.execute(
                "INSERT INTO automation_runs"
                " (automation_id, status, error_detail, started_at, completed_at,"
                "  conversation_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    automation_id,
                    status,
                    error,
                    started,
                    started,
                    conversation_id,
                    started,
                ),
            )
        conn.commit()
        conn.close()

    def test_event_automation_is_not_watched(self):
        runs = [
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        event_trigger = json.dumps(
            {"type": "event", "source": "github-events", "on": "*"}
        )
        self._make_db_with_triggers(
            [(self.AID, "Mention Responder", event_trigger, 1, "ACTIVE")], runs
        )
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_scope_limits_watching(self):
        self.module.CONFIG["scope"] = ["memory curator"]
        runs = [
            ("id-1", "FAILED", "boom", self.ts(hours_ago=2), ""),
            ("id-1", "FAILED", "boom", self.ts(hours_ago=1), ""),
            ("id-2", "FAILED", "boom", self.ts(hours_ago=2), ""),
            ("id-2", "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        automations = [
            ("id-1", "Memory Curator", "0 21 * * *", 1, "ACTIVE"),
            ("id-2", "PR Reviewer", "0 * * * *", 1, "ACTIVE"),
        ]
        self.make_db(automations, runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        self.assertIn("Memory Curator", self.slack.posts[0]["text"])
        self.assertNotIn("PR Reviewer", self.slack.posts[0]["text"])

    def test_exclude_removes_from_scope(self):
        self.module.CONFIG["exclude"] = ["PR Reviewer"]
        runs = [
            ("id-1", "FAILED", "boom", self.ts(hours_ago=2), ""),
            ("id-1", "FAILED", "boom", self.ts(hours_ago=1), ""),
            ("id-2", "FAILED", "boom", self.ts(hours_ago=2), ""),
            ("id-2", "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        automations = [
            ("id-1", "Memory Curator", "0 21 * * *", 1, "ACTIVE"),
            ("id-2", "PR Reviewer", "0 * * * *", 1, "ACTIVE"),
        ]
        self.make_db(automations, runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        self.assertIn("Memory Curator", self.slack.posts[0]["text"])

    def test_watcher_never_watches_itself(self):
        self.module.automation_id = lambda: "self-id"
        runs = [
            ("self-id", "FAILED", "boom", self.ts(hours_ago=2), ""),
            ("self-id", "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        self.make_db(
            [
                (
                    "self-id",
                    "Automation Flip Watcher (OSS automations)",
                    "15 * * * *",
                    1,
                    "ACTIVE",
                )
            ],
            runs,
        )
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_watcher_name_excluded_even_without_self_id(self):
        # A manual run has no event payload; the name check still excludes it.
        runs = [
            ("id-9", "FAILED", "boom", self.ts(hours_ago=2), ""),
            ("id-9", "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        self.make_db(
            [
                (
                    "id-9",
                    "Automation Flip Watcher (OSS automations)",
                    "15 * * * *",
                    1,
                    "ACTIVE",
                )
            ],
            runs,
        )
        self.run_main()
        self.assertEqual(self.slack.posts, [])


class HeartbeatTest(WorkerTestBase):
    AID = "dddddddd-0000-0000-0000-000000000004"

    def setUp(self):
        super().setUp()
        self.module.CONFIG["heartbeat_enabled"] = True
        self.module.CONFIG["heartbeat_hour_utc"] = 13  # matches self.now

    def test_heartbeat_posts_once_at_configured_hour(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "Memory Curator", "0 21 * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)
        self.assertIn("1/1 watched automations healthy", self.slack.posts[0]["text"])
        self.assertIn("Automation uptime — 2026-11-05", self.slack.posts[0]["text"])

    def test_heartbeat_posts_once_per_day(self):
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "Memory Curator", "0 21 * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.run_main()
        self.assertEqual(len(self.slack.posts), 1)

    def test_no_heartbeat_outside_configured_hour(self):
        self.module.CONFIG["heartbeat_hour_utc"] = 8
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "Memory Curator", "0 21 * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(self.slack.posts, [])

    def test_heartbeat_lists_unhealthy_automations(self):
        runs = [
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        self.assertEqual(len(self.slack.posts), 2)
        self.assertIn("observed on first watch", self.slack.posts[0]["text"])
        self.assertIn("0/1 watched automations healthy", self.slack.posts[1]["text"])
        self.assertIn("PR Reviewer: failing", self.slack.posts[1]["text"])

    def test_heartbeat_lists_unknown_automations(self):
        runs = [
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
            (self.AID, "FAILED", "boom", self.ts(hours_ago=1), ""),
        ]
        automations = [
            (self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE"),
            ("id-2", "Weekly Review", "0 0 * * 5", 1, "ACTIVE"),
        ]
        self.make_db(automations, runs)
        self.run_main()
        heartbeat = self.slack.posts[-1]
        self.assertIn("Weekly Review: no runs in the watch window", heartbeat["text"])

    def test_heartbeat_flags_runs_without_streak(self):
        runs = [
            (self.AID, "FAILED", "boom", self.ts(hours_ago=2), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), ""),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        heartbeat = self.slack.posts[-1]
        self.assertIn(
            "PR Reviewer: runs seen but no healthy/failing streak yet",
            heartbeat["text"],
        )

    def test_heartbeat_singular_failed_run(self):
        # A silent failure leaves status=failing with a streak of one.
        conv_id = "4f0a6c90-1111-2222-3333-444444444444"
        conv_dir = (
            Path(self.module.CONFIG["conversations_dir"]) / conv_id.replace("-", "")
        )
        conv_dir.mkdir(parents=True)
        (conv_dir / "base_state.json").write_text(
            json.dumps({"execution_status": "error"})
        )
        self.module.CONFIG["silent_failure_check"] = True
        runs = [
            (self.AID, "COMPLETED", "", self.ts(hours_ago=3), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=2), ""),
            (self.AID, "COMPLETED", "", self.ts(hours_ago=1), conv_id),
        ]
        self.make_db([(self.AID, "PR Reviewer", "0 * * * *", 1, "ACTIVE")], runs)
        self.run_main()
        heartbeat = self.slack.posts[-1]
        self.assertIn("PR Reviewer: failing", heartbeat["text"])
        self.assertIn("(1 failed run)", heartbeat["text"])


class CronEstimateTest(WorkerTestBase):
    def test_hourly(self):
        self.assertEqual(self.module.cron_interval_seconds("0 * * * *"), 3600)

    def test_every_15_minutes(self):
        self.assertEqual(self.module.cron_interval_seconds("*/15 * * * *"), 15 * 60)

    def test_twice_a_day(self):
        self.assertEqual(self.module.cron_interval_seconds("0 0,12 * * *"), 43200)

    def test_weekly(self):
        self.assertEqual(self.module.cron_interval_seconds("0 0 * * 5"), 7 * 86400)

    def test_weekday_schedule_is_weekly_estimate(self):
        self.assertEqual(self.module.cron_interval_seconds("0 12 * * 1-5"), 7 * 86400)

    def test_irregular_day_fields_return_none(self):
        self.assertIsNone(self.module.cron_interval_seconds("0 0 1,15 * 1"))

    def test_garbage_returns_none(self):
        self.assertIsNone(self.module.cron_interval_seconds("not a cron"))


class MirrorTest(unittest.TestCase):
    def test_deployed_tarball_matches_plugin_scripts(self):
        plugin = (
            ROOT / "plugins/automation-flip-watcher/scripts/worker.py"
        ).read_bytes()
        for tarball in ROOT.glob(
            "automations/automation-flip-watcher-*/tarball/worker.py"
        ):
            self.assertEqual(tarball.read_bytes(), plugin, str(tarball))

    def test_plugin_manifest_is_valid(self):
        manifest = json.loads(
            (ROOT / "plugins/automation-flip-watcher/.plugin/plugin.json").read_text()
        )
        self.assertEqual(
            manifest["automation"]["schema_version"], "openhands.automation.plugin.v1"
        )
        entrypoint = manifest["automation"]["entrypoint"]
        self.assertEqual(entrypoint["runner"], "script")
        self.assertEqual(entrypoint["command"], "python3 worker.py")
        self.assertEqual(
            manifest["automation"]["suggested_trigger"]["schedule"], "15 * * * *"
        )
        schema = manifest["automation"]["config_schema"]
        self.assertIs(schema["additionalProperties"], False)
        self.assertIn("fail_threshold", schema["properties"])


# Run every behavioral test against both copies of the worker.
def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    base_classes = [
        ConfigTest,
        FlipDetectionTest,
        SilentFailureTest,
        StaleTest,
        ScopeTest,
        HeartbeatTest,
        CronEstimateTest,
    ]
    for base in base_classes:
        for index in range(len(MODULES)):
            name = f"{base.__name__}_copy{index}"
            variant = type(name, (base,), {"MODULE_INDEX": index})
            suite.addTests(loader.loadTestsFromTestCase(variant))
    suite.addTests(loader.loadTestsFromTestCase(MirrorTest))
    return suite