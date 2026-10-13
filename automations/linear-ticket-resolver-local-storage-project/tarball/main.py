"""
Linear Ticket Resolver - OpenHands Automation Script

Polls one Linear project for open issues currently assigned (or delegated) to
the all-hands bot user and attempts to resolve each one:

  1. Acknowledge the assignment with a Linear comment.
  2. Clone every candidate repository (public, read-only, no auth needed)
     side by side into one workspace, each on a branch named after the
     ticket (`openhands/oss-1234`), and start an AI coding conversation with
     the ticket title/description/comments as context. The ticket does not
     say which repo it belongs to, so the agent decides. It has its own
     GITHUB_TOKEN (via the standard secrets mechanism) to push the branch
     itself once it is done.
  3. Once the conversation reaches a terminal state, the script checks
     GitHub directly (never trusting the agent's own claim) for which of the
     branches were actually pushed with new commits:
       - For each one: open a draft PR referencing the Linear identifier
         (which also lets Linear's GitHub integration link it), and post a
         Linear comment linking the PR(s).
       - If none: post the agent's final response as a Linear comment
         (e.g. it needs clarification, or answered a design question
         without needing a code change).
     A conversation that errors, gets stuck, or exceeds the time budget
     posts an explanatory comment instead.

Linear cannot reach this box (the public URL sits behind Google OAuth), so
this is a cron poll rather than a webhook.

The unit of work is (issue identifier, assignment marker), where the marker
is the Linear history entry that assigned the bot. Unassigning and
re-assigning the bot therefore starts a fresh attempt; a finished attempt is
not repeated while the same assignment stands. A delegation (Linear's agent
assignment) has no such history entry and is keyed as `delegate`.

There is no trust gate as in the GitHub resolver: only members of the Linear
workspace can assign issues at all.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = json.loads((Path(__file__).with_name("config.json")).read_text())
LINEAR_PROJECT_ID = CONFIG["linear_project_id"]
LINEAR_BOT_USER_ID = CONFIG["linear_bot_user_id"]
LINEAR_API_KEY_SECRET = CONFIG.get("linear_api_key_secret", "LINEAR_BOT_READ_AND_MENTION_TOKEN")
REPOS: list[str] = CONFIG["repos"]
GITHUB_BOT_USERNAME = CONFIG.get("github_bot_username", "all-hands-bot")
DRAFT_PR = bool(CONFIG.get("draft_pr", True))
BRANCH_PREFIX = CONFIG.get("branch_prefix", "openhands/")
OPENHANDS_URL_DEFAULT = CONFIG.get("openhands_url", "https://oss-agent-canvas.ngrok.dev")
# Each holds one conversation, a checkout of every repo, and (if the agent
# pushes) real LLM + compute spend, so this bounds cost and load on the VM.
MAX_CONCURRENT_RESOLUTIONS = int(CONFIG.get("max_concurrent_resolutions", 2))
MAX_ACTIVE_AGE = int(CONFIG.get("max_active_age_seconds", 3 * 60 * 60))
# Consecutive conversation-creation failures tolerated per resolution key
# before giving up, so a ticket whose setup always fails is not retried on
# every tick forever.
MAX_CREATION_ATTEMPTS = int(CONFIG.get("max_creation_attempts", 3))
HTTP_TIMEOUT = int(CONFIG.get("http_timeout_seconds", 30))
MAX_SEEN = int(CONFIG.get("max_seen_events", 500))
MAX_EVALUATED_ENTRIES = int(CONFIG.get("max_evaluated_entries", 500))

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
_STATE_KEY = "linear-state-v1"


def _require_kv() -> None:
    if not (_KV_TOKEN and _KV_BASE):
        raise RuntimeError("Automation KV store is required for crash-safe ticket-resolution dedup")


def _kv_request(method: str, suffix: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{_STATE_KEY}{suffix}", data=data,
        headers={"Authorization": f"Bearer {_KV_TOKEN}", "Content-Type": "application/json"}, method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def _compact_active(active: dict | None) -> dict | None:
    if not active:
        return None
    fields = (
        "resolution_key", "issue_id", "identifier", "url", "status",
        "owner_run_id", "claimed_at", "conversation_id", "workspace_dir",
        "branch", "last_activity", "error", "terminal_status", "finalizer_run_id",
    )
    compact = {field: active[field] for field in fields if field in active}
    for field in ("url", "workspace_dir", "error"):
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
    return {"version": 1, "project": LINEAR_PROJECT_ID, "seen": seen, "acknowledged": acknowledged,
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


def _claim_resolution(issue: dict, resolution_key: str) -> dict | None:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        active = state["active"]
        if resolution_key in state["seen"] or resolution_key in active:
            return None
        if len(active) >= MAX_CONCURRENT_RESOLUTIONS:
            return None
        claim = {
            "resolution_key": resolution_key, "issue_id": issue["id"],
            "identifier": issue["identifier"], "url": issue.get("url", ""), "status": "claimed",
            "owner_run_id": os.environ.get("AUTOMATION_RUN_ID", ""), "claimed_at": time.time(),
        }
        active[resolution_key] = claim
        state["active"] = active
        if _save_state(state, version):
            return claim
    raise RuntimeError("Could not claim ticket resolution after concurrent state updates")


def _record_failed_attempt(resolution_key: str) -> int:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        count = int(state["attempts"].get(resolution_key, 0)) + 1
        if count >= MAX_CREATION_ATTEMPTS:
            state["attempts"].pop(resolution_key, None)
            if resolution_key not in state["seen"]:
                state["seen"] = (state["seen"] + [resolution_key])[-MAX_SEEN:]
        else:
            state["attempts"][resolution_key] = count
        if _save_state(state, version):
            return count
        time.sleep(0.2)
    return MAX_CREATION_ATTEMPTS


def _append_to(list_name: str, resolution_key: str) -> None:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        if resolution_key in state[list_name]:
            return
        state[list_name] = (state[list_name] + [resolution_key])[-MAX_SEEN:]
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
    and releasing the slot."""
    run_id = os.environ.get("AUTOMATION_RUN_ID", "")
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        record = state["active"].get(resolution_key)
        if not record or record.get("status") == "finalizing":
            return None
        record["status"] = "finalizing"
        record["terminal_status"] = terminal_status
        record["finalizer_run_id"] = run_id
        state["active"][resolution_key] = record
        if _save_state(state, version):
            return record
        time.sleep(0.2)
    return None


