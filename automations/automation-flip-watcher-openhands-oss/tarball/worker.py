"""Automation Flip Watcher — fast-path uptime alerts for the OSS automation VM.

The weekly Automation & Conversation Health Review gives the high-level overview
of the various smaller issues, but it only runs once a week. This watcher covers
the other half: when a watched automation flips from consistently passing to
consistently failing, a Slack alert goes out within the hour instead of at the
next Friday review.

The worker is deterministic — no LLM. It reads run history from the automation
service's SQLite database (read-only), keeps per-automation streak state in the
automation KV store, and posts to Slack only when something needs a human:

  * flip      — a healthy automation goes failing (`fail_threshold` consecutive
                failed runs right after a healthy baseline)
  * observed  — first watch of an automation that is already failing
  * silent    — a run reports COMPLETED while its conversation ended in
                error/stuck, the success/failure blind spot the weekly review
                calls `silent:completed-null-conversation-id`
  * stale     — an enabled automation in scope produced no run for far longer
                than its cron cadence
  * recovery  — an alerted automation is back to green
  * heartbeat — a daily one-line uptime summary

Guardrails: read-only against the VM (SQLite opened `mode=ro`, conversation
files read-only); the only writes are this automation's KV state and Slack
messages. It never modifies, enables, disables, or dispatches anything, and it
never watches itself.
"""

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HTTP_TIMEOUT = 30

DEFAULT_DB_PATH = "/var/lib/agent-canvas/.openhands/automation/automations.db"
DEFAULT_CONVERSATIONS_DIR = (
    "/var/lib/agent-canvas/.openhands/agent-canvas/dev_conversations"
)
DEFAULT_CANVAS_URL = "https://oss-agent-canvas.ngrok.dev"
DEFAULT_ALERT_CHANNEL = "C0BGK8NRYUX"
DEFAULT_SLACK_SECRET = "SLACK_BOT_TOKEN"
WATCHER_NAME = "automation flip watcher (oss automations)"

# Run statuses that count as outcomes; anything else (PENDING, RUNNING,
# SKIPPED, or a status this watcher does not know) is not an outcome.
PASS_STATUSES = {"COMPLETED"}
FAIL_STATUSES = {"FAILED", "CANCELLED"}

MAX_ERROR_CHARS = 300


# ── Configuration ─────────────────────────────────────────────────────────────

_CONFIG_TYPES = {
    "scope": list,
    "exclude": list,
    "fail_threshold": int,
    "healthy_streak": int,
    "watch_window_hours": int,
    "realert_hours": int,
    "alert_channel": str,
    "slack_token_secret": str,
    "canvas_url": str,
    "db_path": str,
    "conversations_dir": str,
    "heartbeat_enabled": bool,
    "heartbeat_hour_utc": int,
    "silent_failure_check": bool,
    "stale_check_enabled": bool,
}

DEFAULT_CONFIG = {
    "scope": [],
    "exclude": [],
    "fail_threshold": 2,
    "healthy_streak": 2,
    "watch_window_hours": 48,
    "realert_hours": 24,
    "alert_channel": DEFAULT_ALERT_CHANNEL,
    "slack_token_secret": DEFAULT_SLACK_SECRET,
    "canvas_url": DEFAULT_CANVAS_URL,
    "db_path": DEFAULT_DB_PATH,
    "conversations_dir": DEFAULT_CONVERSATIONS_DIR,
    "heartbeat_enabled": True,
    "heartbeat_hour_utc": 8,
    "silent_failure_check": True,
    "stale_check_enabled": True,
}


