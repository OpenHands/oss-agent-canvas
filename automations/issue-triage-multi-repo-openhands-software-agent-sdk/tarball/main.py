"""Reliable GitHub issue triage for OpenHands repositories.

Multi-repo and event-driven: one run per `issues` webhook delivery, no cron.
Each run sweeps the whole 48h window for the delivering repo, so it does not
matter which issue the event named -- any qualifying delivery re-evaluates
everything outstanding. That is also why the trigger subscribes to `opened`,
`reopened` and `unlabeled` but deliberately NOT `labeled`: the triage agent
applies labels itself, and `labeled` would make every triage re-trigger this
automation once per label applied.

Deliveries are not deduplicated or retried by the service, so an event that
is lost costs latency, not coverage -- the next issue event in that repo
picks the work up. Applied labels are the real state: `needs_triage` reads
them from GitHub, so a re-run after lost KV state simply finds nothing to do.

REPO below is the repo currently being processed, set once per repo in
main(); every helper reads it, which keeps the prompt and API paths pointed
at the right repository. State and lock keys are per-repo so the two repos
cannot clobber each other inside this automation's single KV namespace.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

REPOS = [
    "OpenHands/OpenHands",
    "OpenHands/software-agent-sdk",
]
# The repo currently being processed. Set per repo in main(); read by the
# helpers below so their GitHub paths and the triage prompt stay in step.
REPO = ""
RECONCILE_HOURS = 48
MAX_STARTS_PER_RUN = 3
MAX_ATTEMPTS = 4
RETRY_DELAYS = (300, 900, 3600, 10800)
MAX_RECORDS = 5000
TERMINAL_STATUSES = {"finished", "idle", "error", "stuck", "stopped"}
STATE_KEY_PREFIX = "state-v3"
LOCK_KEY_PREFIX = "run-lock-v3"
LOCK_TTL_SECONDS = 300


def repo_slug():
    return REPO.replace("/", "__")


def state_key():
    return f"{STATE_KEY_PREFIX}:{repo_slug()}"


def lock_key():
    return f"{LOCK_KEY_PREFIX}:{repo_slug()}"


def env_key():
    return os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0") or ""


def get_secret(name):
    base = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    req = urllib.request.Request(
        f"{base}/api/settings/secrets/{name}",
        headers={"X-Session-API-Key": env_key()},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode().strip()


def fire_callback(status="COMPLETED", error=None, conversation_id=None):
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    if conversation_id:
        body["conversation_id"] = conversation_id
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as exc:
        print(f"Callback error (non-fatal): {exc}")


KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")


def kv_available():
    return bool(KV_TOKEN and KV_BASE)


def kv_get(key):
    req = urllib.request.Request(
        f"{KV_BASE}/v1/kv/{key}", headers={"Authorization": f"Bearer {KV_TOKEN}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)["value"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def kv_set(key, value, nx=False):
    url = f"{KV_BASE}/v1/kv/{key}"
    if nx:
        url += "?nx=true"
    req = urllib.request.Request(
        url,
        data=json.dumps(value).encode(),
        headers={
            "Authorization": f"Bearer {KV_TOKEN}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    urllib.request.urlopen(req, timeout=30).read()


def acquire_lock(now):
    if not kv_available():
        return True
    lock = kv_get(lock_key())
    if lock:
        expires_at = datetime.fromisoformat(lock["expires_at"])
        if expires_at > now:
            return False
        kv_delete(lock_key())
    try:
        kv_set(
            lock_key(),
            {"run_id": os.environ.get("AUTOMATION_RUN_ID", ""), "expires_at": (now + timedelta(seconds=LOCK_TTL_SECONDS)).isoformat()},
            nx=True,
        )
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return False
        raise


def kv_delete(key):
    req = urllib.request.Request(
        f"{KV_BASE}/v1/kv/{key}",
        headers={"Authorization": f"Bearer {KV_TOKEN}"},
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


def release_lock():
    if kv_available():
        kv_delete(lock_key())


def state_path():
    payload = json.loads(os.environ.get("AUTOMATION_EVENT_PAYLOAD", "{}"))
    automation_id = payload.get("automation_id", "default")
    root = Path(os.environ.get("WORKSPACE_BASE", Path.home() / ".openhands"))
    path = root.parent / "automation-state" / f"issue-triage-v3-{automation_id}-{repo_slug()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def default_state():
    return {"version": 3, "repo": REPO, "issues": {}, "updated_at": None}


def load_state():
    if kv_available():
        return kv_get(state_key()) or default_state()
    path = state_path()
    return json.loads(path.read_text()) if path.exists() else default_state()


def save_state(state):
    state["version"] = 3
    state["repo"] = REPO
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    records = state.setdefault("issues", {})
    if len(records) > MAX_RECORDS:
        keep = sorted(records, key=lambda key: records[key].get("updated_at", ""))[-MAX_RECORDS:]
        state["issues"] = {key: records[key] for key in keep}
    if kv_available():
        kv_set(state_key(), state)
    else:
        path = state_path()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2, sort_keys=True))
        temp.replace(path)


def github_request(token, method, path, params=None, body=None):
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urlencode(params)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw.strip() else {}


def github_token():
    """Used for this poller's own read-only calls (/user, listing open
    issues). Label writes are unaffected: they happen inside the
    spawned conversation, which independently looks up its injected secret
    (see ALLOWED_SECRETS/build_secrets).

    Uses the dedicated GITHUB_TOKEN_ISSUE_TRIAGE secret, falling back to the
    generic GITHUB_TOKEN only if the dedicated secret is not registered, so a
    missing dedicated secret never silently breaks triage."""
    for name in ("GITHUB_TOKEN_ISSUE_TRIAGE", "GITHUB_TOKEN"):
        try:
            token = get_secret(name)
            if token:
                return token
        except Exception:
            pass
    raise RuntimeError("No GitHub token secret is available")


def parse_timestamp(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fetch_recent_open_issues(token, now):
    cutoff = now - timedelta(hours=RECONCILE_HOURS)
    results = []
    for page in range(1, 11):
        items = github_request(
            token,
            "GET",
            f"/repos/{REPO}/issues",
            {"state": "open", "sort": "created", "direction": "desc", "per_page": 100, "page": page},
        )
        if not items:
            break
        reached_cutoff = False
        for issue in items:
            if issue.get("pull_request"):
                continue
            if parse_timestamp(issue["created_at"]) < cutoff:
                reached_cutoff = True
                continue
            results.append(issue)
        if reached_cutoff or len(items) < 100:
            break
    return results


def label_names(issue):
    return {label.get("name", "") for label in issue.get("labels", [])} - {""}


def needs_triage(issue):
    substantive = label_names(issue) - {"needs-triage"}
    priorities = {"priority:low", "priority:medium", "priority:high"}
    return not substantive or ("bug" in substantive and not substantive.intersection(priorities))


def oh_request(method, path, body=None):
    base = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Session-API-Key": env_key(), "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw.strip() else {}


# Secrets injected into the spawned triage conversation. The dedicated
# GITHUB_TOKEN_ISSUE_TRIAGE secret is the token the triage agent should use
# for its label writes; GITHUB_TOKEN is included as a fallback so the spawned
# conversation can still proceed if the dedicated secret is not registered.
ALLOWED_SECRETS = ("GITHUB_TOKEN_ISSUE_TRIAGE", "GITHUB_TOKEN")


def build_secrets():
    return {
        name: {
            "kind": "LookupSecret",
            "url": f"/api/settings/secrets/{name}",
            "headers": {"X-Session-API-Key": env_key()},
        }
        for name in ALLOWED_SECRETS
    }


def build_prompt(issue):
    existing = sorted(label_names(issue))
    return f"""You are an issue triage bot for {REPO}. Apply appropriate existing repository labels to the issue below, silently and conservatively.

