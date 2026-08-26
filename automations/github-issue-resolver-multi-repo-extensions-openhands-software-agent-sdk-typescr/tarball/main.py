"""
GitHub Issue Resolver (multi-repo) - OpenHands Automation Script

Event-driven: one run per GitHub webhook delivery, no cron. Two event shapes
drive the two jobs the old 5-minute poll used to do on every tick:

  * `issues` / action `assigned`  -> pick the issue up
  * `push` to `openhands/issue-*` -> the agent pushed, so finalize it

Every run *also* reconciles that repo's in-flight resolutions before handling
its own event. That is what replaces the clock: a resolution that finishes
after its own run has exited is closed out by the next event in that repo,
whatever that event happens to be. A repo that goes completely silent is the
one case where an orphan lingers, which is the accepted trade for dropping the
poll (see `_process_repo`).

Runs are capped at 1800s by the service, so `_wait_for_repo` blocks for at most
INLINE_WAIT_BUDGET and then hands off to that reconcile path rather than being
SIGTERM'd mid-finalize.

The event payload arrives as AUTOMATION_EVENT_PAYLOAD. It is NOT deduplicated
by the service (custom webhooks record `provider_event_id` as NULL), so GitHub
redeliveries reach us as fresh events and every path below stays idempotent
through the KV `seen` / `active` state.

For each issue picked up:

  1. Only issues assigned by a trusted actor (an org member/collaborator on
     this repo) are worked on. If the assigner is not trusted, a plain
     comment explains that a maintainer needs to do the assigning, and this
     specific (issue, assigner) pair is not retried - but if a different,
     trusted person re-assigns the same issue later, that is a fresh attempt.
  2. For a trusted assignment: clone the repo (public, read-only, no auth
     needed), create a branch, and start an AI coding conversation with the
     issue title/body/comments as context. The agent has its own
     GITHUB_TOKEN (via the standard secrets mechanism) to push the branch
     itself once it is done.
  3. Once the conversation reaches a terminal state, the script checks
     GitHub directly (never trusting the agent's own claim) for whether the
     branch was actually pushed with new commits:
       - If yes: open a draft PR referencing "Closes #<issue>".
       - If no: post the agent's final response as a plain issue comment
         (e.g. it needs clarification, could not reproduce it, or decided no
         code change was warranted).
     A conversation that errors, gets stuck, or exceeds the time budget
     posts an explanatory comment instead.

Each repository is processed independently with its own per-repo KV state key
(state-v1:<repo-slug>), so issue numbers and state buckets never collide across
repositories. Any field below can be overridden per-entry in REPOS; omitted
fields fall back to the module-level defaults.

The org webhook feeding this delivers events for *every* repo in the org, so
an event whose repository is not in REPOS is dropped immediately.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── Multi-repo configuration ──────────────────────────────────────────────────
# Each repo is processed independently with its own per-repo KV state key
# (state-v1:<repo-slug>), so issue numbers / assigners / state buckets never
# collide across repositories - exactly like running one automation per repo,
# but from one shared org webhook. Any field below can be overridden
# per-entry; omitted fields fall back to the module-level defaults.
REPOS = [
    {"repo": "OpenHands/extensions"},
    {"repo": "OpenHands/OpenHands"},
    {"repo": "OpenHands/software-agent-sdk"},
    {"repo": "OpenHands/typescript-client"},
]

# Module-level defaults shared by every repo (override per-entry in REPOS).
BOT_USERNAME = "all-hands-bot"
DRAFT_PR = True
BRANCH_PREFIX = "openhands/issue-"
OPENHANDS_URL_DEFAULT = "https://oss-agent-canvas.ngrok.dev"
# Each holds one conversation, one repo checkout, and (if the agent pushes)
# real LLM + compute spend, so this bounds cost and load per repo.
MAX_CONCURRENT_RESOLUTIONS = 2
# Coding + testing + iterating legitimately takes much longer than a review.
MAX_ACTIVE_AGE = 3 * 60 * 60
# Consecutive conversation-creation failures tolerated per (issue, assigner)
# before giving up - without this, an issue whose repo prep always fails
# would be retried every minute forever.
MAX_CREATION_ATTEMPTS = 3
HTTP_TIMEOUT = 30
# Bounds on the KV-stored dedup lists, so state stays well under the KV
# value size limit and old entries can eventually age out.
MAX_SEEN = 500
MAX_EVALUATED_ENTRIES = 500

DONE_DEBOUNCE = 15
CAS_RETRIES = 8
TERMINAL_STATUSES = {"idle", "finished", "error", "stuck"}

# The service caps a run at 1800s and SIGTERMs it there. Wait well inside that
# so finalizing (branch check, PR creation, comments) still has room; anything
# slower is picked up by a later event's reconcile pass instead.
INLINE_WAIT_BUDGET = 1500
INLINE_POLL_INTERVAL = 15
# Branch a push must touch for it to mean "the agent pushed work for an issue".
PUSH_REF_PREFIX = "refs/heads/"

# Per-repo context, set at the start of each repo's processing in main().
# All repo-specific behaviour reads from here so the GitHub calls and CAS
# state act on the correct repository. It is only mutated once per repo
# iteration in main() and read synchronously within that iteration, so there
# is no cross-repo interleaving.
_CURRENT: dict = {}


def _cfg(key: str, default):
    """Read a config value for the current repo, falling back to the
    module-level default when the per-repo entry does not override it."""
    val = _CURRENT.get(key)
    return val if val is not None else default


def _state_key() -> str:
    return f"state-v1:{_CURRENT['repo'].replace('/', '__')}"


def log(*args: object) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]", *args, flush=True)


# ── Incoming event ───────────────────────────────────────────────────────────
# AUTOMATION_EVENT_PAYLOAD wraps the delivery as {"event": {"payload": ...}}
# for a custom webhook source. Absent means the run was dispatched by hand, in
# which case main() falls back to reconciling every repo.


def _load_event() -> dict | None:
    raw = os.environ.get("AUTOMATION_EVENT_PAYLOAD")
    if not raw:
        return None
    try:
        outer = json.loads(raw)
    except Exception as exc:
        print(f"Could not parse AUTOMATION_EVENT_PAYLOAD: {exc}")
        return None
    event = outer.get("event") or {}
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else None


def _entry_for(repo_full_name: str) -> dict | None:
    """Map a payload's repository onto its REPOS entry, or None if the org
    webhook delivered an event for a repo this automation does not manage."""
    for entry in REPOS:
        if entry.get("repo", "").lower() == (repo_full_name or "").lower():
            return entry
    return None


def _push_branch(payload: dict) -> str | None:
    """Branch name for a push delivery, or None if this is not one."""
    ref = payload.get("ref")
    if not isinstance(ref, str) or "issue" in payload:
        return None
    if not ref.startswith(PUSH_REF_PREFIX):
        return None
    return ref[len(PUSH_REF_PREFIX):]


# ── Agent server / secrets ──────────────────────────────────────────────────

def _agent_url() -> str:
    return os.environ.get("AGENT_SERVER_URL", "http://127.0.0.1:18000").rstrip("/")


def _get_env_key() -> str:
    return os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "") or ""


def get_secret(name: str) -> str:
    req = urllib.request.Request(
        f"{_agent_url()}/api/settings/secrets/{name}",
        headers={"X-Session-API-Key": _get_env_key()},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode().strip()


def fire_callback(status: str = "COMPLETED", error: str | None = None,
                   conversation_id: str | None = None) -> None:
    """Tell the automation service this run is done. Without this, every run
    stays shown as RUNNING forever in the run history - later events still fire
    regardless, but the status is misleading for anyone watching."""
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body: dict = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    if conversation_id:
        body["conversation_id"] = conversation_id
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        print(f"Callback error (non-fatal): {exc}")


def _oh_request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_agent_url()}{path}", data=data, method=method,
        headers={"X-Session-API-Key": _get_env_key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Agent API {method} {path} -> {exc.code}: {exc.read().decode()[:300]}")


def _fetch_settings() -> dict:
    req = urllib.request.Request(
        f"{_agent_url()}/api/settings",
        headers={"X-Session-API-Key": _get_env_key(), "X-Expose-Secrets": "plaintext"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def _get_agent_dict() -> dict:
    agent_settings = _fetch_settings().get("agent_settings", {})
    return {
        "kind": "Agent",
        "llm": agent_settings.get("llm", {}),
        "tools": [{"name": "terminal"}, {"name": "file_editor"}],
    }


def _build_secrets_payload() -> dict:
    try:
        secrets_list = _oh_request("GET", "/api/settings/secrets").get("secrets", [])
    except Exception as exc:
        log(f"warning: could not list secrets: {exc}")
        return {}
    secrets: dict = {}
    for s in secrets_list:
        name = s.get("name", "")
        if not name:
            continue
        lookup: dict = {"kind": "LookupSecret", "url": f"/api/settings/secrets/{name}"}
        if _get_env_key():
            lookup["headers"] = {"X-Session-API-Key": _get_env_key()}
        if s.get("description"):
            lookup["description"] = s["description"]
        secrets[name] = lookup
    return secrets


def create_conversation(initial_message: str, workspace_dir: Path) -> str:
    payload: dict = {
        "workspace": {"working_dir": str(workspace_dir)},
        "agent": _get_agent_dict(),
        "initial_message": {"content": [{"text": initial_message}]},
    }
    secrets = _build_secrets_payload()
    if secrets:
        payload["secrets"] = secrets
    return _oh_request("POST", "/api/conversations", payload)["id"]


def conversation_status(conv_id: str) -> str:
    return _oh_request("GET", f"/api/conversations/{conv_id}").get("execution_status", "unknown")


def conversation_final_response(conv_id: str) -> str:
    return _oh_request("GET", f"/api/conversations/{conv_id}/agent_final_response").get("response", "")


# ── KV state (crash-safe dedup + concurrency slots) ─────────────────────────

_KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
_KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")


def _require_kv() -> None:
    if not (_KV_TOKEN and _KV_BASE):
        raise RuntimeError("Automation KV store is required for crash-safe issue-resolution dedup")


def _kv_request(method: str, suffix: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{_state_key()}{suffix}", data=data,
        headers={"Authorization": f"Bearer {_KV_TOKEN}", "Content-Type": "application/json"}, method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def _resolution_key(issue_number: int, assigner: str) -> str:
    return f"{issue_number}:{assigner}"


def _compact_active(active: dict | None) -> dict | None:
    if not active:
        return None
    fields = (
        "resolution_key", "issue_number", "assigner", "html_url", "status",
        "owner_run_id", "claimed_at", "conversation_id", "workspace_dir",
        "branch", "last_activity", "error", "terminal_status", "finalizer_run_id",
    )
    compact = {field: active[field] for field in fields if field in active}
    for field in ("html_url", "workspace_dir", "error"):
        if field in compact:
            compact[field] = str(compact[field])[:1000]
    return compact


def _compact_state(value: dict | None) -> dict:
    value = value or {}
    seen = [str(item) for item in (value.get("seen") or [])][-MAX_SEEN:]
    acknowledged = [str(item) for item in (value.get("acknowledged") or [])][-MAX_SEEN:]
    raw_active = value.get("active") or {}
    if not isinstance(raw_active, dict):
        raw_active = {}
    active = {}
    for key, record in raw_active.items():
        compact = _compact_active(record)
        if compact:
            compact.setdefault("resolution_key", key)
            active[key] = compact
    raw_attempts = value.get("attempts") or {}
    attempts = {}
    if isinstance(raw_attempts, dict):
        for key, count in raw_attempts.items():
            try:
                attempts[str(key)] = int(count)
            except (TypeError, ValueError):
                continue
    attempts = dict(list(attempts.items())[-MAX_EVALUATED_ENTRIES:])
    return {"version": 1, "repo": _cfg("repo", ""), "seen": seen, "acknowledged": acknowledged,
            "attempts": attempts, "active": active}


def _load_state() -> tuple[dict, int]:
    _require_kv()
    try:
        data = _kv_request("GET", "?meta=true")
        return _compact_state(data.get("value")), int(data.get("version", 0))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _compact_state(None), 0
        raise


def _save_state(state: dict, version: int) -> bool:
    suffix = "?nx=true" if version == 0 else f"?if_version={version}"
    try:
        _kv_request("PUT", suffix, _compact_state(state))
        return True
    except urllib.error.HTTPError as exc:
        if exc.code in (409, 412):
            return False
        raise


def _claim_resolution(issue: dict, assigner: str) -> tuple[str, dict] | None:
    number = issue["number"]
    resolution_key = _resolution_key(number, assigner)
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        active = state["active"]
        if resolution_key in state["seen"] or resolution_key in active:
            return None
        if len(active) >= _cfg("max_concurrent_resolutions", MAX_CONCURRENT_RESOLUTIONS):
            return None
        claim = {
            "resolution_key": resolution_key, "issue_number": number, "assigner": assigner,
            "html_url": issue.get("html_url", ""), "status": "claimed",
            "owner_run_id": os.environ.get("AUTOMATION_RUN_ID", ""), "claimed_at": time.time(),
        }
        active[resolution_key] = claim
        state["active"] = active
        if _save_state(state, version):
            return resolution_key, claim
    raise RuntimeError("Could not claim issue resolution after concurrent state updates")


def _record_failed_attempt(resolution_key: str) -> int:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        count = int(state["attempts"].get(resolution_key, 0)) + 1
        if count >= _cfg("max_creation_attempts", MAX_CREATION_ATTEMPTS):
            state["attempts"].pop(resolution_key, None)
            if resolution_key not in state["seen"]:
                state["seen"] = (state["seen"] + [resolution_key])[-MAX_SEEN:]
        else:
            state["attempts"][resolution_key] = count
        if _save_state(state, version):
            return count
        time.sleep(0.2)
    return _cfg("max_creation_attempts", MAX_CREATION_ATTEMPTS)


def _mark_seen(resolution_key: str) -> None:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        if resolution_key in state["seen"]:
            return
        state["seen"] = (state["seen"] + [resolution_key])[-MAX_SEEN:]
        if _save_state(state, version):
            return
        time.sleep(0.2)


def _mark_acknowledged(resolution_key: str) -> None:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        if resolution_key in state["acknowledged"]:
            return
        state["acknowledged"] = (state["acknowledged"] + [resolution_key])[-MAX_SEEN:]
        if _save_state(state, version):
            return
        time.sleep(0.2)


def _update_active(resolution_key: str, **changes) -> dict | None:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        record = state["active"].get(resolution_key)
        if not record:
            return None
        record.update(changes)
        state["active"][resolution_key] = record
        if _save_state(state, version):
            return record
        time.sleep(0.2)
    return None


def _acquire_finalizer(resolution_key: str, terminal_status: str) -> dict | None:
    """Make exactly one concurrent run responsible for posting the outcome
    and releasing the slot, mirroring the PR reviewer's finalizer claim."""
    run_id = os.environ.get("AUTOMATION_RUN_ID", "")
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        record = state["active"].get(resolution_key)
        if not record:
            return None
        if record.get("status") == "finalizing":
            return None
        record["status"] = "finalizing"
        record["terminal_status"] = terminal_status
        record["finalizer_run_id"] = run_id
        state["active"][resolution_key] = record
        if _save_state(state, version):
            return record
        time.sleep(0.2)
    return None


