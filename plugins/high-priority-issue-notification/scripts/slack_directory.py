"""Resolve GitHub maintainers to Slack user IDs, cached in the automation KV store.

The notification prompt names maintainers with real Slack mentions (`<@U…>`),
which requires a GitHub-login -> Slack-user-ID mapping. Resolving that mapping
means matching each maintainer against the workspace's Slack directory, which is
expensive (the OpenHands workspace has several thousand members). The result is
therefore cached in the automation KV store so later runs only resolve logins
they have not seen before.

Matching is deliberately conservative — a wrong mention pings the wrong person,
which is worse than falling back to a plain `@login`:

  1. Slack username equals the GitHub login (normalized).
  2. Slack real/display name equals the maintainer's GitHub display name.
  3. A username derived from the GitHub display name (`first.last`,
     `firstlast`, `flast`) equals a Slack username.
  4. A commit-author email from either reviewed repo resolves via
     `users.lookupByEmail`.

Anything still unresolved is left out of the map; the prompt falls back to the
plain GitHub handle for those.
"""

import json
import os
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

SLACK_API = "https://slack.com/api"
GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 30

KV_KEY = "github_slack_map_v1"

_KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
_KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")

_BOT_SUFFIX = "[bot]"


def normalize(value):
    """Casefold, strip accents, and drop everything that is not alphanumeric."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in decomposed.casefold() if ch.isalnum())


def is_bot(login):
    return not login or login.endswith(_BOT_SUFFIX)


def _request(url, headers, *, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read())


# ── KV cache ──────────────────────────────────────────────────────────────────

def kv_available():
    return bool(_KV_TOKEN and _KV_BASE)


def kv_load():
    """Return the cached {map, updated_at} or None when absent/unavailable."""
    if not kv_available():
        return None
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{KV_KEY}",
        headers={"Authorization": f"Bearer {_KV_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            value = json.loads(response.read()).get("value")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return value if isinstance(value, dict) else None


def kv_save(state):
    if not kv_available():
        return
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{KV_KEY}",
        data=json.dumps(state).encode(),
        headers={
            "Authorization": f"Bearer {_KV_TOKEN}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        response.read()


# ── Slack directory ───────────────────────────────────────────────────────────

def _slack_get(token, endpoint, params=None):
    url = f"{SLACK_API}/{endpoint}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return _request(url, {"Authorization": f"Bearer {token}"})


def fetch_slack_directory(token):
    """Return every human member of the workspace, paging through users.list."""
    members = []
    cursor = ""
    while True:
        params = {"limit": "999"}
        if cursor:
            params["cursor"] = cursor
        payload = _slack_get(token, "users.list", params)
        if not payload.get("ok"):
            raise RuntimeError(f"users.list failed: {payload.get('error')}")
        for member in payload.get("members", []):
            if member.get("deleted") or member.get("is_bot") or member.get("id") == "USLACKBOT":
                continue
            members.append(member)
        cursor = (payload.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return members


def build_index(members):
    """Index members by normalized Slack username and by normalized real name."""
    by_username = {}
    by_realname = {}
    for member in members:
        profile = member.get("profile") or {}
        username = normalize(member.get("name"))
        if username and username not in by_username:
            by_username[username] = member
        for candidate in (profile.get("real_name"), profile.get("display_name")):
            key = normalize(candidate)
            if key and key not in by_realname:
                by_realname[key] = member
    return by_username, by_realname


def _name_variants(github_name):
    """Slack-username candidates derived from a GitHub display name."""
    parts = [normalize(p) for p in (github_name or "").split() if normalize(p)]
    if len(parts) < 2:
        return []
    first, last = parts[0], parts[-1]
    return [f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}"]


def match_member(login, github_name, by_username, by_realname):
    """Return the Slack member for a maintainer, or None. Never guesses loosely."""
    member = by_username.get(normalize(login))
    if member:
        return member
    if github_name:
        member = by_realname.get(normalize(github_name))
        if member:
            return member
        for variant in _name_variants(github_name):
            member = by_username.get(variant)
            if member:
                return member
    return None


def lookup_by_email(token, email):
    try:
        payload = _slack_get(token, "users.lookupByEmail", {"email": email})
    except urllib.error.HTTPError:
        return None
    if not payload.get("ok"):
        return None
    return payload.get("user")


def _github_json(url, token):
    return _request(
        url,
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "openhands-automation",
        },
    )


def fetch_maintainers(github_token, repos):
    """Return the set of human collaborators with push access across the repos."""
    maintainers = set()
    for repo in repos:
        page = 1
        while True:
            url = (
                f"{GITHUB_API}/repos/{repo}/collaborators"
                f"?permission=push&per_page=100&page={page}"
            )
            batch = _github_json(url, github_token)
            for collaborator in batch:
                login = collaborator.get("login")
                if not is_bot(login):
                    maintainers.add(login)
            if len(batch) < 100:
                break
            page += 1
    return maintainers


def fetch_display_name(github_token, login):
    """Return the GitHub profile display name for a login, or '' if unknown."""
    try:
        profile = _github_json(f"{GITHUB_API}/users/{login}", github_token)
    except urllib.error.HTTPError:
        return ""
    return profile.get("name") or ""


def _commit_emails(github_token, login, repos):
    for repo in repos:
        try:
            commits = _github_json(
                f"{GITHUB_API}/repos/{repo}/commits?author={login}&per_page=10",
                github_token,
            )
        except urllib.error.HTTPError:
            continue
        for commit in commits:
            author = (commit.get("commit") or {}).get("author") or {}
            email = author.get("email")
            if email and not email.endswith("@users.noreply.github.com"):
                yield email


def build_map(github_token, slack_token, repos, *, log=None):
    """Resolve maintainers to Slack IDs, reusing and refreshing the KV cache.

    Returns {github_login: slack_user_id}. Raises only when there is no cached
    map and the Slack directory cannot be fetched — callers treat the map as
    optional and fall back to plain GitHub handles.
    """
    log = log or (lambda _msg: None)
    state = None
    try:
        state = kv_load()
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        log(f"slack_directory: KV read failed, continuing uncached: {exc}")

    cached_map = dict((state or {}).get("map") or {})

    maintainers = fetch_maintainers(github_token, repos)
    log(f"slack_directory: {len(maintainers)} maintainers across {len(repos)} repos")

    unresolved = sorted(login for login in maintainers if login not in cached_map)
    if not unresolved:
        log("slack_directory: all maintainers already resolved in cache")
        return cached_map

    members = fetch_slack_directory(slack_token)
    by_username, by_realname = build_index(members)
    log(f"slack_directory: indexed {len(members)} Slack members")

    resolved = dict(cached_map)
    for login in unresolved:
        name = fetch_display_name(github_token, login)
        member = match_member(login, name, by_username, by_realname)
        if member is None:
            for email in _commit_emails(github_token, login, repos):
                member = lookup_by_email(slack_token, email)
                if member:
                    break
        if member:
            resolved[login] = member["id"]

    try:
        kv_save({"updated_at": time.time(), "map": resolved})
        log(f"slack_directory: cached {len(resolved)} mappings")
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        log(f"slack_directory: KV write failed, continuing: {exc}")

    return resolved