def _clear_active(resolution_key: str) -> bool:
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
    """Drop the checkouts a finished resolution no longer needs. The
    conversation is kept as the record of what the agent did, and the
    checkouts are only removed once it is confirmed stopped."""
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
    log(f"  Removed checkouts {workspace_dir}")


def _finish_resolution(record: dict, resolution_key: str) -> None:
    _release_resources(record)
    _append_to("seen", resolution_key)
    _clear_active(resolution_key)


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


_ISSUE_FIELDS = """
  id identifier title description url
  state { name type }
  assignee { id }
  labels { nodes { name } }
"""


def _list_assigned_issues(key: str) -> list[dict]:
    """Open issues in the project assigned or delegated to the bot. Linear
    agent apps are often *delegated* rather than assigned, so both count; if
    this workspace's schema has no `delegate` filter, fall back to assignee."""
    open_filter = {"type": {"nin": ["completed", "canceled"]}}
    project_filter = {"id": {"eq": LINEAR_PROJECT_ID}}
    by_assignee = {"assignee": {"id": {"eq": LINEAR_BOT_USER_ID}}}
    by_delegate = {"delegate": {"id": {"eq": LINEAR_BOT_USER_ID}}}
    query = f"""query($filter: IssueFilter) {{
      issues(filter: $filter, first: 50) {{ nodes {{ {_ISSUE_FIELDS} }} }}
    }}"""
    base = {"project": project_filter, "state": open_filter}
    try:
        data = _linear(key, query, {"filter": {**base, "or": [by_assignee, by_delegate]}})
    except RuntimeError as exc:
        if "delegate" not in str(exc):
            raise
        data = _linear(key, query, {"filter": {**base, **by_assignee}})
    return data["issues"]["nodes"]


def _get_issue(key: str, issue_id: str) -> dict:
    query = f"""query($id: String!) {{ issue(id: $id) {{ {_ISSUE_FIELDS} }} }}"""
    return _linear(key, query, {"id": issue_id})["issue"]