def _clear_active(resolution_key: str, **_details) -> bool:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        if resolution_key not in state["active"]:
            return True
        state["active"].pop(resolution_key, None)
        if _save_state(state, version):
            return True
        time.sleep(0.2)
    return False


def _release_resources(record: dict) -> None:
    """Drop the repository checkout a finished resolution no longer needs.

    The conversation is deliberately KEPT (not deleted) - it is the record
    of what the agent did and is wanted for inspection afterwards. Only the
    checkout is removed, and only once the conversation is confirmed
    stopped: the give-up-on-timeout path can reach here while a conversation
    is technically still running, and removing its working directory out
    from under it would be worse than just leaving the disk space used.
    """
    workspace_dir = record.get("workspace_dir")
    if not workspace_dir:
        return
    conv_id = record.get("conversation_id")
    if conv_id:
        try:
            status = conversation_status(conv_id)
        except urllib.error.HTTPError as exc:
            status = "finished" if exc.code == 404 else None
        except Exception:
            status = None
        if status is None:
            log(f"  Could not confirm conversation {conv_id} has stopped; leaving checkout {workspace_dir} in place")
            return
        if status not in TERMINAL_STATUSES:
            log(f"  Conversation {conv_id} is still '{status}'; leaving its checkout in place")
            return
    try:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        log(f"  Removed checkout {workspace_dir}")
    except Exception as exc:
        log(f"  Warning: could not remove checkout {workspace_dir}: {exc}")


