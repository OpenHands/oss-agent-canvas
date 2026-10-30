"""
Linear Assistant (webhook) - OpenHands Automation Script

Same behaviour as the polling `linear-assistant`, driven by Linear's webhook
instead of a cron poll. Linear delivers an app-user notification to the
automation service (a custom `linear` webhook); each delivery starts one run of
this script, which handles exactly that one request end to end:

  * @-mention in a comment (or the description) -> start a conversation with
    the ticket plus the request, reply in that comment's thread.
  * ticket assigned to the bot -> start a conversation that tries to solve it.

The webhook payload is only used to learn the notification id and type. The
notification itself is re-read from Linear's API, so the script does not depend
on the exact shape of the webhook body and never trusts it for the request text.

Unlike the poller there is no later tick to finish the work, so the run stays
alive until the conversation reaches a terminal state, then checks GitHub
directly (never trusting the agent's own claim) for pushed branches, opens a
draft PR for each, and posts the reply. Set the automation timeout to at least
`max_active_age_seconds`.

Redeliveries are dropped by the automation service when the webhook is created
with `event_id_header: "Linear-Delivery"`.

Replying needs the Linear token to carry the `comments:create` (or `write`)
scope; reading the notification only needs `read`.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
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
# How long one run waits for its conversation before giving up on it.
MAX_ACTIVE_AGE = int(CONFIG.get("max_active_age_seconds", 3 * 60 * 60))
POLL_INTERVAL = int(CONFIG.get("poll_interval_seconds", 20))
HTTP_TIMEOUT = int(CONFIG.get("http_timeout_seconds", 30))

MENTION_TYPES = {"issueCommentMention", "issueMention"}
ASSIGN_TYPES = {"issueAssignedToYou"}
DONE_DEBOUNCE = 15
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


def _get_notification(key: str, notification_id: str) -> dict:
    query = """query($id: String!) { notification(id: $id) {
      id type createdAt
      ... on IssueNotification {
        issue { id identifier title url team { key } }
        comment { id body user { id name } parent { id } }
        actor { id name }
      }
    } }"""
    return _linear(key, query, {"id": notification_id})["notification"]


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


def _reply(key: str, task: dict, body: str) -> None:
    _post_linear_comment(key, task["issue_id"], _with_ai_disclosure(body), task.get("reply_parent_id"))


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


def _notification_id_from_event(event: dict) -> str | None:
    """The webhook body is only used to find the notification, whose id sits
    under `notification` in Linear's app-user notification payload."""
    notification = event.get("notification") or {}
    return notification.get("id") or event.get("notificationId")


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

def _start_task(linear_key: str, base_branches: dict[str, str], task: dict) -> tuple[str, Path, str]:
    """Create the workspace and conversation; returns (conversation id, workspace, branch)."""
    branch_name = _branch_name(task)
    log(f"Starting {task['kind']} task for {task['identifier']} ({task['key']})")
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
        # A webhook is delivered once, so say so instead of leaving the ticket silent.
        _reply(linear_key, task, f"⚠️ **I could not start working on this.**\n\n```\n{str(exc)[:500]}\n```"
                                 "\n\nMention me again to retry.")
        raise
    try:
        _reply(linear_key, task, f"👀 On it. Follow along: {_openhands_url()}/conversations/{conv_id}")
    except Exception as exc:
        log(f"Warning: acknowledgement failed for {task['identifier']}: {exc}")
    return conv_id, workspace_dir, branch_name


def _wait_for_conversation(conv_id: str) -> str:
    """Block until the conversation is terminal; returns its status, or
    "timeout" once the run has waited `max_active_age_seconds`."""
    deadline = time.time() + MAX_ACTIVE_AGE
    time.sleep(DONE_DEBOUNCE)
    while time.time() < deadline:
        try:
            status = conversation_status(conv_id)
        except Exception as exc:
            if "-> 404" in str(exc):
                return "missing"
            log(f"Warning: could not get status for {conv_id}: {exc}")
        else:
            if status in TERMINAL_STATUSES:
                return status
        time.sleep(POLL_INTERVAL)
    return "timeout"