Issue: {REPO}#{issue['number']}
Existing labels: {existing}
Title: {issue.get('title', '(no title)')}
Body:
{issue.get('body') or '(no body)'}

Use GITHUB_TOKEN_ISSUE_TRIAGE (a GitHub token) only with https://api.github.com. Fetch the repository's complete existing label set before choosing labels. Apply 1–4 fitting labels using POST /repos/{REPO}/issues/{issue['number']}/labels with additive semantics. Never replace existing labels.

Use `bug` for defects and `enhancement` for feature requests. Prefer useful area labels. Add `release-note-required` only for clear user-facing changes. Add `security` for credential, authentication, sandboxing, or vulnerability concerns. If and only if you apply `bug`, apply exactly one of `priority:low`, `priority:medium`, or `priority:high`: low for minor impact, medium for material but limited impact or a reasonable workaround, high for critical or widespread impact, data/security risk, or no practical workaround.

Do not post comments or reactions. Do not close, modify, assign, pin, or transfer the issue. Do not print secrets. Verify the final labels with GET /repos/{REPO}/issues/{issue['number']} and report the final label names."""


def start_conversation(issue):
    settings_req = urllib.request.Request(
        os.environ.get("AGENT_SERVER_URL", "").rstrip("/") + "/api/settings",
        headers={"X-Session-API-Key": env_key(), "X-Expose-Secrets": "encrypted"},
    )
    with urllib.request.urlopen(settings_req, timeout=30) as response:
        settings = json.load(response)
    agent = dict(settings.get("agent_settings") or {})
    agent.pop("schema_version", None)
    agent.pop("mcp_config", None)
    agent["tools"] = [{"name": "terminal", "params": {}}]
    payload = {
        "secrets_encrypted": True,
        "agent_settings": agent,
        "workspace": {"kind": "LocalWorkspace", "working_dir": os.environ.get("WORKSPACE_BASE", "/workspace")},
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": 100,
        "stuck_detection": True,
        "worktree": False,
        "initial_message": {"role": "user", "content": [{"type": "text", "text": build_prompt(issue)}], "run": True},
    }
    secrets = build_secrets()
    if secrets:
        payload["secrets"] = secrets
    result = oh_request("POST", "/api/conversations", payload)
    if not result.get("id"):
        raise RuntimeError(f"Conversation creation returned no id: {result}")
    return result["id"]


def conversation_status(conversation_id):
    return oh_request("GET", f"/api/conversations/{conversation_id}").get("execution_status", "unknown")


def retry_due(record, now):
    next_retry = record.get("next_retry_at")
    return not next_retry or datetime.fromisoformat(next_retry) <= now


def schedule_retry(record, now, error):
    attempts = int(record.get("attempts", 0))
    record.update({"status": "retryable_failed", "error": error, "conversation_id": None})
    if attempts >= MAX_ATTEMPTS:
        record["status"] = "exhausted"
        record["next_retry_at"] = (now + timedelta(hours=12)).isoformat()
    else:
        record["next_retry_at"] = (now + timedelta(seconds=RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)])).isoformat()


def refresh_outstanding(records, live_issues, now):
    for number, record in records.items():
        if record.get("status") != "conversation_started":
            continue
        issue = live_issues.get(number)
        if issue and not needs_triage(issue):
            record.update({"status": "succeeded", "conversation_id": None, "error": None, "updated_at": now.isoformat()})
            print(f"#{number}: verified triaged with {sorted(label_names(issue))}")
            continue
        try:
            status = conversation_status(record["conversation_id"])
        except Exception as exc:
            print(f"#{number}: status check failed: {exc}")
            continue
        if status in TERMINAL_STATUSES:
            schedule_retry(record, now, f"conversation ended as {status} without verified labels")
            record["updated_at"] = now.isoformat()
            print(f"#{number}: {record['error']}; retry scheduled")


def run_locked(now):
    state = load_state()
    records = state.setdefault("issues", {})
    token = github_token()
    user = github_request(token, "GET", "/user").get("login")
    if not user:
        raise RuntimeError("GitHub token validation failed")
    issues = fetch_recent_open_issues(token, now)
    live = {str(issue["number"]): issue for issue in issues}
    print(f"Authenticated as {user}; reconciling {len(issues)} open issues from the last {RECONCILE_HOURS} hours")

    refresh_outstanding(records, live, now)
    started = []
    for issue in sorted(issues, key=lambda item: item["created_at"]):
        if len(started) >= MAX_STARTS_PER_RUN or not needs_triage(issue):
            continue
        number = str(issue["number"])
        record = records.setdefault(number, {"attempts": 0})
        if record.get("status") in {"conversation_started", "succeeded"}:
            continue
        if not retry_due(record, now):
            continue
        try:
            conversation_id = start_conversation(issue)
            record.update({
                "status": "conversation_started",
                "conversation_id": conversation_id,
                "attempts": int(record.get("attempts", 0)) + 1,
                "error": None,
                "next_retry_at": None,
                "updated_at": now.isoformat(),
            })
            started.append(conversation_id)
            print(f"#{number}: started triage conversation {conversation_id}")
        except Exception as exc:
            record["attempts"] = int(record.get("attempts", 0)) + 1
            schedule_retry(record, now, str(exc))
            record["updated_at"] = now.isoformat()
            print(f"#{number}: failed to start triage: {exc}")

    save_state(state)
    outstanding = sum(record.get("status") == "conversation_started" for record in records.values())
    print(f"Started {len(started)} conversation(s); {outstanding} awaiting verification")
    return started[-1] if started else None


def load_event():
    """The webhook delivery, or None when the run was dispatched by hand.

    AUTOMATION_EVENT_PAYLOAD wraps it as {"event": {"payload": ...}} for a
    custom webhook source.
    """
    raw = os.environ.get("AUTOMATION_EVENT_PAYLOAD")
    if not raw:
        return None
    try:
        outer = json.loads(raw)
    except Exception as exc:
        print(f"Could not parse AUTOMATION_EVENT_PAYLOAD: {exc}")
        return None
    payload = (outer.get("event") or {}).get("payload")
    return payload if isinstance(payload, dict) else None


def repo_for(full_name):
    """Match a payload's repository to one this automation manages. The org
    webhook delivers every repo in the org, so most events are not ours."""
    for repo in REPOS:
        if repo.lower() == (full_name or "").lower():
            return repo
    return None


def process_repo(repo, now):
    global REPO
    REPO = repo
    print(f"\n=== {REPO} ===")
    # The lock is per-repo, so concurrent events for different repos do not
    # block each other; two events for the same repo still serialize.
    if not acquire_lock(now):
        print("Another run holds this repo's lock; skipping this overlapping run")
        return None
    try:
        return run_locked(now)
    finally:
        release_lock()


def main():
    now = datetime.now(timezone.utc)
    payload = load_event()
    if payload is None:
        # Manual dispatch (or a malformed payload): sweep everything, which is
        # also how to drain the backlog after an outage.
        print("No event payload; sweeping every managed repo")
        repos = list(REPOS)
    else:
        full_name = (payload.get("repository") or {}).get("full_name") or ""
        repo = repo_for(full_name)
        if repo is None:
            print(f"Ignoring event for unmanaged repo {full_name!r}")
            return None
        number = (payload.get("issue") or {}).get("number")
        print(f"Event for {full_name}: action={payload.get('action')!r} issue=#{number}")
        repos = [repo]

    last_conversation_id = None
    for repo in repos:
        # A failure on one repo must never stop the other being processed.
        try:
            conversation_id = process_repo(repo, now)
            if conversation_id:
                last_conversation_id = conversation_id
        except Exception as exc:
            print(f"Error processing {repo}: {exc}")
    return last_conversation_id


if __name__ == "__main__":
    try:
        conversation_id = main()
        fire_callback("COMPLETED", conversation_id=conversation_id)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        fire_callback("FAILED", str(exc))
        sys.exit(1)