def _finish_resolution(record: dict, resolution_key: str) -> None:
    _release_resources(record)
    _mark_seen(resolution_key)
    _clear_active(resolution_key)


# ── GitHub ───────────────────────────────────────────────────────────────────

def _github_request(token: str, method: str, path: str, params: dict | None = None,
                     body: dict | None = None) -> tuple[dict | list, dict]:
    url = f"https://api.github.com{path}"
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read()
        return (json.loads(raw) if raw.strip() else {}), dict(r.headers)


def _github_paginate(token: str, path: str, params: dict | None = None) -> list:
    items: list = []
    page = 1
    params = dict(params or {})
    params["per_page"] = 100
    while True:
        params["page"] = page
        result, _ = _github_request(token, "GET", path, params=params)
        if not result:
            break
        items.extend(result)
        if len(result) < 100:
            break
        page += 1
    return items


def _resolve_github_token() -> str:
    return get_secret("GITHUB_TOKEN")


def _resolve_github_read_token() -> str:
    """Used for the high-volume read-only polling calls (verify, list
    issues, timeline, collaborator checks). Every comment/PR still goes out
    under GITHUB_TOKEN so it is correctly attributed to the bot."""
    return get_secret("GITHUB_TOKEN")


def _verify_token_and_repo(token: str, repo: str) -> dict:
    user, _ = _github_request(token, "GET", "/user")
    print(f"Authenticated as GitHub user: {user.get('login')}")
    repo_info, _ = _github_request(token, "GET", f"/repos/{repo}")
    return repo_info