def _assignment_marker(key: str, issue: dict) -> str:
    """The history entry that most recently assigned the bot, so a fresh
    re-assignment is a fresh attempt. Delegation has no such entry."""
    if (issue.get("assignee") or {}).get("id") != LINEAR_BOT_USER_ID:
        return "delegate"
    query = """query($id: String!) {
      issue(id: $id) { history(last: 50) { nodes { id toAssignee { id } } } }
    }"""
    nodes = _linear(key, query, {"id": issue["id"]})["issue"]["history"]["nodes"]
    for node in reversed(nodes):
        if (node.get("toAssignee") or {}).get("id") == LINEAR_BOT_USER_ID:
            return node["id"]
    return "assigned"


def _issue_comments_text(key: str, issue_id: str) -> str:
    query = """query($id: String!) {
      issue(id: $id) { comments(last: 10) { nodes { body createdAt user { name } botActor { name } } } }
    }"""
    nodes = _linear(key, query, {"id": issue_id})["issue"]["comments"]["nodes"]
    if not nodes:
        return "(no comments)"
    nodes.sort(key=lambda c: c.get("createdAt", ""))
    parts = []
    for c in nodes:
        author = (c.get("user") or c.get("botActor") or {}).get("name", "?")
        parts.append(f"{author}: {(c.get('body') or '').strip()}")
    return "\n---\n".join(parts)


def _post_linear_comment(key: str, issue_id: str, body: str) -> None:
    query = """mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }"""
    _linear(key, query, {"input": {"issueId": issue_id, "body": body}})


def _with_ai_disclosure(body: str) -> str:
    disclosure = "_This was posted by an AI agent (OpenHands) in response to being assigned this ticket._"
    body = (body or "").strip()
    if disclosure.lower() in body.lower():
        return body
    return f"{body}\n\n{disclosure}" if body else disclosure


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


# ── Repository preparation ───────────────────────────────────────────────────

def _branch_name(identifier: str) -> str:
    # Containing the identifier is what makes Linear auto-link the branch/PR.
    return f"{BRANCH_PREFIX}{identifier.lower()}"


def _workspace_path(identifier: str) -> Path:
    workspace = Path(os.environ.get("WORKSPACE_BASE", "/workspace")).resolve()
    return workspace / "linear" / identifier.lower()


def _prepare_workspace(identifier: str, base_branches: dict[str, str], branch_name: str) -> Path:
    """Plain, unauthenticated clones of every candidate repo: all are public.
    Only the eventual push needs credentials, and that is the agent's own job
    using its injected GITHUB_TOKEN."""
    root = _workspace_path(identifier)
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

def _build_resolution_prompt(issue: dict, base_branches: dict[str, str], branch_name: str,
                              comments_text: str) -> str:
    identifier = issue["identifier"]
    title = issue.get("title", "(no title)")
    body = (issue.get("description") or "").strip() or "(no description)"
    labels = ", ".join(label.get("name", "") for label in (issue.get("labels") or {}).get("nodes", [])) or "(none)"
    repo_lines = "\n".join(
        f"   - `./{repo.split('/')[1]}` -> {repo} (base `{base}`)" for repo, base in base_branches.items()
    )
    project_name = CONFIG.get("linear_project_name", "")

    return (
        f"You have been assigned Linear ticket {identifier} and are expected to resolve it if "
        "reasonably possible by pushing a fix - or, if a code change is not the right answer, "
        "replying on the ticket with the information it needs.\n\n"
        f"Ticket     : {identifier} \"{title}\"\n"
        f"Project    : {project_name}\n"
        f"Labels     : {labels}\n"
        f"URL        : {issue.get('url', '')}\n"
        f"\nTicket description:\n---\n{body}\n---\n"
        f"\nRecent comments (most recent 10):\n---\n{comments_text}\n---\n\n"
        "Required workflow:\n"
        "1. The workspace contains a fresh clone of each candidate repository, each with a new "
        f"branch `{branch_name}` already checked out:\n{repo_lines}\n"
        "   Do not delete or re-clone them, and do not switch branches. Decide which repo(s) the "
        "ticket actually concerns; leave the others untouched.\n"
        "2. Follow the AGENTS.md guidance of each repo you change and any applicable skills.\n"
        "3. Investigate the ticket in the actual code before concluding anything. Reproduce it if "
        "practical (write or run a test).\n"
        "4. If you can make a correct, reasonably-scoped fix:\n"
        "   a. Implement it, and run the relevant existing tests (and add new ones covering the "
        "fix) until they pass. Do not push with failing tests.\n"
        "   b. Commit your changes with a clear, descriptive commit message.\n"
        "   c. Run `gh auth setup-git` once (GITHUB_TOKEN/GH_TOKEN is already in your environment), "
        f"then `git push -u origin {branch_name}` from each repo you changed. Never put the token in "
        "a remote URL or a command, or print it.\n"
        "   d. Do not open pull requests yourself and do not comment on Linear yourself; a separate "
        "deterministic step opens the PR for every pushed branch and posts on the ticket.\n"
        "5. If you cannot make a confident fix (unclear requirements, needs a design decision, the "
        "ticket is a question or a design/research task, out of scope, etc.), do NOT push any "
        "commits. Instead, make your final response a clear write-up of what you found: the "
        "relevant code, a concrete proposal, and any specific decision needed from a maintainer.\n"
        "6. Your final response is what gets used either way: if you pushed, it becomes the pull "
        "request description (summarize the fix and note any residual risk or follow-up); "
        "otherwise it is posted verbatim as a Linear comment. Do not include an AI disclosure "
        "yourself - it is added automatically."
    )


