"""
Linear Assistant - OpenHands Automation Script

Makes the Linear bot user (`all-hands-bot-internal`) behave like a teammate
backed by an OpenHands conversation on this box. It reads the bot's own Linear
notifications, which cover both ways of asking it for something:

  * @-mention in a comment (or the description) -> "help me with this":
    start a conversation with the ticket plus the request, and post the
    agent's answer as a reply in that comment's thread.
  * the ticket is assigned to the bot -> "solve this": start a conversation
    that tries to fix the ticket; post the answer as a ticket comment.

Either way the conversation runs over fresh, unauthenticated clones of the
candidate repositories, each on a branch that contains the ticket identifier
(so Linear's GitHub integration links it). The agent pushes with its own
GITHUB_TOKEN if it decides a code change is warranted. Once the conversation
reaches a terminal state, the script checks GitHub directly (never trusting
the agent's own claim) for which branches were actually pushed with new
commits, opens a draft PR for each, and links them in the reply. If nothing
was pushed, the reply is the agent's final response.

Linear cannot reach this box (the public URL sits behind Google OAuth), so
this is a cron poll rather than a webhook. Each notification is handled at
most once (keyed by its id in the KV store), and notifications older than the
automation's first run are ignored, so enabling it does not answer a backlog
of old mentions.

Replying needs the Linear token to carry the `comments:create` (or `write`)
scope; reading the notifications only needs `read`.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG = json.loads((Path(os.environ.get("AUTOMATION_CONFIG_PATH") or Path(__file__).with_name("config.json"))).read_text())
LINEAR_BOT_USER_ID = CONFIG["linear_bot_user_id"]
LINEAR_API_KEY_SECRET = CONFIG.get("linear_api_key_secret", "LINEAR_BOT_READ_AND_MENTION_TOKEN")
# Only tickets in these teams are handled: the repos below are this team's.
LINEAR_TEAM_KEYS = set(CONFIG.get("linear_team_keys") or [])
REPOS: list[str] = CONFIG["repos"]
GITHUB_BOT_USERNAME = CONFIG.get("github_bot_username", "all-hands-bot")
DRAFT_PR = bool(CONFIG.get("draft_pr", True))
BRANCH_PREFIX = CONFIG.get("branch_prefix", "openhands/")
OPENHANDS_URL_DEFAULT = CONFIG.get("openhands_url", "https://oss-agent-canvas.ngrok.dev")
# Each holds one conversation, a checkout of every repo, and real LLM spend,
# so this bounds cost and load on the shared VM. Requests beyond it wait for
# a later tick.
MAX_CONCURRENT_TASKS = int(CONFIG.get("max_concurrent_tasks", 2))
MAX_ACTIVE_AGE = int(CONFIG.get("max_active_age_seconds", 3 * 60 * 60))
# Consecutive conversation-creation failures tolerated per request before
# giving up, so a request whose setup always fails is not retried forever.
MAX_CREATION_ATTEMPTS = int(CONFIG.get("max_creation_attempts", 3))
HTTP_TIMEOUT = int(CONFIG.get("http_timeout_seconds", 30))
MAX_SEEN = int(CONFIG.get("max_seen_events", 500))

MENTION_TYPES = {"issueCommentMention", "issueMention"}
ASSIGN_TYPES = {"issueAssignedToYou"}
DONE_DEBOUNCE = 15
CAS_RETRIES = 8
TERMINAL_STATUSES = {"idle", "finished", "error", "stuck"}
LINEAR_API_URL = "https://api.linear.app/graphql"


def log(*args: object) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]", *args, flush=True)


# ── Agent server / secrets ──────────────────────────────────────────────────

def _agent_url() -> str:
    return os.environ.get("AGENT_SERVER_URL", "http://127.0.0.1:18000").rstrip("/")


def _get_env_key() -> str:
    return os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0") or ""


def get_secret(name: str) -> str:
    req = urllib.request.Request(
        f"{_agent_url()}/api/settings/secrets/{name}",
        headers={"X-Session-API-Key": _get_env_key()},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode().strip()


def fire_callback(status: str = "COMPLETED", error: str | None = None,
                   conversation_id: str | None = None) -> None:
    """Tell the automation service this run is done, so the run is not shown
    as RUNNING forever in the run history."""
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


def _openhands_url() -> str:
    try:
        return (get_secret("OPENHANDS_URL") or OPENHANDS_URL_DEFAULT).rstrip("/")
    except Exception:
        return OPENHANDS_URL_DEFAULT


# ── KV state (crash-safe dedup + concurrency slots) ─────────────────────────

_KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
_KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")
_STATE_KEY = "linear-assistant-state-v1"


def _require_kv() -> None:
    if not (_KV_TOKEN and _KV_BASE):
        raise RuntimeError("Automation KV store is required for crash-safe request dedup")


def _kv_request(method: str, suffix: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{_STATE_KEY}{suffix}", data=data,
        headers={"Authorization": f"Bearer {_KV_TOKEN}", "Content-Type": "application/json"}, method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


_ACTIVE_FIELDS = (
    "key", "kind", "issue_id", "identifier", "url", "reply_parent_id", "request",
    "status", "owner_run_id", "claimed_at", "conversation_id", "workspace_dir",
    "branch", "last_activity", "terminal_status", "finalizer_run_id",
)


def _compact_state(value: dict | None) -> dict:
    value = value or {}
    active = {}
    for key, record in (value.get("active") or {}).items():
        if isinstance(record, dict):
            compact = {f: record[f] for f in _ACTIVE_FIELDS if f in record}
            for f in ("url", "workspace_dir", "request"):
                if f in compact:
                    compact[f] = str(compact[f])[:2000]
            active[key] = compact
    attempts = {}
    for key, count in (value.get("attempts") or {}).items():
        try:
            attempts[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return {
        "version": 1,
        "since": value.get("since") or "",
        "seen": [str(i) for i in (value.get("seen") or [])][-MAX_SEEN:],
        "attempts": dict(list(attempts.items())[-MAX_SEEN:]),
        "active": active,
    }


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


def _mutate_state(fn) -> object:
    """Apply fn(state) under compare-and-swap; fn returns (changed, result)."""
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        changed, result = fn(state)
        if not changed or _save_state(state, version):
            return result
        time.sleep(0.2)
    raise RuntimeError("Could not update automation state after concurrent updates")


def _ensure_since() -> str:
    """The first run records "now"; notifications older than that are a
    backlog from before the assistant existed and are never answered."""
    def fn(state):
        if state["since"]:
            return False, state["since"]
        # Same shape as Linear's createdAt, so the two compare as strings.
        state["since"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return True, state["since"]
    return _mutate_state(fn)


def _claim(task: dict) -> bool:
    def fn(state):
        key = task["key"]
        if key in state["seen"] or key in state["active"] or len(state["active"]) >= MAX_CONCURRENT_TASKS:
            return False, False
        state["active"][key] = {**task, "status": "claimed", "claimed_at": time.time(),
                                "owner_run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
        return True, True
    return _mutate_state(fn)


def _record_failed_attempt(key: str) -> int:
    def fn(state):
        count = int(state["attempts"].get(key, 0)) + 1
        if count >= MAX_CREATION_ATTEMPTS:
            state["attempts"].pop(key, None)
            if key not in state["seen"]:
                state["seen"] = (state["seen"] + [key])[-MAX_SEEN:]
        else:
            state["attempts"][key] = count
        return True, count
    return _mutate_state(fn)


def _mark_seen(key: str) -> None:
    def fn(state):
        if key in state["seen"]:
            return False, None
        state["seen"] = (state["seen"] + [key])[-MAX_SEEN:]
        return True, None
    _mutate_state(fn)


def _update_active(key: str, **changes) -> None:
    def fn(state):
        if key not in state["active"]:
            return False, None
        state["active"][key].update(changes)
        return True, None
    _mutate_state(fn)


def _acquire_finalizer(key: str, terminal_status: str) -> bool:
    """Make exactly one concurrent run responsible for posting the outcome."""
    def fn(state):
        record = state["active"].get(key)
        if not record or record.get("status") == "finalizing":
            return False, False
        record.update(status="finalizing", terminal_status=terminal_status,
                      finalizer_run_id=os.environ.get("AUTOMATION_RUN_ID", ""))
        return True, True
    return _mutate_state(fn)


def _clear_active(key: str) -> None:
    def fn(state):
        if key not in state["active"]:
            return False, None
        state["active"].pop(key)
        return True, None
    _mutate_state(fn)


def _release_resources(record: dict) -> None:
    """Drop the checkouts a finished task no longer needs. The conversation
    is kept as the record of what the agent did, and the checkouts are only
    removed once it is confirmed stopped."""
    workspace_dir = record.get("workspace_dir")
    if not workspace_dir:
        return
    conv_id = record.get("conversation_id")
    if conv_id:
        try:
            status = conversation_status(conv_id)
        except Exception as exc:
            status = "finished" if "-> 404" in str(exc) else None
        if status is None or status not in TERMINAL_STATUSES:
            log(f"  Conversation {conv_id} not confirmed stopped; leaving {workspace_dir} in place")
            return
    shutil.rmtree(workspace_dir, ignore_errors=True)


def _finish(record: dict) -> None:
    _release_resources(record)
    _mark_seen(record["key"])
    _clear_active(record["key"])


# ── Linear ───────────────────────────────────────────────────────────────────

def _linear_auth_header(key: str) -> str:
    # Personal API keys are sent bare; OAuth (app actor) tokens as Bearer.
    return key if key.startswith("lin_api_") else f"Bearer {key}"


def _linear(key: str, query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(
        LINEAR_API_URL, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": _linear_auth_header(key), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            result = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Linear API -> {exc.code}: {exc.read().decode()[:300]}")
    if result.get("errors"):
        raise RuntimeError(f"Linear API errors: {json.dumps(result['errors'])[:500]}")
    return result["data"]


def _list_notifications(key: str) -> list[dict]:
    query = """{ notifications(first: 50) { nodes {
      id type createdAt
      ... on IssueNotification {
        issue { id identifier title url team { key } }
        comment { id body user { id name } parent { id } }
        actor { id name }
      }
    } } }"""
    return _linear(key, query)["notifications"]["nodes"]


def _get_issue(key: str, issue_id: str) -> dict:
    query = """query($id: String!) { issue(id: $id) {
      id identifier title description url
      state { name type }
      assignee { id }
      labels { nodes { name } }
    } }"""
    return _linear(key, query, {"id": issue_id})["issue"]


def _issue_comments_text(key: str, issue_id: str) -> str:
    query = """query($id: String!) {
      issue(id: $id) { comments(last: 15) { nodes { body createdAt user { name } botActor { name } } } }
    }"""
    nodes = _linear(key, query, {"id": issue_id})["issue"]["comments"]["nodes"]
    if not nodes:
        return "(no comments)"
    nodes.sort(key=lambda c: c.get("createdAt", ""))
    return "\n---\n".join(
        f"{(c.get('user') or c.get('botActor') or {}).get('name', '?')}: {(c.get('body') or '').strip()}"
        for c in nodes
    )


def _post_linear_comment(key: str, issue_id: str, body: str, parent_id: str | None = None) -> None:
    query = """mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }"""
    payload = {"issueId": issue_id, "body": body}
    if parent_id:
        payload["parentId"] = parent_id
    _linear(key, query, {"input": payload})


def _reply(key: str, record: dict, body: str) -> None:
    _post_linear_comment(key, record["issue_id"], _with_ai_disclosure(body), record.get("reply_parent_id"))


def _with_ai_disclosure(body: str) -> str:
    disclosure = "_Posted by an AI agent (OpenHands)._"
    body = (body or "").strip()
    return f"{body}\n\n{disclosure}" if body else disclosure


def _task_from_notification(n: dict) -> dict | None:
    """Turn a bot notification into a task, or None if it is not a request."""
    kind = "mention" if n.get("type") in MENTION_TYPES else "assign" if n.get("type") in ASSIGN_TYPES else None
    issue = n.get("issue") or {}
    if not kind or not issue.get("id"):
        return None
    if LINEAR_TEAM_KEYS and (issue.get("team") or {}).get("key") not in LINEAR_TEAM_KEYS:
        return None
    comment = n.get("comment") or {}
    if (comment.get("user") or {}).get("id") == LINEAR_BOT_USER_ID:
        return None
    requester = (comment.get("user") or n.get("actor") or {}).get("name", "someone")
    return {
        "key": n["id"], "kind": kind, "issue_id": issue["id"], "identifier": issue["identifier"],
        "url": issue.get("url", ""),
        # Reply in the thread the mention came from; Linear threads are one
        # level deep, so a mention inside a reply answers under its root.
        "reply_parent_id": (comment.get("parent") or {}).get("id") or comment.get("id"),
        "request": f"{requester}: {(comment.get('body') or '').strip()}" if comment else "",
    }


# ── GitHub ───────────────────────────────────────────────────────────────────

def _github_request(token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://api.github.com{path}", data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else {}


def _default_branch(token: str, repo: str) -> str:
    return _github_request(token, "GET", f"/repos/{repo}").get("default_branch", "main")


def _commits_ahead(token: str, repo: str, base: str, head: str) -> int:
    try:
        return int(_github_request(token, "GET", f"/repos/{repo}/compare/{base}...{head}").get("ahead_by", 0))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 0  # branch was never pushed
        raise


def _open_pr(token: str, repo: str, title: str, head: str, base: str, body: str) -> dict:
    return _github_request(token, "POST", f"/repos/{repo}/pulls", body={
        "title": title, "head": head, "base": base, "body": body, "draft": DRAFT_PR,
    })


# ── Workspace ────────────────────────────────────────────────────────────────

def _branch_name(task: dict) -> str:
    # Containing the identifier is what makes Linear auto-link the branch/PR;
    # the suffix keeps two requests on the same ticket from sharing a branch.
    return f"{BRANCH_PREFIX}{task['identifier'].lower()}-{task['key'][:8]}"


def _prepare_workspace(task: dict, base_branches: dict[str, str], branch_name: str) -> Path:
    """Plain, unauthenticated clones of every candidate repo: all are public.
    Only the eventual push needs credentials, and that is the agent's own job
    using its injected GITHUB_TOKEN."""
    root = Path(os.environ.get("WORKSPACE_BASE", "/workspace")).resolve() / "linear" / task["key"]
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for repo, base in base_branches.items():
        checkout = root / repo.split("/")[1]
        subprocess.run(
            ["git", "clone", "--depth", "50", "--branch", base,
             f"https://github.com/{repo}.git", str(checkout)],
            check=True, capture_output=True, text=True, timeout=300,
        )
        subprocess.run(["git", "-C", str(checkout), "checkout", "-b", branch_name],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", GITHUB_BOT_USERNAME], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.email",
                        f"{GITHUB_BOT_USERNAME}@users.noreply.github.com"], check=True)
    return root


# ── Prompt ───────────────────────────────────────────────────────────────────

def _build_prompt(task: dict, issue: dict, base_branches: dict[str, str], branch_name: str,
                  comments_text: str) -> str:
    identifier = issue["identifier"]
    labels = ", ".join(label.get("name", "") for label in (issue.get("labels") or {}).get("nodes", [])) or "(none)"
    repo_lines = "\n".join(
        f"   - `./{repo.split('/')[1]}` -> {repo} (base `{base}`)" for repo, base in base_branches.items()
    )
    if task["kind"] == "mention":
        goal = (
            f"You were @-mentioned on Linear ticket {identifier}. Do what the person asked. It may be "
            "a question, a review, an investigation, or a request to change code.\n\n"
            f"The request:\n---\n{task.get('request') or '(mention in the ticket description)'}\n---\n"
        )
    else:
        goal = (
            f"Linear ticket {identifier} was assigned to you. Resolve it if reasonably possible by "
            "pushing a fix, or, if a code change is not the right answer, reply with what the ticket "
            "needs (findings, a concrete proposal, or the specific decision required).\n"
        )
    return (
        f"{goal}\n"
        f"Ticket     : {identifier} \"{issue.get('title', '')}\"\n"
        f"State      : {(issue.get('state') or {}).get('name', '?')}\n"
        f"Labels     : {labels}\n"
        f"URL        : {issue.get('url', '')}\n"
        f"\nTicket description:\n---\n{(issue.get('description') or '').strip() or '(no description)'}\n---\n"
        f"\nRecent comments (oldest first):\n---\n{comments_text}\n---\n\n"
        "How to work:\n"
        "1. The workspace contains a fresh clone of each candidate repository, each with a new "
        f"branch `{branch_name}` already checked out:\n{repo_lines}\n"
        "   Do not delete or re-clone them, and do not switch branches. Use whichever repo(s) the "
        "ticket concerns; leave the others untouched.\n"
        "2. Ground every answer in the actual code. Follow the AGENTS.md guidance of each repo you "
        "change.\n"
        "3. Only change code if that is what is being asked for (always so for an assigned ticket "
        "that has a reasonably-scoped fix). If you do:\n"
        "   a. Implement it and run the relevant tests (adding new ones for the change) until they "
        "pass. Do not push with failing tests.\n"
        "   b. Commit with a clear message, run `gh auth setup-git` once (GITHUB_TOKEN/GH_TOKEN is "
        f"in your environment), then `git push -u origin {branch_name}` from each repo you changed. "
        "Never put the token in a remote URL or a command, or print it.\n"
        "   c. Do not open pull requests and do not comment on Linear yourself; a separate step opens "
        "a draft PR for every pushed branch and posts your reply.\n"
        "4. Your final response is posted verbatim on the Linear ticket as your reply (and, if you "
        "pushed, also used as the PR description). Write it for the person who asked: answer "
        "directly, concisely, in Markdown. Do not add an AI disclosure; it is added automatically."
    )


# ── Processing ───────────────────────────────────────────────────────────────

def _start_task(linear_key: str, base_branches: dict[str, str], task: dict) -> str | None:
    branch_name = _branch_name(task)
    print(f"  Starting {task['kind']} task for {task['identifier']} ({task['key']})")
    workspace_dir = None
    try:
        issue = _get_issue(linear_key, task["issue_id"])
        workspace_dir = _prepare_workspace(task, base_branches, branch_name)
        prompt = _build_prompt(task, issue, base_branches, branch_name,
                               _issue_comments_text(linear_key, task["issue_id"]))
        conv_id = create_conversation(prompt, workspace_dir)
    except Exception as exc:
        if workspace_dir:
            shutil.rmtree(workspace_dir, ignore_errors=True)
        _clear_active(task["key"])
        attempts = _record_failed_attempt(task["key"])
        if attempts >= MAX_CREATION_ATTEMPTS:
            print(f"  Setup failed {attempts}x for {task['identifier']}; giving up: {exc}")
            _reply(linear_key, task, f"⚠️ **I could not start working on this** after {attempts} attempts."
                                     f"\n\n```\n{str(exc)[:500]}\n```")
        else:
            print(f"  Setup failed (attempt {attempts}/{MAX_CREATION_ATTEMPTS}); will retry: {exc}")
        return None
    _update_active(task["key"], status="active", conversation_id=conv_id,
                   workspace_dir=str(workspace_dir), branch=branch_name, last_activity=time.time())
    try:
        _reply(linear_key, task, f"👀 On it. Follow along: {_openhands_url()}/conversations/{conv_id}")
    except Exception as exc:
        print(f"  Warning: acknowledgement failed for {task['identifier']}: {exc}")
    return conv_id


def _check_completion(record: dict, linear_key: str, gh_token: str, base_branches: dict[str, str]) -> None:
    status = record.get("status")
    age = time.time() - float(record.get("last_activity") or record.get("claimed_at") or 0.0)

    if age > MAX_ACTIVE_AGE:
        _reply(linear_key, record, f"⚠️ **I gave up on this** after {MAX_ACTIVE_AGE // 3600}h "
                                   f"(last state: `{status}`). Mention me again to retry.")
        _finish(record)
        return
    if status != "active" or age < DONE_DEBOUNCE:
        return

    conv_id = record["conversation_id"]
    try:
        conv_status = conversation_status(conv_id)
    except Exception as exc:
        if "-> 404" in str(exc):
            print(f"  Conversation {conv_id} no longer exists; releasing")
            _finish(record)
        else:
            print(f"  Warning: could not get status for {conv_id}: {exc}")
        return
    if conv_status not in TERMINAL_STATUSES or not _acquire_finalizer(record["key"], conv_status):
        return

    try:
        final = conversation_final_response(conv_id)
    except Exception:
        final = ""
    conv_link = f"{_openhands_url()}/conversations/{conv_id}"

    if conv_status in {"error", "stuck"}:
        _reply(linear_key, record, f"⚠️ **I ran into a problem** (status: `{conv_status}`). "
                                   f"Conversation: {conv_link}\n\n{final}".strip())
        _finish(record)
        return

    # Trust GitHub, not the agent's own claim, for which branches were pushed.
    identifier = record["identifier"]
    branch_name = record["branch"]
    pr_lines = []
    for repo, base in base_branches.items():
        try:
            if _commits_ahead(gh_token, repo, base, branch_name) <= 0:
                continue
            title = f"{identifier}: {_get_issue(linear_key, record['issue_id']).get('title', '').strip()}"[:250]
            body = _with_ai_disclosure(f"Resolves [{identifier}]({record.get('url', '')})\n\n{final}".strip())
            pr = _open_pr(gh_token, repo, title, branch_name, base, body)
            pr_lines.append(f"- {repo}: {pr.get('html_url')}")
        except Exception as exc:
            print(f"  Warning: PR handling failed for {identifier} in {repo}: {exc}")
            pr_lines.append(f"- {repo}: pushed `{branch_name}` but could not open the PR (`{str(exc)[:200]}`)")

    reply = final or "I looked into this but have nothing to add."
    if pr_lines:
        reply += "\n\n**Draft PR(s):**\n" + "\n".join(pr_lines)
    _reply(linear_key, record, f"{reply}\n\nConversation: {conv_link}")
    print(f"  {identifier}: replied ({len(pr_lines)} PR(s))")
    _finish(record)


def main() -> str | None:
    linear_key = get_secret(LINEAR_API_KEY_SECRET)
    gh_token = get_secret("GITHUB_TOKEN")
    base_branches = {repo: _default_branch(gh_token, repo) for repo in REPOS}
    since = _ensure_since()

    state, _ = _load_state()
    for record in list(state["active"].values()):
        try:
            _check_completion(record, linear_key, gh_token, base_branches)
        except Exception as exc:
            print(f"  Warning: completion check failed for {record.get('key')}: {exc}")

    tasks = []
    for n in _list_notifications(linear_key):
        task = _task_from_notification(n) if n.get("createdAt", "") > since else None
        if task:
            tasks.append(task)
    tasks.reverse()  # oldest request first
    print(f"{len(tasks)} request(s) since {since}")

    last_conversation_id = None
    for task in tasks:
        try:
            if not _claim(task):
                continue  # already handled, in flight, or no free slot this tick
            conv_id = _start_task(linear_key, base_branches, task)
            if conv_id:
                last_conversation_id = conv_id
        except Exception as exc:
            # One request's failure must not stop the others or fail the run.
            print(f"  Warning: failed handling {task['identifier']} ({task['key']}): {exc}")
    return last_conversation_id


if __name__ == "__main__":
    try:
        conversation_id = main()
        fire_callback("COMPLETED", conversation_id=conversation_id)
    except Exception as exc:
        fire_callback("FAILED", str(exc))
        raise