def _list_assigned_issues(token: str, repo: str, assignee: str) -> list[dict]:
    issues = _github_paginate(token, f"/repos/{repo}/issues",
                               {"state": "open", "assignee": assignee, "sort": "updated", "direction": "desc"})
    # The /issues endpoint also returns PRs (they are issues under the hood);
    # a PR assigned to the bot is not something this automation acts on.
    return [issue for issue in issues if "pull_request" not in issue]


def _get_issue(token: str, repo: str, number: int) -> dict:
    issue, _ = _github_request(token, "GET", f"/repos/{repo}/issues/{number}")
    return issue


def _find_assigner(token: str, repo: str, issue_number: int, bot_username: str) -> str | None:
    """Who most recently assigned the bot to this issue, from the issue
    timeline. Returns None if that can't be determined (e.g. transient API
    issue), which is treated as untrusted rather than assumed-trusted."""
    events = _github_paginate(token, f"/repos/{repo}/issues/{issue_number}/timeline", {})
    for event in reversed(events):
        if event.get("event") != "assigned":
            continue
        assignee = (event.get("assignee") or {}).get("login", "")
        if assignee.lower() == bot_username.lower():
            actor = (event.get("actor") or {}).get("login")
            if actor:
                return actor
    return None


def _is_trusted_actor(token: str, repo: str, login: str) -> bool:
    """A collaborator on the repo, which also covers org members with any
    repo permission - matches who GitHub itself would let assign the bot in
    the first place for a private/internal repo, and is the deliberate gate
    for a public repo where anyone can technically open issues."""
    try:
        _github_request(token, "GET", f"/repos/{repo}/collaborators/{login}")
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def _post_github_comment(token: str, repo: str, issue_number: int, body: str) -> None:
    _github_request(token, "POST", f"/repos/{repo}/issues/{issue_number}/comments", body={"body": body})