# ── Processing ───────────────────────────────────────────────────────────────

def _process_new_assignment(linear_key: str, base_branches: dict[str, str], issue: dict,
                             resolution_key: str) -> str | None:
    identifier = issue["identifier"]
    branch_name = _branch_name(identifier)
    print(f"  Starting resolution attempt for {identifier}")
    workspace_dir = None
    try:
        workspace_dir = _prepare_workspace(identifier, base_branches, branch_name)
        comments_text = _issue_comments_text(linear_key, issue["id"])
        prompt = _build_resolution_prompt(issue, base_branches, branch_name, comments_text)
        conv_id = create_conversation(prompt, workspace_dir)
    except Exception as exc:
        if workspace_dir:
            shutil.rmtree(workspace_dir, ignore_errors=True)
        _clear_active(resolution_key)
        attempts = _record_failed_attempt(resolution_key)
        if attempts >= MAX_CREATION_ATTEMPTS:
            print(f"  Resolution setup failed {attempts}x for {identifier}; giving up: {exc}")
            _post_linear_comment(linear_key, issue["id"], _with_ai_disclosure(
                f"⚠️ **OpenHands could not start work on this ticket** after {attempts} attempts.\n\n"
                f"```\n{str(exc)[:500]}\n```"))
        else:
            print(f"  Resolution setup failed (attempt {attempts}/{MAX_CREATION_ATTEMPTS}); "
                  f"slot released for retry: {exc}")
        return None
    _update_active(resolution_key, status="active", conversation_id=conv_id,
                    workspace_dir=str(workspace_dir), branch=branch_name, last_activity=time.time())
    _append_to("seen", resolution_key)
    try:
        _post_linear_comment(linear_key, issue["id"], _with_ai_disclosure(
            f"\U0001f916 **OpenHands is working on this ticket.**\n\n"
            f"View the conversation: {_openhands_url()}/conversations/{conv_id}"))
    except Exception as exc:
        print(f"  Warning: acknowledgement comment failed for {identifier}: {exc}")
    return conv_id


