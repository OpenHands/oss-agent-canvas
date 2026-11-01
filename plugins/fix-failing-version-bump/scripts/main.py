"""
Fix Failing Version Bump - OpenHands Automation Script

Cron-polls one or more GitHub repositories for open pull requests authored by
dependabot (`dependabot[bot]`) whose CI is failing on the head commit, then
spawns an OpenHands conversation to diagnose and fix the failing checks and
push the fix to the dependabot branch.

With `auto_merge_dependabot` enabled, the poll also closes the loop: a
dependabot PR whose CI is green is approved and squash-merged when the change
is a version-only bump or a minimal companion fix, and otherwise a maintainer
is asked to review it, mirroring the code-review automation's handoff. Green
PRs whose CI was failing get here naturally: the pushed fix advances the head
SHA, the next poll sees the new key, and the green head is classified then.

The script is deterministic: PR discovery, dependabot authorship, CI-status
checks, merge-eligibility classification, dedup, and conversation lifecycle
are all handled in Python. The LLM is invoked only for the actual repair.

A fix is attempted at most once per (repo, PR, head SHA): pushing a new
version-bump commit by dependabot is new information and re-triggers the
attempt. A PR whose CI is still running is left alone this poll and retried
next time. Closed/merged PRs are dropped from state.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

# Configuration. The deployed automation configures itself through a
# config.json shipped beside this script (same pattern as the GitHub PR
# Reviewer); the constants stay as the defaults and config.json is the
# override. See load_config() below.
DEFAULT_REPOS = [
    {"repo": "OpenHands/extensions"},
    {"repo": "OpenHands/OpenHands"},
    {"repo": "OpenHands/software-agent-sdk"},
    {"repo": "OpenHands/automation"},
    {"repo": "OpenHands/enterprise"},
]

DEFAULT_OPENHANDS_URL = "https://oss-agent-canvas.ngrok.dev"

# When False (default) the automation only fixes failing CI, exactly as
# before. When True, a dependabot PR with green CI is additionally approved
# and squash-merged if the diff is a version-only dependency bump or a
# minimal companion fix (bounded by the two thresholds below); anything
# larger is handed to a maintainer for review instead.
DEFAULT_AUTO_MERGE_DEPENDABOT = False
DEFAULT_MAINTAINERS: list[str] = []
DEFAULT_MAX_MERGES_PER_RUN = 3
DEFAULT_MINIMAL_FIX_MAX_FILES = 10
DEFAULT_MINIMAL_FIX_MAX_CHANGED_LINES = 100

REPOS = DEFAULT_REPOS
AUTO_MERGE_DEPENDABOT = DEFAULT_AUTO_MERGE_DEPENDABOT
MAINTAINERS: list[str] = list(DEFAULT_MAINTAINERS)
MAX_MERGES_PER_RUN = DEFAULT_MAX_MERGES_PER_RUN
MINIMAL_FIX_MAX_FILES = DEFAULT_MINIMAL_FIX_MAX_FILES
MINIMAL_FIX_MAX_CHANGED_LINES = DEFAULT_MINIMAL_FIX_MAX_CHANGED_LINES

CONFIG_FILENAME = "config.json"

# Config keys, paired with the type each must have. A wrong type is a hard
# error at import: the alternative is silently merging PRs with a boolean
# that never made it through as one.
_CONFIG_TYPES: dict[str, type] = {
    "repos": list,
    "maintainers": list,
    "auto_merge_dependabot": bool,
    "max_merges_per_run": int,
    "minimal_fix_max_files": int,
    "minimal_fix_max_changed_lines": int,
    "openhands_url": str,
}


def load_config(directory: Path | None = None) -> dict:
    """Return the rendered config shipped beside this script, or {} if absent.

    Only the keys above are read; anything else in the file is ignored, so a
    host may ship provenance there without this script caring.
    """
    path = (directory or Path(__file__).resolve().parent) / CONFIG_FILENAME
    if not path.is_file():
        return {}

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{CONFIG_FILENAME} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{CONFIG_FILENAME} must contain a JSON object")

    config = {}
    for key, expected in _CONFIG_TYPES.items():
        if key not in raw:
            continue
        value = raw[key]
        # bool is an int in Python, so an unguarded int check would accept
        # `"max_merges_per_run": true` and then silently clamp to it.
        if not isinstance(value, expected) or (
            expected is not bool and isinstance(value, bool)
        ):
            raise SystemExit(
                f"{CONFIG_FILENAME}: {key} must be {expected.__name__}, "
                f"got {type(value).__name__}"
            )
        config[key] = value
    if "repos" in config and not config["repos"]:
        raise SystemExit(f'{CONFIG_FILENAME}: repos must be a non-empty list of "owner/repo" entries')
    for key in ("max_merges_per_run", "minimal_fix_max_files", "minimal_fix_max_changed_lines"):
        if key in config and config[key] < 1:
            raise SystemExit(f"{CONFIG_FILENAME}: {key} must be at least 1")
    return config


def apply_config(directory: Path | None = None) -> None:
    """Load config.json beside this script into the module constants."""
    global REPOS, AUTO_MERGE_DEPENDABOT, MAINTAINERS
    global MAX_MERGES_PER_RUN, MINIMAL_FIX_MAX_FILES, MINIMAL_FIX_MAX_CHANGED_LINES

    config = load_config(directory)
    if "repos" in config:
        REPOS = [{"repo": repo} for repo in config["repos"]]
    AUTO_MERGE_DEPENDABOT = config.get("auto_merge_dependabot", AUTO_MERGE_DEPENDABOT)
    MAINTAINERS = config.get("maintainers", MAINTAINERS)
    MAX_MERGES_PER_RUN = config.get("max_merges_per_run", MAX_MERGES_PER_RUN)
    MINIMAL_FIX_MAX_FILES = config.get("minimal_fix_max_files", MINIMAL_FIX_MAX_FILES)
    MINIMAL_FIX_MAX_CHANGED_LINES = config.get(
        "minimal_fix_max_changed_lines", MINIMAL_FIX_MAX_CHANGED_LINES
    )


apply_config()

DEPENDABOT_LOGIN = "dependabot[bot]"

DONE_DEBOUNCE = 15
TERMINAL_STATUSES = {"idle", "finished", "error", "stuck"}
HTTP_TIMEOUT = 30
# At most this many fix conversations may be in flight at once, PER REPO.
MAX_CONCURRENT_FIXES = 2
# Handled fix keys, newest last. Bounded so state stays small and a key can
# eventually age out, letting a previously-failed fix be retried after a
# dependabot re-bump.
MAX_SEEN_FIXES = 200


def _get_env_key() -> str:
    return os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")


def get_secret(name: str) -> str:
    url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    key = _get_env_key()
    req = urllib.request.Request(
        f"{url}/api/settings/secrets/{name}",
        headers={"X-Session-API-Key": key},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode().strip()


def fire_callback(
    status: str = "COMPLETED",
    error: str | None = None,
    conversation_id: str | None = None,
) -> None:
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body: dict = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
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
        urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        print(f"Callback error (non-fatal): {exc}")


# ── State persistence (KV store with local-file fallback) ─────────────────────

_KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
_KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")
_STATE_KEY = "state"


def _kv_available() -> bool:
    return bool(_KV_TOKEN and _KV_BASE)


def _kv_get(key: str) -> dict | None:
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{key}",
        headers={"Authorization": f"Bearer {_KV_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read())["value"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _kv_set(key: str, value: dict) -> None:
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{key}",
        data=json.dumps(value).encode(),
        headers={
            "Authorization": f"Bearer {_KV_TOKEN}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        r.read()


def _state_file_path() -> str:
    workspace_base = os.environ.get("WORKSPACE_BASE", "")
    event_payload = json.loads(os.environ.get("AUTOMATION_EVENT_PAYLOAD", "{}"))
    automation_id = event_payload.get("automation_id", "default")

    if workspace_base:
        root = Path(workspace_base).resolve().parent.parent
    else:
        root = Path.home() / ".openhands" / "workspaces"

    state_dir = root / "automation-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return str(state_dir / f"fix_failing_version_bump_{automation_id}.json")


def _default_state() -> dict:
    return {
        "version": 1,
        "repos": {},
    }


def load_state() -> dict:
    if _kv_available():
        data = _kv_get(_STATE_KEY)
        if data is not None:
            print("State loaded from KV store")
            return data
        return _default_state()
    path = _state_file_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: state file {path} unreadable ({exc}); starting fresh")
    return _default_state()


def save_state(state: dict) -> None:
    if _kv_available():
        _kv_set(_STATE_KEY, state)
        print("State saved to KV store")
        return
    path = _state_file_path()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)
    print(f"State saved to {path}")


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _github_request(
    token: str,
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
    accept: str = "application/vnd.github+json",
) -> tuple:
    url = f"https://api.github.com{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read()
        return (json.loads(raw) if raw.strip() else {}), dict(r.headers)


def _github_paginate(token: str, path: str, params: dict | None = None) -> list:
    results: list = []
    page = 1
    base_params = dict(params or {})
    base_params.setdefault("per_page", 100)
    while True:
        base_params["page"] = page
        data, _ = _github_request(token, "GET", path, params=base_params)
        if not isinstance(data, list):
            break
        results.extend(data)
        if len(data) < base_params["per_page"]:
            break
        page += 1
    return results


def _resolve_github_token() -> str:
    try:
        token = get_secret("GITHUB_TOKEN")
        if token:
            return token
    except Exception:
        pass
    raise RuntimeError(
        "GITHUB_TOKEN secret is not set. "
        "Go to OpenHands Settings → Secrets and add your GitHub token."
    )


def _verify_token(token: str) -> str:
    try:
        user_data, _ = _github_request(token, "GET", "/user")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("GITHUB_TOKEN is invalid or expired.") from exc
        raise RuntimeError(f"GitHub /user check failed: {exc.code}") from exc
    login = user_data.get("login", "?")
    print(f"Authenticated as GitHub user: {login}")
    return login


def _verify_repo(token: str, repo: str) -> None:
    try:
        _github_request(token, "GET", f"/repos/{repo}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"Repository '{repo}' is not accessible with the current token.") from exc
        raise RuntimeError(f"GitHub /repos/{repo} check failed: {exc.code}") from exc


def _list_open_prs(token: str, repo: str) -> list[dict]:
    return _github_paginate(
        token,
        f"/repos/{repo}/pulls",
        {"state": "open", "sort": "updated", "direction": "desc"},
    )


def _get_pr(token: str, repo: str, pr_number: int) -> dict:
    pr, _ = _github_request(token, "GET", f"/repos/{repo}/pulls/{pr_number}")
    return pr


def _post_github_comment(token: str, repo: str, pr_number: int, body: str) -> None:
    try:
        _github_request(
            token,
            "POST",
            f"/repos/{repo}/issues/{pr_number}/comments",
            body={"body": body},
        )
    except Exception as exc:
        print(f"  Warning: failed to post comment on PR #{pr_number}: {exc}")


def _ci_status(token: str, repo: str, head_sha: str) -> str:
    """Returns "running", "failed", "clear", or "unknown" for the given commit.

    A commit with no checks configured is "clear". On API errors returns
    "unknown" (treated like "running": skip this poll, re-check next time)
    so a transient network error never looks like CI passing.
    """
    try:
        page = 1
        any_failed = False
        while True:
            data, _ = _github_request(
                token, "GET", f"/repos/{repo}/commits/{head_sha}/check-runs",
                params={"per_page": 100, "page": page},
            )
            runs = data.get("check_runs", [])
            if any(run.get("status") != "completed" for run in runs):
                return "running"
            if any(run.get("conclusion") in ("failure", "timed_out", "action_required") for run in runs):
                any_failed = True
            if len(runs) < 100:
                break
            page += 1
        status_data, _ = _github_request(token, "GET", f"/repos/{repo}/commits/{head_sha}/status")
        if int(status_data.get("total_count", 0)) > 0:
            legacy_state = status_data.get("state")
            if legacy_state == "pending":
                return "running"
            if legacy_state == "failure":
                any_failed = True
        return "failed" if any_failed else "clear"
    except Exception as exc:
        print(f"  Warning: could not determine CI status for {head_sha[:12]}: {exc}")
        return "unknown"


def _failed_check_names(token: str, repo: str, head_sha: str) -> list[str]:
    """Names of check runs that failed on the head commit, for the fix prompt."""
    names: list[str] = []
    try:
        page = 1
        while True:
            data, _ = _github_request(
                token, "GET", f"/repos/{repo}/commits/{head_sha}/check-runs",
                params={"per_page": 100, "page": page},
            )
            for run in data.get("check_runs", []):
                if run.get("status") == "completed" and run.get("conclusion") in (
                    "failure", "timed_out", "action_required",
                ):
                    names.append(run.get("name", "(unnamed)"))
            if len(data.get("check_runs", [])) < 100:
                break
            page += 1
    except Exception as exc:
        print(f"  Warning: could not list failed check names: {exc}")
    return names


# ── Agent server (conversation) helpers ───────────────────────────────────────

def _oh_request(agent_url: str, api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    url = f"{agent_url}{path}"
    headers = {"X-Session-API-Key": api_key, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode()
        raise RuntimeError(f"Agent API {method} {path} → {exc.code}: {body_text}") from exc


def _fetch_settings(agent_url: str, api_key: str) -> dict:
    req = urllib.request.Request(
        f"{agent_url}/api/settings",
        headers={"X-Session-API-Key": api_key, "X-Expose-Secrets": "plaintext"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def _get_agent_dict(agent_url: str, api_key: str) -> dict:
    data = _fetch_settings(agent_url, api_key)
    llm = data.get("agent_settings", {}).get("llm", {})
    return {
        "kind": "Agent",
        "llm": llm,
        "tools": [{"name": "terminal"}, {"name": "file_editor"}],
    }


def _get_mcp_config(agent_url: str, api_key: str) -> dict | None:
    try:
        data = _fetch_settings(agent_url, api_key)
        mcp_config = data.get("agent_settings", {}).get("mcp_config")
        if isinstance(mcp_config, dict) and mcp_config.get("mcpServers"):
            return mcp_config
    except Exception as exc:
        print(f"Warning: could not fetch MCP config: {exc}")
    return None


def _list_secret_names(agent_url: str, api_key: str) -> list[dict]:
    try:
        result = _oh_request(agent_url, api_key, "GET", "/api/settings/secrets")
        return result.get("secrets", [])
    except Exception as exc:
        print(f"Warning: could not list secrets: {exc}")
        return []


def _build_secrets_payload(agent_url: str, api_key: str) -> dict:
    secrets = {}
    for secret in _list_secret_names(agent_url, api_key):
        name = secret.get("name", "")
        if not name:
            continue
        lookup: dict = {
            "kind": "LookupSecret",
            "url": f"/api/settings/secrets/{name}",
        }
        if api_key:
            lookup["headers"] = {"X-Session-API-Key": api_key}
        desc = secret.get("description")
        if desc:
            lookup["description"] = desc
        secrets[name] = lookup
    return secrets


def create_conversation(agent_url: str, api_key: str, initial_message: str) -> str:
    workspace_dir = os.environ.get("WORKSPACE_BASE", "/workspace")
    payload: dict = {
        "workspace": {"working_dir": workspace_dir},
        "agent": _get_agent_dict(agent_url, api_key),
        "initial_message": {"content": [{"text": initial_message}]},
    }
    secrets = _build_secrets_payload(agent_url, api_key)
    if secrets:
        payload["secrets"] = secrets
    mcp_config = _get_mcp_config(agent_url, api_key)
    if mcp_config:
        payload["mcp_config"] = mcp_config
    result = _oh_request(agent_url, api_key, "POST", "/api/conversations", payload)
    return result["id"]


def conversation_status(agent_url: str, api_key: str, conv_id: str) -> str:
    result = _oh_request(agent_url, api_key, "GET", f"/api/conversations/{conv_id}")
    return result.get("execution_status", "unknown")


def conversation_final_response(agent_url: str, api_key: str, conv_id: str) -> str:
    result = _oh_request(agent_url, api_key, "GET", f"/api/conversations/{conv_id}/agent_final_response")
    return result.get("response", "")


def _stop_conversation(agent_url: str, api_key: str, conv_id: str) -> None:
    try:
        _oh_request(agent_url, api_key, "POST", f"/api/conversations/{conv_id}/stop")
    except Exception as exc:
        print(f"  Warning: could not stop conversation {conv_id}: {exc}")


# ── Domain logic ──────────────────────────────────────────────────────────────

def _author_login(pr: dict) -> str:
    return (pr.get("user") or {}).get("login", "")


def _is_dependabot(pr: dict) -> bool:
    return _author_login(pr).lower() == DEPENDABOT_LOGIN.lower()


def _head_sha(pr: dict) -> str:
    return ((pr.get("head") or {}).get("sha") or "").strip()


def _head_branch(pr: dict) -> str:
    return (pr.get("head") or {}).get("ref", "")


def _fix_key(repo: str, pr_number: int, head_sha: str) -> str:
    return f"{repo}#{pr_number}@{head_sha}"


def _with_ai_disclosure(body: str) -> str:
    disclosure = "_This comment was posted by an AI agent (OpenHands)._"
    body = (body or "").strip()
    if disclosure.lower() in body.lower():
        return body
    return f"{body}\n\n{disclosure}" if body else disclosure


# ── Auto-merge (version-only bumps and minimal companion fixes) ───────────────

# Dependency manifests and lockfiles dependabot is allowed to touch, keyed by
# file basename. A PR whose changed files all land here is a version-only bump.
_BUMP_FILENAMES: frozenset[str] = frozenset({
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "bun.lockb", "bun.lock",
    "requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock",
    "Pipfile", "Pipfile.lock", "setup.py", "setup.cfg",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "Gemfile", "Gemfile.lock", ".ruby-version", ".tool-versions",
    "composer.json", "composer.lock", "pom.xml", "build.gradle",
    "build.gradle.kts", "gradle.lockfile", "packages.lock.json",
    "pubspec.yaml", "pubspec.lock", "mix.exs", "mix.lock",
    "Podfile.lock", "Cartfile.resolved", "packages.config",
    "packages.props", "packages.deps.json", "versions.props",
    "flake.nix", "flake.lock",
})


def _is_bump_path(path: str) -> bool:
    """Whether a changed file is a dependency manifest/lockfile."""
    name = path.rsplit("/", 1)[-1].lower()
    if name in {n.lower() for n in _BUMP_FILENAMES}:
        return True
    parts = path.lower().split("/")
    # requirements/*.txt, containers/*/requirements.txt, vendored trees, etc.
    if name.endswith(".txt") and "requirements" in parts:
        return True
    if name.endswith(".lock") or name.endswith(".lockb"):
        return True
    return "vendored" in parts or "vendor" in parts and name.endswith(".sbt")


def _pr_files(token: str, repo: str, number: int) -> list[dict]:
    return _github_paginate(token, f"/repos/{repo}/pulls/{number}/files", {"per_page": 100})


def _classify_bump(token: str, repo: str, pr: dict) -> tuple[str, str]:
    """Classify a green dependabot PR as ("version-only"|"minimal-fix"|"large", reason).

    - "version-only": every changed file is a dependency manifest or lockfile,
      i.e. dependabot's own bump with nothing else riding along.
    - "minimal-fix": the PR carries source changes (typically the agent's
      companion fix for the bumped dependency) but stays small: at most
      MINIMAL_FIX_MAX_FILES files and MINIMAL_FIX_MAX_CHANGED_LINES added+deleted
      lines. The thresholds are the deterministic stand-in for "minimal".
    - "large": anything else needs a maintainer's judgement.
    """
    files = _pr_files(token, repo, pr["number"])
    if not files:
        return "large", "no changed files could be listed for the PR"
    if all(_is_bump_path(f.get("filename", "")) for f in files):
        return "version-only", "the diff only touches dependency manifests and lockfiles"

    changes = sum(int(f.get("additions") or 0) + int(f.get("deletions") or 0) for f in files)
    if len(files) > MINIMAL_FIX_MAX_FILES:
        return "large", (
            f"the diff touches {len(files)} files (beyond the "
            f"{MINIMAL_FIX_MAX_FILES}-file minimal-fix threshold)"
        )
    if changes > MINIMAL_FIX_MAX_CHANGED_LINES:
        return "large", (
            f"the diff changes {changes} lines (beyond the "
            f"{MINIMAL_FIX_MAX_CHANGED_LINES}-line minimal-fix threshold)"
        )
    return "minimal-fix", (
        f"the diff stays within the minimal-fix bounds "
        f"({len(files)} files, {changes} changed lines)"
    )


def _approve_pr(token: str, repo: str, number: int, body: str) -> bool:
    try:
        _github_request(
            token,
            "POST",
            f"/repos/{repo}/pulls/{number}/reviews",
            body={"event": "APPROVE", "body": _with_ai_disclosure(body)},
        )
        return True
    except Exception as exc:
        print(f"  Warning: could not approve {repo} PR #{number}: {exc}")
        return False


def _merge_pr(token: str, repo: str, pr: dict, body: str) -> bool:
    number = pr["number"]
    title = (pr.get("title") or "").strip() or f"#{number}"
    payload = {
        "merge_method": "squash",
        "commit_title": f"{title} (#{number})"[:250],
        "commit_message": _with_ai_disclosure(body),
        "sha": _head_sha(pr),
    }
    try:
        _github_request(token, "PUT", f"/repos/{repo}/pulls/{number}/merge", body=payload)
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        print(f"  Could not merge {repo} PR #{number}: {exc.code} {detail}")
        return False
    except Exception as exc:
        print(f"  Could not merge {repo} PR #{number}: {exc}")
        return False


def _parse_maintainers(value) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in value or []:
        login = str(item).strip().lstrip("@")
        key = login.lower()
        if login and key not in seen and key != DEPENDABOT_LOGIN.lower():
            seen.add(key)
            result.append(login)
    return result


def _request_maintainer_review(token: str, repo: str, pr: dict, reason: str) -> str | None:
    """Ask one configured maintainer to review the PR, like the code-review automation.

    Skips roster members who already reviewed the current head, are already
    requested, or authored the PR. Returns the requested login, or None when
    the roster cannot produce one (the caller comments so a human still sees it).
    """
    roster = _parse_maintainers(MAINTAINERS)
    if not roster:
        return None

    number = pr["number"]
    head_sha = _head_sha(pr)
    author = _author_login(pr).lower()
    requested = {
        (item.get("login") or "").lower() for item in pr.get("requested_reviewers", [])
    }
    reviewed = set()
    try:
        reviews, _ = _github_request(token, "GET", f"/repos/{repo}/pulls/{number}/reviews")
        for review in reviews if isinstance(reviews, list) else []:
            review_sha = review.get("commit_id")
            login = ((review.get("user") or {}).get("login") or "").lower()
            if review_sha == head_sha and login:
                reviewed.add(login)
    except Exception as exc:
        print(f"  Warning: could not list reviews on {repo} PR #{number}: {exc}")

    candidates = [
        login for login in roster
        if login.lower() not in requested
        and login.lower() not in reviewed
        and login.lower() != author
    ]
    for login in candidates:
        try:
            _github_request(
                token,
                "POST",
                f"/repos/{repo}/pulls/{number}/requested_reviewers",
                body={"reviewers": [login]},
            )
            return login
        except Exception as exc:
            print(f"  Warning: could not request review from {login} on {repo} PR #{number}: {exc}")
    return None


def _maintainer_handoff_comment(login: str | None, reason: str) -> str:
    mention = f"@{login}" if login else "maintainers"
    return (
        f"{mention} — this dependabot PR's CI is green, but the auto-merge policy "
        f"skipped it because {reason}. Please review and decide whether to merge."
    )


def _maybe_merge_green_dependabot(
    github_token: str,
    repo: str,
    pr: dict,
    head_sha: str,
    fixes: dict,
    merges: dict,
) -> bool:
    """Approve and merge a green dependabot PR, or hand it to a maintainer.

    Returns True when the PR was merged this poll. Every other outcome (still
    unclassified, handed off, merge refused) records the head key in `fixes`
    with a terminal status so later polls do not re-approve or re-ask.
    """
    number = pr["number"]
    key = _fix_key(repo, number, head_sha)
    kind, reason = _classify_bump(github_token, repo, pr)
    print(f"  PR #{number} (dependabot, CI green) classified: {kind} — {reason}")

    if kind == "large":
        login = _request_maintainer_review(github_token, repo, pr, reason)
        comment = _maintainer_handoff_comment(login, reason)
        _post_github_comment(github_token, repo, number, _with_ai_disclosure(comment))
        fixes[key] = {
            "repo": repo,
            "pr_number": number,
            "head_sha": head_sha,
            "status": "handoff",
            "maintainer": login,
            "last_activity": time.time(),
        }
        return False

    if not _approve_pr(
        github_token, repo, number,
        f"✅ Auto-approved by OpenHands: CI is green and {reason}.",
    ):
        fixes[key] = {
            "repo": repo, "pr_number": number, "head_sha": head_sha,
            "status": "handoff", "last_activity": time.time(),
        }
        return False

    merge_body = (
        f"Auto-merged by OpenHands after a green CI run. {reason.capitalize()}."
        f"\n\nApproved-by: all-hands-bot"
    )
    if _merge_pr(github_token, repo, pr, merge_body):
        merges[key] = {
            "repo": repo,
            "pr_number": number,
            "head_sha": head_sha,
            "classification": kind,
            "merged_at": time.time(),
        }
        print(f"  Merged {repo} PR #{number} at {head_sha[:12]} ({kind})")
        return True

    # Approved but not merged (branch protection, conflict, stale head, ...).
    # Leave the approval in place and let a maintainer close the loop.
    _post_github_comment(
        github_token, repo, number,
        _with_ai_disclosure(
            "✅ OpenHands approved this PR (CI green, version-only/minimal change) but "
            "GitHub refused the merge. A maintainer merge may be needed."
        ),
    )
    fixes[key] = {
        "repo": repo, "pr_number": number, "head_sha": head_sha,
        "status": "handoff", "last_activity": time.time(),
    }
    return False


def _build_fix_prompt(repo: str, pr: dict, head_sha: str, head_branch: str, failed_checks: list[str]) -> str:
    number = pr.get("number", "?")
    title = pr.get("title", "(no title)")
    body = (pr.get("body") or "").strip() or "(no description)"
    html_url = pr.get("html_url", "")
    base_branch = (pr.get("base") or {}).get("ref", "?")
    clone_url = f"https://github.com/{repo}.git"
    checks_str = "\n".join(f"- {c}" for c in failed_checks) or "- (names unavailable; inspect the check runs via the API)"

    return (
        "You are an AI software engineer. A dependabot version-bump pull request's CI is "
        "failing, and your job is to diagnose the failure and push a fix commit to the "
        "dependabot branch so CI goes green. Dependabot only edits dependency manifests "
        "(package.json, requirements.txt, pyproject.toml, Cargo.toml, go.mod, etc.) and "
        "lockfiles; the failure is almost always a downstream incompatibility introduced by "
        "the bumped version, not a mistake in the bump itself.\n\n"
        f"Repository : {repo}\n"
        f"Clone URL  : {clone_url}\n"
        f"PR #{number}: \"{title}\"\n"
        f"Author     : @{_author_login(pr)} (dependabot)\n"
        f"Base → Head: {base_branch} ← {head_branch}\n"
        f"Head SHA   : {head_sha}\n"
        f"URL        : {html_url}\n"
        f"\nFailing checks on {head_sha[:12]}:\n{checks_str}\n"
        f"\nPR Description:\n---\n{body}\n---\n\n"
        "Required workflow:\n"
        "1. Clone the repository into a fresh working directory inside the workspace.\n"
        f"   Example: `git clone {clone_url} fix-{number}` then `cd fix-{number}`.\n"
        "   Use the GITHUB_TOKEN secret for any authenticated git/gh operations; never print it.\n"
        "2. Fetch and check out the PR head branch, then verify HEAD matches the SHA above.\n"
        f"   Example: `git fetch origin {head_branch}` then `git checkout {head_branch}` then "
        f"`git rev-parse HEAD` (must equal {head_sha}).\n"
        "3. Identify the failing checks. Use `gh run list` / `gh run view` / `gh api` with "
        "GITHUB_TOKEN, or the GitHub REST API, to fetch logs for the failed check runs. "
        "Do NOT rerun CI yet.\n"
        "4. Read the failing log and pinpoint the root cause. Correlate it with the dependency "
        "version change in the PR diff (`gh pr diff` or `git diff` against the base branch).\n"
        "5. Make the minimal source fix needed for the bumped dependency to work. Prefer "
        "adapting call sites, types, or config over pinning or reverting the version bump. "
        "Do NOT modify the manifest/lockfile bump itself unless the bump is genuinely wrong "
        "(e.g. a yanked release) — explain such a revert explicitly.\n"
        "6. Run the repo's own test/lint/build commands locally to confirm the fix works "
        "before pushing. Only push once local checks pass (or are clearly unrelated).\n"
        f"7. Commit the fix with a clear message and push to `{head_branch}`. Dependabot owns "
        "this branch; pushing a follow-up commit is expected and safe.\n"
        "8. Optionally rerun just the failed CI jobs with `gh run rerun <id> --failed`.\n"
        "9. Report back concisely: what was failing, the root cause, the fix you pushed "
        "(commit SHA), and the resulting CI status. Do NOT close or merge the PR.\n\n"
        "Constraints:\n"
        "- Push only to the PR head branch listed above. Never force-push, never push to the "
        "base branch, never merge or close the PR.\n"
        "- Keep the change minimal and focused on restoring CI. Do not reformat unrelated code.\n"
        "- If after genuine effort the failure cannot be fixed from source (e.g. an upstream "
        "bug in the released dependency), say so clearly and leave the PR open rather than "
        "reverting the bump silently.\n"
        "- Delete the cloned working directory you created in step 1 before finishing.\n"
    )


def _process_fix_request(
    github_token: str,
    agent_url: str,
    api_key: str,
    openhands_url: str,
    repo: str,
    pr: dict,
    head_sha: str,
    fixes: dict,
) -> str | None:
    number = pr["number"]
    head_branch = _head_branch(pr)
    title = pr.get("title", "(no title)")
    key = _fix_key(repo, number, head_sha)

    print(f"  Queuing fix for {repo} PR #{number} at {head_sha[:12]}: {title}")
    failed_checks = _failed_check_names(github_token, repo, head_sha)
    prompt = _build_fix_prompt(repo, pr, head_sha, head_branch, failed_checks)

    try:
        conv_id = create_conversation(agent_url, api_key, prompt)
    except Exception as exc:
        print(f"  Error creating fix conversation for {repo} PR #{number}: {exc}")
        return None

    fixes[key] = {
        "repo": repo,
        "pr_number": number,
        "head_sha": head_sha,
        "head_branch": head_branch,
        "status": "active",
        "conversation_id": conv_id,
        "last_activity": time.time(),
    }
    print(f"  Created fix conversation {conv_id}")

    conv_url = f"{openhands_url}/conversations/{conv_id}"
    _post_github_comment(
        github_token,
        repo,
        number,
        _with_ai_disclosure(
            "🤖 **OpenHands is fixing the failing CI on this dependabot version bump.**\n\n"
            f"Head commit: `{head_sha}`\n"
            f"View the conversation: {conv_url}"
        ),
    )
    return conv_id


def _check_fix_completion(
    rec: dict,
    latest_open_prs: dict[int, dict],
    repo: str,
    github_token: str,
    agent_url: str,
    api_key: str,
) -> None:
    if (time.time() - rec.get("last_activity", 0.0)) < DONE_DEBOUNCE:
        return

    conv_id = rec["conversation_id"]
    pr_number = rec["pr_number"]
    reviewed_sha = rec.get("head_sha", "")
    current_pr = latest_open_prs.get(pr_number)

    if not current_pr:
        rec["status"] = "closed"
        _stop_conversation(agent_url, api_key, conv_id)
        print(f"  {repo} PR #{pr_number} closed/merged — abandoning fix")
        return

    current_sha = _head_sha(current_pr)
    # If the head advanced (e.g. OpenHands already pushed, or dependabot re-bumped),
    # mark the prior attempt done; a new key will pick up the new SHA.
    if current_sha and reviewed_sha and current_sha != reviewed_sha:
        rec["status"] = "closed"
        print(f"  {repo} PR #{pr_number} head advanced {reviewed_sha[:12]} → {current_sha[:12]}; prior fix attempt closed")
        return

    try:
        status = conversation_status(agent_url, api_key, conv_id)
    except Exception as exc:
        print(f"  Warning: could not get status for {conv_id}: {exc}")
        return

    print(f"  {repo} PR #{pr_number} conversation {conv_id} → status={status}")
    if status not in TERMINAL_STATUSES:
        return

    try:
        final = conversation_final_response(agent_url, api_key, conv_id)
    except Exception:
        final = ""

    if status in {"error", "stuck"}:
        comment_body = _with_ai_disclosure(
            f"⚠️ **OpenHands encountered a problem** fixing the failing CI at commit "
            f"`{reviewed_sha[:12]}` (status: `{status}`).\n\n{final}".strip()
        )
    else:
        comment_body = _with_ai_disclosure(
            final
            or f"✅ **OpenHands finished working on the failing CI for commit `{reviewed_sha[:12]}`.** "
            "See the conversation for details."
        )

    _post_github_comment(github_token, repo, pr_number, comment_body)
    rec["status"] = "closed"
    rec["completed_at"] = time.time()
    print(f"  Posted fix summary for {repo} PR #{pr_number} at {reviewed_sha[:12]}")


def _trim_seen(repo_state: dict) -> None:
    fixes = repo_state.setdefault("fixes", {})
    if len(fixes) > MAX_SEEN_FIXES:
        # Drop oldest closed entries first.
        closed = sorted(
            (k for k, v in fixes.items() if v.get("status") != "active"),
            key=lambda k: fixes[k].get("last_activity", 0),
        )
        for k in closed[: len(fixes) - MAX_SEEN_FIXES]:
            del fixes[k]

    merges = repo_state.setdefault("merges", {})
    if len(merges) > MAX_SEEN_FIXES:
        # Drop oldest merged keys first; a merged PR cannot reopen.
        oldest = sorted(merges, key=lambda k: merges[k].get("merged_at", 0))
        for k in oldest[: len(merges) - MAX_SEEN_FIXES]:
            del merges[k]


def _process_repo(
    repo: str,
    github_token: str,
    agent_url: str,
    api_key: str,
    openhands_url: str,
    state: dict,
) -> str | None:
    print(f"=== {repo} ===")
    try:
        _verify_repo(github_token, repo)
    except Exception as exc:
        print(f"  Skipping {repo}: {exc}")
        return None

    repo_state = state.setdefault("repos", {}).setdefault(repo, {"fixes": {}, "merges": {}})
    fixes: dict = repo_state.setdefault("fixes", {})
    merges: dict = repo_state.setdefault("merges", {})

    open_prs = _list_open_prs(github_token, repo)
    latest_open_prs = {pr["number"]: pr for pr in open_prs}
    print(f"Found {len(open_prs)} open PR(s) in {repo}")

    last_conversation_id = None
    active_count = sum(1 for v in fixes.values() if v.get("status") == "active")
    merged_count = 0

    for pr in open_prs:
        number = pr["number"]
        head_sha = _head_sha(pr)

        if not _is_dependabot(pr):
            continue
        if not head_sha:
            print(f"  PR #{number} has no head SHA; skipping")
            continue

        key = _fix_key(repo, number, head_sha)
        if key in fixes:
            rec = fixes[key]
            print(f"  PR #{number} (dependabot) at {head_sha[:12]} already handled ({rec.get('status')})")
            continue

        fresh_pr = _get_pr(github_token, repo, number)
        fresh_head_sha = _head_sha(fresh_pr)
        if fresh_head_sha != head_sha:
            print(f"  PR #{number} head changed during poll ({head_sha[:12]} → {fresh_head_sha[:12]}); using latest")
            head_sha = fresh_head_sha
            key = _fix_key(repo, number, head_sha)
            if key in fixes:
                continue
        if not _is_dependabot(fresh_pr):
            print(f"  PR #{number} no longer by dependabot; skipping")
            continue

        ci = _ci_status(github_token, repo, head_sha)
        print(f"  PR #{number} (dependabot) at {head_sha[:12]} CI={ci}")
        if ci in ("running", "unknown"):
            continue

        if AUTO_MERGE_DEPENDABOT and ci == "clear":
            # A green head: merge it if it is a version-only bump or a minimal
            # companion fix, otherwise hand it to a maintainer. PRs whose CI
            # was fixed by this automation arrive here on a later poll, once
            # the pushed fix has advanced the head SHA.
            if merged_count >= MAX_MERGES_PER_RUN:
                print(f"  PR #{number} mergeable but {merged_count} merged this run (max {MAX_MERGES_PER_RUN}); deferring")
                continue
            if _maybe_merge_green_dependabot(github_token, repo, fresh_pr, head_sha, fixes, merges):
                merged_count += 1
            continue
        if ci == "clear":
            continue

        if active_count >= MAX_CONCURRENT_FIXES:
            print(f"  PR #{number} needs a fix but {active_count} in-flight (max {MAX_CONCURRENT_FIXES}); deferring")
            continue

        conv_id = _process_fix_request(
            github_token, agent_url, api_key, openhands_url, repo, fresh_pr, head_sha, fixes,
        )
        if conv_id:
            last_conversation_id = conv_id
            active_count += 1

    for rec in list(fixes.values()):
        if rec.get("status") != "active":
            continue
        _check_fix_completion(rec, latest_open_prs, repo, github_token, agent_url, api_key)

    _trim_seen(repo_state)
    repo_state["updated_at"] = time.time()
    return last_conversation_id


def main() -> str | None:
    state = load_state()
    agent_url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    api_key = _get_env_key()

    github_token = _resolve_github_token()
    _verify_token(github_token)

    try:
        openhands_url = get_secret("OPENHANDS_URL").rstrip("/") or DEFAULT_OPENHANDS_URL
    except Exception:
        openhands_url = DEFAULT_OPENHANDS_URL

    last_conversation_id = None
    for entry in REPOS:
        repo = entry["repo"]
        try:
            conv_id = _process_repo(repo, github_token, agent_url, api_key, openhands_url, state)
            if conv_id:
                last_conversation_id = conv_id
        except Exception as exc:
            print(f"Error processing {repo}: {exc}")

    state["updated_at"] = time.time()
    save_state(state)
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