def _branch_exists(token: str, repo: str, branch: str) -> dict | None:
    try:
        info, _ = _github_request(token, "GET", f"/repos/{repo}/branches/{branch}")
        return info
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _commits_ahead(token: str, repo: str, base: str, head: str) -> int:
    result, _ = _github_request(token, "GET", f"/repos/{repo}/compare/{base}...{head}")
    return int(result.get("ahead_by", 0))


def _open_pr(token: str, repo: str, title: str, head: str, base: str, body: str, draft: bool) -> dict:
    result, _ = _github_request(token, "POST", f"/repos/{repo}/pulls", body={
        "title": title, "head": head, "base": base, "body": body, "draft": draft,
    })
    return result


def _with_ai_disclosure(body: str) -> str:
    disclosure = "_This was posted by an AI agent (OpenHands) in response to being assigned this issue._"
    body = (body or "").strip()
    if disclosure.lower() in body.lower():
        return body
    return f"{body}\n\n{disclosure}" if body else disclosure


# ── Repository preparation ───────────────────────────────────────────────────

def _checkout_path(issue_number: int) -> Path:
    workspace = Path(os.environ.get("WORKSPACE_BASE", "/workspace")).resolve()
    return workspace / "repositories" / f"issue-{issue_number}"


def _prepare_repository(repo: str, issue_number: int, base_branch: str, branch_name: str) -> Path:
    """A plain, unauthenticated clone: both target repos are public, and
    read access needs no token. Only the eventual push needs credentials,
    and that is the agent's own job using its injected GITHUB_TOKEN secret -
    this script never handles a push-capable credential itself."""
    checkout = _checkout_path(issue_number)
    if checkout.exists():
        shutil.rmtree(checkout)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "50", "--branch", base_branch,
         f"https://github.com/{repo}.git", str(checkout)],
        check=True, capture_output=True, text=True, timeout=300,
    )
    subprocess.run(["git", "-C", str(checkout), "checkout", "-b", branch_name], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", _cfg("bot_username", BOT_USERNAME)], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.email",
                     f"{_cfg('bot_username', BOT_USERNAME)}@users.noreply.github.com"], check=True)
    return checkout


# ── Prompt ───────────────────────────────────────────────────────────────────

def _issue_comments_text(token: str, repo: str, issue_number: int) -> str:
    comments = _github_paginate(token, f"/repos/{repo}/issues/{issue_number}/comments", {})
    if not comments:
        return "(no comments)"
    parts = []
    for c in comments[-10:]:
        author = (c.get("user") or {}).get("login", "?")
        parts.append(f"@{author}: {(c.get('body') or '').strip()}")
    return "\n---\n".join(parts)