def _finalize(task: dict, conv_id: str, branch_name: str, conv_status: str, linear_key: str,
              gh_token: str, base_branches: dict[str, str]) -> None:
    conv_link = f"{_openhands_url()}/conversations/{conv_id}"
    identifier = task["identifier"]

    if conv_status == "timeout":
        _reply(linear_key, task, f"⚠️ **I gave up on this** after {MAX_ACTIVE_AGE // 3600}h. "
                                 f"Conversation: {conv_link}\n\nMention me again to retry.")
        return
    if conv_status == "missing":
        log(f"Conversation {conv_id} no longer exists; nothing to report")
        return

    try:
        final = conversation_final_response(conv_id)
    except Exception:
        final = ""

    if conv_status in {"error", "stuck"}:
        _reply(linear_key, task, f"⚠️ **I ran into a problem** (status: `{conv_status}`). "
                                 f"Conversation: {conv_link}\n\n{final}".strip())
        return

    # Trust GitHub, not the agent's own claim, for which branches were pushed.
    pr_lines = []
    for repo, base in base_branches.items():
        try:
            if _commits_ahead(gh_token, repo, base, branch_name) <= 0:
                continue
            title = f"{identifier}: {_get_issue(linear_key, task['issue_id']).get('title', '').strip()}"[:250]
            body = _with_ai_disclosure(f"Resolves [{identifier}]({task.get('url', '')})\n\n{final}".strip())
            pr = _open_pr(gh_token, repo, title, branch_name, base, body)
            pr_lines.append(f"- {repo}: {pr.get('html_url')}")
        except Exception as exc:
            log(f"Warning: PR handling failed for {identifier} in {repo}: {exc}")
            pr_lines.append(f"- {repo}: pushed `{branch_name}` but could not open the PR (`{str(exc)[:200]}`)")

    reply = final or "I looked into this but have nothing to add."
    if pr_lines:
        reply += "\n\n**Draft PR(s):**\n" + "\n".join(pr_lines)
    _reply(linear_key, task, f"{reply}\n\nConversation: {conv_link}")
    log(f"{identifier}: replied ({len(pr_lines)} PR(s))")


def _release_workspace(workspace_dir: Path, conv_id: str) -> None:
    """Drop the checkouts once the conversation is confirmed stopped. The
    conversation is kept as the record of what the agent did."""
    try:
        stopped = conversation_status(conv_id) in TERMINAL_STATUSES
    except Exception as exc:
        stopped = "-> 404" in str(exc)
    if stopped:
        shutil.rmtree(workspace_dir, ignore_errors=True)
    else:
        log(f"Conversation {conv_id} not confirmed stopped; leaving {workspace_dir} in place")


def main() -> str | None:
    payload = json.loads(os.environ.get("AUTOMATION_EVENT_PAYLOAD") or "{}")
    event = payload.get("event") or {}
    notification_id = _notification_id_from_event(event)
    if not notification_id:
        log(f"No notification id in the event (keys: {sorted(event)}); nothing to do")
        return None

    linear_key = get_secret(LINEAR_API_KEY_SECRET)
    notification = _get_notification(linear_key, notification_id)
    task = _task_from_notification(notification) if notification else None
    if not task:
        log(f"Notification {notification_id} is not a request for the assistant; nothing to do")
        return None

    gh_token = get_secret("GITHUB_TOKEN")
    base_branches = {repo: _default_branch(gh_token, repo) for repo in REPOS}

    conv_id, workspace_dir, branch_name = _start_task(linear_key, base_branches, task)
    conv_status = _wait_for_conversation(conv_id)
    try:
        _finalize(task, conv_id, branch_name, conv_status, linear_key, gh_token, base_branches)
    finally:
        _release_workspace(workspace_dir, conv_id)
    return conv_id


if __name__ == "__main__":
    try:
        conversation_id = main()
        fire_callback("COMPLETED", conversation_id=conversation_id)
    except Exception as exc:
        fire_callback("FAILED", str(exc))
        raise