def load_config(directory=None):
    """Return the rendered config beside this script, or the defaults."""
    path = (directory or Path(__file__).resolve().parent) / (
        os.environ.get("AUTOMATION_CONFIG_PATH") or "config.json"
    )
    if not path.is_file():
        return dict(DEFAULT_CONFIG)
    raw = json.loads(path.read_text())
    config = dict(DEFAULT_CONFIG)
    for key, expected in _CONFIG_TYPES.items():
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, expected) or (
            expected is not bool and isinstance(value, bool)
        ):
            raise SystemExit(
                f"config.json: {key} must be {expected.__name__}, "
                f"got {type(value).__name__}"
            )
        config[key] = value
    if config["fail_threshold"] < 1:
        raise SystemExit("config.json: fail_threshold must be at least 1")
    if config["healthy_streak"] < 1:
        raise SystemExit("config.json: healthy_streak must be at least 1")
    if config["watch_window_hours"] < 1:
        raise SystemExit("config.json: watch_window_hours must be at least 1")
    if not 0 <= config["heartbeat_hour_utc"] <= 23:
        raise SystemExit("config.json: heartbeat_hour_utc must be 0-23")
    if not config["alert_channel"]:
        raise SystemExit("config.json: alert_channel must not be empty")
    return config


CONFIG = load_config()


# ── Small helpers ─────────────────────────────────────────────────────────────

def utcnow():
    return datetime.now(timezone.utc)


def parse_ts(value):
    """Parse a timestamp from the runs table into an aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if text.endswith(" UTC"):
        text = text[: -len(" UTC")]
    text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def one_line(text, limit=MAX_ERROR_CHARS):
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "…"
    return collapsed


# ── Run environment ───────────────────────────────────────────────────────────

def automation_id() -> str:
    """This watcher's own automation id, when running as an automation."""
    try:
        payload = json.loads(os.environ.get("AUTOMATION_EVENT_PAYLOAD", "{}"))
    except json.JSONDecodeError:
        payload = {}
    return str(payload.get("automation_id") or "")


def dry_run() -> bool:
    return os.environ.get("FLIP_WATCHER_DRY_RUN", "") not in ("", "0", "false")


def read_secret(name: str) -> str:
    """Read a named secret: environment first, then the agent server."""
    value = os.environ.get(name)
    if value:
        return value
    agent_url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    if not agent_url:
        raise RuntimeError(f"secret {name} unavailable: no env var and no agent server")
    key = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")
    req = urllib.request.Request(
        f"{agent_url}/api/settings/secrets/{name}",
        headers={"X-Session-API-Key": key},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode().strip()


def report_failure(exc):
    """Report a failed run to Automations (best effort), then return."""
    url = os.environ.get("AUTOMATION_CALLBACK_URL")
    if not url:
        return
    body = {
        "status": "FAILED",
        "run_id": os.environ.get("AUTOMATION_RUN_ID", ""),
        "error": str(exc),
    }
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": (
                        "Bearer "
                        + (
                            os.environ.get("AUTOMATION_CALLBACK_API_KEY")
                            or os.environ.get("OPENHANDS_API_KEY", "")
                        )
                    ),
                },
            ),
            timeout=HTTP_TIMEOUT,
        )
    except Exception as callback_exc:
        print(f"Callback error (non-fatal): {callback_exc}")


# ── State persistence (KV store with local-file fallback) ────────────────────

_KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
_KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")
_STATE_KEY = "state"


def _kv_available() -> bool:
    return bool(_KV_TOKEN and _KV_BASE)