def _build_resolution_prompt(issue: dict, repo: str, branch_name: str, base_branch: str,
                              comments_text: str) -> str:
    number = issue.get("number", "?")
    title = issue.get("title", "(no title)")
    body = (issue.get("body") or "").strip() or "(no description)"
    html_url = issue.get("html_url", "")
    labels = ", ".join(label.get("name", "") for label in issue.get("labels", [])) or "(none)"

    return (
        f"You have been assigned GitHub issue #{number} in {repo} and are expected to "
        "resolve it if reasonably possible, opening a pull request with the fix - or, if "
        "you cannot, explaining why as a plain comment instead.\n\n"
        f"Repository : {repo}\n"
        f"Issue #{number}: \"{title}\"\n"
        f"Labels     : {labels}\n"
        f"URL        : {html_url}\n"
        f"\nIssue description:\n---\n{body}\n---\n"
        f"\nRecent comments (most recent 10):\n---\n{comments_text}\n---\n\n"
        "Required workflow:\n"
        f"1. The workspace is already a clone of {repo} at `{base_branch}`, with a new branch "
        f"`{branch_name}` already checked out. Do not delete or re-clone the repository, and do "
        "not switch branches.\n"
        "2. Follow the automatically loaded AGENTS.md guidance and applicable repository/public skills.\n"
        "3. Investigate the issue in the actual code before concluding anything. Reproduce it if "
        "practical (write or run a test).\n"
        "4. If you can make a correct, reasonably-scoped fix:\n"
        "   a. Implement it, and run the relevant existing tests (and add new ones covering the "
        "fix) until they pass. Do not push with failing tests.\n"
        "   b. Commit your changes with a clear, descriptive commit message.\n"
        "   c. Run `gh auth setup-git` once (GITHUB_TOKEN/GH_TOKEN is already in your environment for "
        f"`gh` to use), then `git push -u origin {branch_name}`. Never put the token in a remote URL, a "
        "command, or print it - `gh auth setup-git` configures push access without that.\n"
        "   d. Do not open the pull request yourself; a separate deterministic step does that once "
        "it confirms your branch was pushed.\n"
        "5. If you cannot make a confident fix (unclear requirements, cannot reproduce, needs a design "
        "decision, out of scope, etc.), do NOT push any commits. Instead, make your final response a "
        "clear explanation of what you found and, if applicable, what specific information or decision "
        "is needed from a maintainer. This will be posted verbatim as an issue comment.\n"
        "6. Your final response is what gets used either way: if you pushed a branch, it becomes the "
        "pull request description (so summarize the fix and note any residual risk or follow-up); "
        "otherwise it becomes the issue comment explaining the outcome. Do not include the AI "
        "disclosure yourself - it is added automatically."
    )


# ── Processing ───────────────────────────────────────────────────────────────

def _process_new_assignment(read_token: str, write_token: str, repo: str, base_branch: str, issue: dict,
                             assigner: str, resolution_key: str, claim: dict) -> str | None:
    number = issue["number"]
    branch_name = f"{_cfg('branch_prefix', BRANCH_PREFIX)}{number}"
    print(f"  Starting resolution attempt for issue #{number} (assigned by @{assigner})")
    workspace_dir = None
    try:
        workspace_dir = _prepare_repository(repo, number, base_branch, branch_name)
        comments_text = _issue_comments_text(read_token, repo, number)
        prompt = _build_resolution_prompt(issue, repo, branch_name, base_branch, comments_text)
        conv_id = create_conversation(prompt, workspace_dir)
    except Exception as exc:
        if workspace_dir:
            shutil.rmtree(workspace_dir, ignore_errors=True)
        _clear_active(resolution_key)
        attempts = _record_failed_attempt(resolution_key)
        max_attempts = _cfg("max_creation_attempts", MAX_CREATION_ATTEMPTS)
        if attempts >= max_attempts:
            print(f"  Resolution setup failed {attempts}x for issue #{number}; giving up: {exc}")
            _post_github_comment(write_token, repo, number, _with_ai_disclosure(
                f"⚠️ **OpenHands could not start work on this issue** after {attempts} attempts.\n\n"
                f"```\n{str(exc)[:500]}\n```"))
        else:
            print(f"  Resolution setup failed (attempt {attempts}/{max_attempts}); "
                  f"slot released for retry: {exc}")
        return None
    _update_active(resolution_key, status="active", conversation_id=conv_id,
                    workspace_dir=str(workspace_dir), branch=branch_name, last_activity=time.time())
    _mark_seen(resolution_key)
    try:
        openhands_url = _cfg("openhands_url", OPENHANDS_URL_DEFAULT)
        try:
            openhands_url = (get_secret("OPENHANDS_URL") or openhands_url).rstrip("/")
        except Exception:
            pass
        _post_github_comment(write_token, repo, number, _with_ai_disclosure(
            f"\U0001f916 **OpenHands is working on this issue.**\n\n"
            f"View the conversation: {openhands_url}/conversations/{conv_id}"))
    except Exception as exc:
        print(f"  Warning: acknowledgement comment failed for issue #{number}: {exc}")
    return conv_id