def _check_resolution_completion(record: dict, linear_key: str, gh_token: str,
                                  base_branches: dict[str, str]) -> None:
    resolution_key = record["resolution_key"]
    identifier = record["identifier"]
    issue_id = record["issue_id"]
    status = record.get("status")
    age = time.time() - float(record.get("last_activity") or record.get("claimed_at") or 0.0)

    if age > MAX_ACTIVE_AGE:
        _post_linear_comment(linear_key, issue_id, _with_ai_disclosure(
            f"⚠️ **OpenHands gave up on this ticket** after {MAX_ACTIVE_AGE // 3600}h "
            f"(last state: `{status}`). Unassign and reassign to try again."))
        _finish_resolution(record, resolution_key)
        print(f"  Resolution {resolution_key} exceeded {MAX_ACTIVE_AGE}s in state '{status}'; slot released")
        return

    if status == "finalizing" or status != "active" or age < DONE_DEBOUNCE:
        return

    conv_id = record["conversation_id"]
    try:
        issue = _get_issue(linear_key, issue_id)
    except Exception as exc:
        print(f"  Warning: could not refetch {identifier}: {exc}")
        return
    if (issue.get("state") or {}).get("type") in ("completed", "canceled"):
        print(f"  {identifier} was closed while in progress; releasing slot without posting")
        _finish_resolution(record, resolution_key)
        return

    try:
        conv_status = conversation_status(conv_id)
    except Exception as exc:
        if "-> 404" in str(exc):
            print(f"  Conversation {conv_id} no longer exists; slot released")
            _finish_resolution(record, resolution_key)
            return
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

    if conv_status in {"error", "stuck"}:
        _post_linear_comment(linear_key, issue_id, _with_ai_disclosure(
            f"⚠️ **OpenHands ran into a problem working on this ticket** "
            f"(status: `{conv_status}`).\n\n{final}".strip()))
        _finish_resolution(record, resolution_key)
        return

    # Trust GitHub, not the agent's own claim, for which branches were pushed.
    branch_name = record.get("branch") or _branch_name(identifier)
    pr_lines = []
    for repo, base in base_branches.items():
        try:
            if _commits_ahead(gh_token, repo, base, branch_name) <= 0:
                continue
            title = f"{identifier}: {issue.get('title', '').strip()}"[:250]
            body = _with_ai_disclosure(f"Resolves [{identifier}]({issue.get('url', '')})\n\n{final}".strip())
            pr = _open_pr(gh_token, repo, title, branch_name, base, body)
            pr_lines.append(f"- {repo}: {pr.get('html_url')}")
            print(f"  {identifier}: opened {pr.get('html_url')}")
        except Exception as exc:
            print(f"  Warning: PR handling failed for {identifier} in {repo}: {exc}")
            pr_lines.append(f"- {repo}: pushed `{branch_name}` but could not open the PR (`{str(exc)[:200]}`)")

    if pr_lines:
        _post_linear_comment(linear_key, issue_id, _with_ai_disclosure(
            "✅ **OpenHands opened a pull request for this ticket:**\n\n" + "\n".join(pr_lines)
            + f"\n\nConversation: {_openhands_url()}/conversations/{conv_id}"))
    else:
        _post_linear_comment(linear_key, issue_id, _with_ai_disclosure(
            final or "OpenHands looked into this ticket but did not make a code change."))
        print(f"  {identifier}: no branch pushed; posted the result as a comment")

    _finish_resolution(record, resolution_key)


def main() -> str | None:
    linear_key = get_secret(LINEAR_API_KEY_SECRET)
    gh_token = get_secret("GITHUB_TOKEN")
    base_branches = {repo: _default_branch(gh_token, repo) for repo in REPOS}

    state, _ = _load_state()
    for record in list(state["active"].values()):
        try:
            _check_resolution_completion(record, linear_key, gh_token, base_branches)
        except Exception as exc:
            print(f"  Warning: completion check failed for {record.get('resolution_key')}: {exc}")

    state, _ = _load_state()
    capacity = MAX_CONCURRENT_RESOLUTIONS - len(state["active"])
    print(f"{len(state['active'])}/{MAX_CONCURRENT_RESOLUTIONS} resolution(s) in flight")

    last_conversation_id = None
    issues = _list_assigned_issues(linear_key)
    print(f"{len(issues)} open ticket(s) currently assigned to the bot")
    for issue in issues:
        identifier = issue["identifier"]
        try:
            resolution_key = f"{identifier}:{_assignment_marker(linear_key, issue)}"
            state, _ = _load_state()
            if resolution_key in state["seen"] or resolution_key in state["active"]:
                continue

            if resolution_key not in state["acknowledged"]:
                # Acknowledge as soon as the assignment is seen, independent of
                # whether a slot is free this tick or setup succeeds first try.
                _post_linear_comment(linear_key, issue["id"], _with_ai_disclosure(
                    "\U0001f440 I see this ticket was assigned to me and will start looking into it shortly."))
                _append_to("acknowledged", resolution_key)

            if capacity <= 0:
                continue
            if not _claim_resolution(issue, resolution_key):
                continue
            capacity -= 1
            conv_id = _process_new_assignment(linear_key, base_branches, issue, resolution_key)
            if conv_id:
                last_conversation_id = conv_id
        except Exception as exc:
            # One ticket's failure must not stop the others or fail the run.
            print(f"  Warning: failed evaluating {identifier}: {exc}")
    return last_conversation_id


if __name__ == "__main__":
    try:
        conversation_id = main()
        fire_callback("COMPLETED", conversation_id=conversation_id)
    except Exception as exc:
        fire_callback("FAILED", str(exc))
        raise