def _kv_get(key: str):
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{urllib.parse.quote(key, safe='')}",
        headers={"Authorization": f"Bearer {_KV_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read())["value"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _kv_set(key: str, value) -> None:
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{urllib.parse.quote(key, safe='')}",
        data=json.dumps(value).encode(),
        headers={
            "Authorization": f"Bearer {_KV_TOKEN}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        r.read()


def _state_file_path() -> Path:
    workspace_base = os.environ.get("WORKSPACE_BASE", "")
    if workspace_base:
        root = Path(workspace_base).resolve().parent.parent
    else:
        root = Path.home() / ".openhands" / "workspaces"
    state_dir = root / "automation-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"automation_flip_watcher_{automation_id() or 'default'}.json"


def _default_state() -> dict:
    return {"version": 1, "automations": {}, "heartbeat_date": ""}


def load_state() -> dict:
    if _kv_available():
        data = _kv_get(_STATE_KEY)
        if data is not None:
            print("State loaded from KV store")
            return data
        return _default_state()
    path = _state_file_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: state file {path} unreadable ({exc}); starting fresh")
    return _default_state()


def save_state(state: dict) -> None:
    if _kv_available():
        _kv_set(_STATE_KEY, state)
        print("State saved to KV store")
        return
    path = _state_file_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)
    print(f"State saved to {path}")


# ── Slack ─────────────────────────────────────────────────────────────────────

def slack_api(token: str, method: str, payload: dict) -> dict:
    url = f"https://slack.com/api/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        result = json.load(r)
    if not result.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {result.get('error')}")
    return result


def post_message(token: str, channel: str, text: str, thread_ts: str | None = None):
    """Post to Slack; returns the new message's ts, or None in dry-run mode."""
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if dry_run():
        print(f"[dry-run] would post to {channel} (thread {thread_ts}):\n{text}\n")
        return None
    return slack_api(token, "chat.postMessage", payload).get("ts")


# ── Runs database (read-only) ────────────────────────────────────────────────

def open_db(path: str):
    uri = Path(path).absolute().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_automations(conn):
    rows = conn.execute(
        "SELECT id, name, trigger, enabled, state, timeout FROM automations"
    ).fetchall()
    automations = []
    for row in rows:
        try:
            trigger = json.loads(row["trigger"] or "{}")
        except (json.JSONDecodeError, TypeError):
            trigger = {}
        automations.append(
            {
                "id": str(row["id"]),
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "state": row["state"],
                "trigger": trigger,
            }
        )
    return automations


def fetch_runs(conn, window_start: datetime):
    rows = conn.execute(
        "SELECT automation_id, status, error_detail, started_at, completed_at,"
        " conversation_id FROM automation_runs"
        " WHERE COALESCE(started_at, created_at) >= ?"
        " ORDER BY COALESCE(started_at, created_at) ASC",
        (window_start.isoformat(),),
    ).fetchall()
    runs = {}
    for row in rows:
        started = parse_ts(row["started_at"]) or window_start
        runs.setdefault(str(row["automation_id"]), []).append(
            {
                "status": str(row["status"] or "").upper(),
                "error_detail": row["error_detail"] or "",
                "started_at": started,
                "completed_at": parse_ts(row["completed_at"]),
                "conversation_id": row["conversation_id"] or "",
            }
        )
    return runs


# ── Cron cadence estimation (for the stale check) ────────────────────────────

def cron_field_values(field: str, low: int, high: int):
    """Return the set of values one cron field matches, or None for `*`."""
    field = field.strip()
    if field == "*":
        return None
    values = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_raw = part.split("/", 1)
            try:
                step = int(step_raw)
            except ValueError:
                return None
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            bounds = part.split("-", 1)
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                return None
        else:
            try:
                start = end = int(part)
            except ValueError:
                return None
        values.update(range(start, end + 1, step))
    return values or None


def _field_gaps(values, span):
    """Smallest gap between consecutive firings of one field's values.

    `span` is the field's period in the same units as the values; the wrap
    gap covers the step across the boundary (e.g. `0 0,12` fires 12h apart
    in both directions).
    """
    ordered = sorted(values)
    gaps = [later - earlier for earlier, later in zip(ordered, ordered[1:])]
    gaps.append(span - (ordered[-1] - ordered[0]) if len(ordered) > 1 else span)
    return min(gaps)


def cron_interval_seconds(expression: str):
    """Estimate a cron schedule's cadence, or None when too irregular.

    A rough estimate is enough: the stale threshold is the cadence doubled.
    """
    fields = expression.split()
    if len(fields) != 5:
        return None
    minutes = cron_field_values(fields[0], 0, 59)
    hours = cron_field_values(fields[1], 0, 23)
    dom = cron_field_values(fields[2], 1, 31)
    mon = cron_field_values(fields[3], 1, 12)
    dow = cron_field_values(fields[4], 0, 6)
    if dom is not None and dow is not None:
        return None  # both day fields restricted — too irregular to estimate
    if dom is not None or mon is not None or dow is not None:
        # Day-restricted schedules (e.g. weekdays) — treat as weekly.
        return 7 * 86400
    if hours is None:
        # Fires every hour; the cadence comes from the minute field.
        if minutes is None:
            return 3600
        return _field_gaps(minutes, 60) * 60
    return _field_gaps(hours, 24) * 3600


# ── Conversation silent-failure check ─────────────────────────────────────────

def silent_failure(run: dict, conversations_dir: str) -> bool:
    """Whether a COMPLETED run's conversation actually ended error/stuck."""
    conversation_id = run.get("conversation_id")
    if not conversation_id:
        return False
    base_state = (
        Path(conversations_dir)
        / str(conversation_id).replace("-", "")
        / "base_state.json"
    )
    if not base_state.is_file():
        return False
    try:
        data = json.loads(base_state.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    status = str((data or {}).get("execution_status") or "").lower()
    return status in ("error", "stuck")


# ── Per-automation evaluation ─────────────────────────────────────────────────

def trailing_streak(outcomes):
    """The last outcome kind and how many consecutive times it repeats."""
    if not outcomes:
        return "none", 0
    last = outcomes[-1]
    count = 0
    for outcome in reversed(outcomes):
        if outcome != last:
            break
        count += 1
    return last, count


def conversation_link(conversation_id: str, canvas_url: str):
    if not conversation_id:
        return None
    return f"{canvas_url.rstrip('/')}/conversations/{conversation_id}"


def evaluate(name, runs, record, now, config):
    """Compute one automation's new state and any action worth taking.

    `runs` is the window's ordered run list (oldest first); `record` is the
    persisted per-automation state, mutated in place. Returns an action dict
    or None.
    """
    fail_threshold = config["fail_threshold"]
    healthy_streak = config["healthy_streak"]

    outcomes = []
    outcome_runs = []
    for run in runs:
        if run["status"] in PASS_STATUSES:
            outcomes.append("pass")
            outcome_runs.append(run)
        elif run["status"] in FAIL_STATUSES:
            outcomes.append("fail")
            outcome_runs.append(run)
        # anything else (PENDING, RUNNING, SKIPPED, unknown) is not an outcome

    last_kind, last_count = trailing_streak(outcomes)
    prior_status = record.get("status", "unknown")
    in_alert = bool(record.get("in_alert"))
    action = None

    # The blind-spot check: trailing outcomes can look healthy while the
    # newest COMPLETED run's conversation actually errored.
    silent_run = None
    if config["silent_failure_check"] and last_kind == "pass":
        completed = [r for r in runs if r["status"] in PASS_STATUSES]
        if completed:
            newest = completed[-1]
            if silent_failure(newest, config["conversations_dir"]):
                fingerprint = newest["conversation_id"]
                outcomes[-1] = "silent_fail"
                last_kind, last_count = trailing_streak(outcomes)
                silent_run = newest
                record["silent_fingerprint"] = fingerprint

    if last_kind == "fail" and last_count >= fail_threshold:
        first_fail = outcome_runs[-last_count]["started_at"]
        last_fail = outcome_runs[-1]
        record["fail_streak"] = last_count
        record["pass_streak"] = 0
        record["status"] = "failing"
        # A flip needs evidence the automation was consistently passing: a
        # healthy baseline from earlier runs, or a healthy streak right
        # before the failures inside this window (first watch included).
        prefix_passes = 0
        for outcome in reversed(outcomes[:-last_count] or []):
            if outcome != "pass":
                break
            prefix_passes += 1
        was_healthy = prior_status == "healthy" or prefix_passes >= healthy_streak
        if not in_alert:
            record["in_alert"] = True
            record["first_alert_at"] = iso(now)
            record["last_alert_at"] = iso(now)
            action = {
                "kind": "flip" if was_healthy else "observed",
                "name": name,
                "fail_count": last_count,
                "since": first_fail,
                "last_run": last_fail,
            }
        else:
            last_alert = parse_ts(record.get("last_alert_at"))
            if last_alert is None or now - last_alert >= timedelta(
                hours=config["realert_hours"]
            ):
                record["last_alert_at"] = iso(now)
                action = {
                    "kind": "realert",
                    "name": name,
                    "fail_count": last_count,
                    "since": record.get("first_alert_at"),
                    "last_run": last_fail,
                }
    elif silent_run is not None:
        record["status"] = "failing"
        record["fail_streak"] = last_count
        already_alerted = record.get("silent_alerted_fingerprint")
        if not in_alert:
            record["in_alert"] = True
            record["first_alert_at"] = iso(now)
            record["last_alert_at"] = iso(now)
            record["silent_alerted_fingerprint"] = record.get("silent_fingerprint")
            action = {"kind": "silent", "name": name, "last_run": silent_run}
        elif already_alerted != record.get("silent_fingerprint"):
            record["last_alert_at"] = iso(now)
            record["silent_alerted_fingerprint"] = record.get("silent_fingerprint")
            action = {"kind": "silent", "name": name, "last_run": silent_run}
    elif last_kind == "pass" and last_count >= healthy_streak:
        record["status"] = "healthy"
        record["pass_streak"] = last_count
        record["fail_streak"] = 0
        record.pop("stale_alerted", None)
        if in_alert:
            record["in_alert"] = False
            action = {"kind": "recovery", "name": name, "pass_count": last_count}
    else:
        # Not enough trailing outcomes to declare either way; keep the
        # previous status so a sparse automation doesn't thrash alerts.
        record["pass_streak"] = last_count if last_kind == "pass" else 0
        record["fail_streak"] = last_count if last_kind == "fail" else 0
        record["status"] = prior_status

    if action is None and config["stale_check_enabled"]:
        action = stale_action(name, runs, record, now, config)
    return action


def stale_action(name, runs, record, now, config):
    trigger = record.get("trigger") or {}
    if (trigger.get("type") or "cron") != "cron":
        return None
    interval = cron_interval_seconds(trigger.get("schedule", ""))
    if interval is None:
        return None
    threshold = max(2 * interval, 2 * 3600)
    if any(r["status"] in ("PENDING", "RUNNING") for r in runs):
        return None
    last_started = max(
        (r["started_at"] for r in runs if r["started_at"]), default=None
    )
    if last_started is None or now - last_started < timedelta(seconds=threshold):
        return None
    if record.get("stale_alerted"):
        return None
    record["stale_alerted"] = True
    return {
        "kind": "stale",
        "name": name,
        "last_started": last_started,
        "interval": interval,
    }


# ── Alert formatting ──────────────────────────────────────────────────────────

DISCLOSURE = "_Sent by OpenHands on behalf of the user._"


def format_alert(action, config):
    name = action["name"]
    kind = action["kind"]
    if kind == "flip":
        title = f"*⚠️ Automation Flip Watcher — {name} flipped to failing*"
        since = action.get("since")
        lines = [
            f"• {action['fail_count']} consecutive failed runs"
            + (f" since {iso(since)}" if since else "")
        ]
    elif kind == "observed":
        title = (
            f"*⚠️ Automation Flip Watcher — {name} is failing"
            " (observed on first watch)*"
        )
        lines = [f"• {action['fail_count']} consecutive failed runs in the window"]
    elif kind == "realert":
        title = f"*⚠️ Automation Flip Watcher — {name} is still failing*"
        lines = [
            f"• {action['fail_count']} failed runs since first alert"
            + (f" ({action['since']})" if action.get("since") else "")
        ]
    elif kind == "silent":
        title = f"*⚠️ Automation Flip Watcher — {name} completed but actually failed*"
        lines = [
            "• run reported COMPLETED while its conversation ended in error/stuck"
        ]
    elif kind == "stale":
        title = f"*⚠️ Automation Flip Watcher — {name} has not run*"
        hours = round(action["interval"] / 3600, 1)
        lines = [
            f"• no run for far longer than its cadence (~every {hours}h);"
            f" last run {iso(action['last_started'])}"
        ]
    else:
        title = f"*⚠️ Automation Flip Watcher — {name}*"
        lines = []
    last_run = action.get("last_run")
    if last_run:
        if last_run.get("error_detail"):
            lines.append(f"• last error: `{one_line(last_run['error_detail'])}`")
        detail = []
        if last_run.get("started_at"):
            detail.append(f"last run {iso(last_run['started_at'])}")
        if last_run.get("status"):
            detail.append(f"status {last_run['status']}")
        link = conversation_link(
            last_run.get("conversation_id", ""), config["canvas_url"]
        )
        if link:
            detail.append(f"<{link}|conversation>")
        if detail:
            lines.append("• " + " — ".join(detail))
    lines.append("Reply to this message to trigger an investigation.")
    lines.append(DISCLOSURE)
    return title + "\n" + "\n".join(lines)


def format_recovery(action):
    return (
        f"*✅ Automation Flip Watcher — {action['name']} is back to green*\n"
        f"• {action['pass_count']} consecutive passing runs\n" + DISCLOSURE
    )


def format_heartbeat(now, rows):
    healthy = sum(1 for row in rows if row["status"] == "healthy")
    lines = [
        f"*Automation uptime — {now.strftime('%Y-%m-%d')}*",
        f"• {healthy}/{len(rows)} watched automations healthy",
    ]
    for row in rows:
        if row["status"] == "healthy":
            continue
        if row["status"] == "failing":
            since = row.get("first_alert_at")
            streak = row.get("fail_streak") or 0
            lines.append(
                f"• {row['name']}: failing"
                + (f" since {since}" if since else "")
                + f" ({streak} failed run{'s' if streak != 1 else ''})"
            )
        elif row.get("has_runs"):
            lines.append(f"• {row['name']}: runs seen but no healthy/failing streak yet")
        else:
            lines.append(f"• {row['name']}: no runs in the watch window")
    lines.append(DISCLOSURE)
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config = CONFIG
    now = utcnow()
    window_start = now - timedelta(hours=config["watch_window_hours"])
    self_id = automation_id()

    conn = open_db(config["db_path"])
    try:
        automations = fetch_automations(conn)
        runs = fetch_runs(conn, window_start)
    finally:
        conn.close()

    scope = {entry.strip().lower() for entry in config["scope"]}
    exclude = {entry.strip().lower() for entry in config["exclude"]}
    exclude.add(WATCHER_NAME)

    watched = []
    for automation in automations:
        name = automation["name"]
        if scope and name.strip().lower() not in scope:
            continue
        if name.strip().lower() in exclude:
            continue
        if automation["id"] == self_id:
            continue
        if not automation["enabled"] or automation["state"] not in (None, "ACTIVE"):
            continue
        if (automation["trigger"] or {}).get("type") not in (None, "cron"):
            continue
        watched.append(automation)

    if not watched:
        print("No automations in scope; nothing to do.")
        return

    state = load_state()
    records = state.setdefault("automations", {})
    actions = []
    rows = []

    for automation in watched:
        name = automation["name"]
        record = records.setdefault(name, {})
        record["trigger"] = automation["trigger"]
        automation_runs = runs.get(automation["id"], [])
        action = evaluate(name, automation_runs, record, now, config)
        rows.append(
            {
                "name": name,
                "status": record.get("status", "unknown"),
                "fail_streak": record.get("fail_streak", 0),
                "first_alert_at": record.get("first_alert_at"),
                "has_runs": bool(automation_runs),
            }
        )
        if action:
            actions.append(action)

    heartbeat_due = (
        config["heartbeat_enabled"]
        and now.hour == config["heartbeat_hour_utc"]
        and state.get("heartbeat_date") != now.strftime("%Y-%m-%d")
    )
    if heartbeat_due and rows:
        actions.append({"kind": "heartbeat", "rows": rows})

    if not actions:
        print(f"{len(watched)} automations watched; no alerts.")
        save_state(state)
        return

    token = read_secret(config["slack_token_secret"])
    for action in actions:
        kind = action["kind"]
        if kind == "heartbeat":
            text = format_heartbeat(now, action["rows"])
            message_ts = post_message(token, config["alert_channel"], text)
            if message_ts:
                state["heartbeat_date"] = now.strftime("%Y-%m-%d")
            continue
        record = records[action["name"]]
        if kind == "recovery":
            text = format_recovery(action)
            message_ts = post_message(
                token, config["alert_channel"], text, record.get("thread_ts")
            )
            if message_ts:
                record.pop("thread_ts", None)
            continue
        text = format_alert(action, config)
        if kind == "realert":
            post_message(
                token, config["alert_channel"], text, record.get("thread_ts")
            )
        else:
            message_ts = post_message(token, config["alert_channel"], text)
            if message_ts:
                record["thread_ts"] = message_ts

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        report_failure(exc)
        raise