def _check_resolution_completion(record: dict, read_token: str, write_token: str, repo: str, base_branch: str) -> None:
    resolution_key = record["resolution_key"]
    number = record["issue_number"]
    status = record.get("status")
    age = time.time() - float(record.get("last_activity") or record.get("claimed_at") or 0.0)
    max_active_age = _cfg("max_active_age_seconds", MAX_ACTIVE_AGE)

    if age > max_active_age:
        _post_github_comment(write_token, repo, number, _with_ai_disclosure(
            f"⚠️ **OpenHands gave up on this issue** after {max_active_age // 3600}h "
            f"(last state: `{status}`). Unassign and reassign to try again."))
        _finish_resolution(record, resolution_key)
        print(f"  Resolution {resolution_key} exceeded {max_active_age}s in state '{status}'; slot released")
        return

    if status == "finalizing":
        return  # another (or this) run is already posting the outcome
    if status != "active" or age < DONE_DEBOUNCE:
        return

    conv_id = record["conversation_id"]
    try:
        issue = _get_issue(read_token, repo, number)
    except Exception as exc:
        print(f"  Warning: could not refetch issue #{number}: {exc}")
        return
    if issue.get("state") != "open":
        print(f"  Issue #{number} was closed while in progress; releasing slot without posting")
        _finish_resolution(record, resolution_key)
        return

    try:
        conv_status = conversation_status(conv_id)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"  Conversation {conv_id} no longer exists; slot released")
            _finish_resolution(record, resolution_key)
            return
        print(f"  Warning: could not get status for {conv_id}: {exc}")
        return
    except Exception as exc:
        print(f"  Warning: could not get status for {conv_id}: {exc}")
        return
    if conv_status not in TERMINAL_STATUSES:
        return
    if not _acquire_finalizer(resolution_key, conv_status):
        return

    try:
        final = conversation_final_response(conv_id)
    except Exception:
        final = ""

    branch_name = record.get("branch") or f"{_cfg('branch_prefix', BRANCH_PREFIX)}{number}"
    if conv_status in {"error", "stuck"}:
        _post_github_comment(write_token, repo, number, _with_ai_disclosure(
            f"⚠️ **OpenHands ran into a problem working on this issue** "
            f"(status: `{conv_status}`).\n\n{final}".strip()))
    else:
        # Trust GitHub, not the agent's own claim, for whether a branch was
        # actually pushed.
        branch_info = _branch_exists(read_token, repo, branch_name)
        ahead = _commits_ahead(read_token, repo, base_branch, branch_name) if branch_info else 0
        if branch_info and ahead > 0:
            title = f"Fix #{number}: {issue.get('title', '').strip()}"[:250]
            body = _with_ai_disclosure(f"Closes #{number}\n\n{final}".strip())
            try:
                pr = _open_pr(write_token, repo, title, branch_name, base_branch, body, _cfg("draft_pr", DRAFT_PR))
                print(f"  Issue #{number}: opened PR #{pr.get('number')} ({branch_name} -> {base_branch})")
            except Exception as exc:
                print(f"  Warning: PR creation failed for issue #{number}: {exc}")
                _post_github_comment(write_token, repo, number, _with_ai_disclosure(
                    f"⚠️ **OpenHands pushed a fix to `{branch_name}` but could not open the "
                    f"pull request:**\n\n```\n{str(exc)[:500]}\n```"))
        else:
            _post_github_comment(write_token, repo, number, _with_ai_disclosure(
                final or "OpenHands looked into this issue but did not make a code change."))
            print(f"  Issue #{number}: no branch pushed; posted the result as a comment")

    _finish_resolution(record, resolution_key)


def _process_repo(
    repo_entry: dict, read_token: str, write_token: str,
) -> str | None:
    """Process one repository end-to-end: reconcile in-flight resolutions, then
    evaluate every assigned issue through the trust gate. Each repo has its
    own KV state key, so this is isolated from every other repo."""
    global _CURRENT
    _CURRENT = dict(repo_entry)
    repo = _cfg("repo", "")
    bot_username = _cfg("bot_username", BOT_USERNAME)
    print(f"\n=== {repo} ===")
    # A 401 here is a real misconfiguration (HTTPError) and must surface as a
    # hard failure. But a transient connection failure to api.github.com should
    # skip this repo and let a later event retry, not fail the whole run.
    try:
        repo_info = _verify_token_and_repo(read_token, repo)
    except urllib.error.HTTPError:
        raise
    except Exception as exc:
        print(f"  Skipping {repo}: could not reach GitHub to verify token ({exc}); will retry on a later event")
        return None
    base_branch = repo_info.get("default_branch", "main")

    state, _ = _load_state()
    for record in list(state["active"].values()):
        try:
            _check_resolution_completion(record, read_token, write_token, repo, base_branch)
        except Exception as exc:
            print(f"  Warning: completion check failed for {record.get('resolution_key')}: {exc}")

    max_concurrent = _cfg("max_concurrent_resolutions", MAX_CONCURRENT_RESOLUTIONS)
    state, _ = _load_state()
    in_flight = len(state["active"])
    capacity = max_concurrent - in_flight
    print(f"  {in_flight}/{max_concurrent} resolution(s) in flight; capacity for {max(capacity, 0)} more")

    last_conversation_id = None
    issues = _list_assigned_issues(read_token, repo, bot_username)
    print(f"  {len(issues)} open issue(s) currently assigned to @{bot_username}")
    for issue in issues:
        number = issue["number"]
        try:
            assigner = _find_assigner(read_token, repo, number, bot_username)
            if not assigner:
                print(f"  Issue #{number}: could not determine who assigned the bot; skipping this event")
                continue
            resolution_key = _resolution_key(number, assigner)
            state, _ = _load_state()
            if resolution_key in state["seen"] or resolution_key in state["active"]:
                continue

            if not _is_trusted_actor(read_token, repo, assigner):
                _post_github_comment(write_token, repo, number, _with_ai_disclosure(
                    f"\U0001f44b @{assigner}, I can only pick up work assigned by a maintainer or "
                    "repository collaborator. A maintainer can re-assign this to me to have me take "
                    "a look."))
                _mark_seen(resolution_key)
                print(f"  Issue #{number}: assigner @{assigner} is not a trusted collaborator; declined")
                continue

            if resolution_key not in state["acknowledged"]:
                # Post something the moment the assignment is recognized as
                # legitimate, decoupled from whether the repo clone /
                # conversation setup actually succeeds on the first try (or
                # even gets a free capacity slot on this event) - otherwise a run
                # of setup failures or a busy queue leaves the issue looking
                # completely ignored until either a success or the final
                # give-up comment, which can be many minutes away.
                _post_github_comment(write_token, repo, number, _with_ai_disclosure(
                    "\U0001f440 I see this issue was assigned to me and will start looking into it "
                    "shortly."))
                _mark_acknowledged(resolution_key)

            if capacity <= 0:
                continue
            claimed = _claim_resolution(issue, assigner)
            if not claimed:
                continue
            resolution_key, claim = claimed
            capacity -= 1
            conv_id = _process_new_assignment(read_token, write_token, repo, base_branch, issue, assigner, resolution_key, claim)
            if conv_id:
                last_conversation_id = conv_id
        except Exception as exc:
            # One issue's failure must not stop every other assigned issue
            # from being evaluated in the same run, and must not fail the
            # whole run.
            print(f"  Warning: failed evaluating issue #{number}: {exc}")
    return last_conversation_id


def _wait_for_repo(repo_entry: dict, read_token: str, write_token: str) -> None:
    """Block while this repo has in-flight resolutions, finalizing each as it
    reaches a terminal state, until the run's time budget runs out.

    Staying in-process is what lets a resolution open its PR in the same run
    that started it, instead of waiting for the next unrelated event. When the
    budget expires the records are simply left active -- the reconcile pass at
    the top of `_process_repo` closes them out on a later event.
    """
    global _CURRENT
    _CURRENT = dict(repo_entry)
    repo = _cfg("repo", "")

    state, _ = _load_state()
    if not state["active"]:
        return
    try:
        base_branch = _verify_token_and_repo(read_token, repo).get("default_branch", "main")
    except Exception as exc:
        print(f"  Not waiting on {repo}: could not reach GitHub ({exc})")
        return

    deadline = time.time() + INLINE_WAIT_BUDGET
    while time.time() < deadline:
        time.sleep(INLINE_POLL_INTERVAL)
        state, _ = _load_state()
        records = list(state["active"].values())
        if not records:
            print(f"  {repo}: all resolutions finalized")
            return
        for record in records:
            try:
                _check_resolution_completion(record, read_token, write_token, repo, base_branch)
            except Exception as exc:
                print(f"  Warning: completion check failed for "
                      f"{record.get('resolution_key')}: {exc}")

    state, _ = _load_state()
    if state["active"]:
        print(f"  {repo}: {len(state['active'])} resolution(s) still running at the "
              f"{INLINE_WAIT_BUDGET}s budget; a later event for this repo finishes them")


def main() -> str | None:
    _require_kv()
    read_token = _resolve_github_read_token()
    write_token = _resolve_github_token()

    payload = _load_event()
    if payload is None:
        # Manual dispatch (or a malformed payload): fall back to sweeping every
        # managed repo, which is also the way to drain work after an outage.
        print("No event payload; sweeping every managed repo")
        entries = list(REPOS)
    else:
        repo_full = (payload.get("repository") or {}).get("full_name") or ""
        entry = _entry_for(repo_full)
        if entry is None:
            # The org webhook delivers every repo in the org; most are not ours.
            print(f"Ignoring event for unmanaged repo {repo_full!r}")
            return None
        branch = _push_branch(payload)
        print(f"Event for {repo_full}: action={payload.get('action')!r}"
              + (f" branch={branch!r}" if branch else ""))
        entries = [entry]

    last_conversation_id = None
    for repo_entry in entries:
        # A failure on one repo must never stop the others being processed.
        try:
            conv_id = _process_repo(repo_entry, read_token, write_token)
            if conv_id:
                # Only wait on work this run actually started. Waiting whenever
                # the repo has anything in flight would pin a sandbox for the
                # full budget on every event in a burst, all watching the same
                # resolutions that one run is already waiting on.
                last_conversation_id = conv_id
                _wait_for_repo(repo_entry, read_token, write_token)
        except Exception as exc:
            repo = repo_entry.get("repo", "?")
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
