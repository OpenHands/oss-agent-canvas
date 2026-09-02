"""Stale issue nudge (+ auto-close where enabled) across multiple repositories.

Merges the two previously separate single-repo automations. The two repos do
NOT behave identically, so behaviour is driven by per-repo config in REPOS
rather than being flattened:

  OpenHands/OpenHands
    Nudge only. This repo has no MAINTAINERS/CODEOWNERS file and a large
    (21-person) admin/maintain collaborator list, so unassigned issues get a
    summary comment with no @mention rather than guessing at who's relevant.

  OpenHands/software-agent-sdk
    Nudge + close. Unassigned issues tag the repo MAINTAINERS file. Closing
    replaces the close side of .github/workflows/stale.yml, and "genuine"
    inactivity deliberately excludes our own nudge comments - since those bump
    `updated_at` every ~10 days, an issue nobody replies to would otherwise
    look "recently active" forever and never reach the close threshold.
    Issues labeled with anything in `exempt_labels` are never closed (but are
    still nudged).

Both actions are restricted to issues (not PRs). Because posting a comment
bumps GitHub's `updated_at`, an issue naturally won't qualify again until it
has been quiet for another NUDGE_INACTIVITY_DAYS - no separate dedup state.

Each repository is processed independently: a failure on one (for example the
transient `HTTP Error 403: Forbidden` these runs periodically hit) must not
cost the other repository its run, because before the merge they were separate
automations. Failures are collected and re-raised at the end so the run is
still reported as FAILED.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REPOS = [
    {
        "repo": "OpenHands/OpenHands",
        "close_enabled": False,
        "mention_maintainers": False,
    },
    {
        "repo": "OpenHands/software-agent-sdk",
        "close_enabled": True,
        "mention_maintainers": True,
        "exempt_labels": {"roadmap", "backlog", "tracker"},
    },
]

NUDGE_INACTIVITY_DAYS = 10
CLOSE_INACTIVITY_DAYS = 30
MAX_ISSUES_PER_RUN = 6
MAX_CLOSE_PER_RUN = 6
REQUEST_TIMEOUT = 15
REQUEST_RETRIES = 3
RETRY_BACKOFF_SECONDS = (2, 5)


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


def fire_callback(status="COMPLETED", error=None):
    """Signal run completion. MUST be called on every exit path - success AND error."""
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
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


def github_request(token, method, path, params=None, body=None):
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "oss-stale-issue-nudge",
            "Content-Type": "application/json",
        },
        method=method,
    )
    last_error = None
    for attempt in range(REQUEST_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError:
            raise  # a real HTTP error (4xx/5xx) - retrying won't help
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < REQUEST_RETRIES - 1:
                print(f"{method} {path}: {exc}; retrying")
                time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
    raise last_error


def get_current_login(token):
    return github_request(token, "GET", "/user")["login"]


def fetch_maintainers(token, repository):
    data = github_request(token, "GET", f"/repos/{repository}/contents/MAINTAINERS")
    content = base64.b64decode(data["content"]).decode()
    return sorted(set(re.findall(r"^-\s*@(\S+)", content, re.MULTILINE)))


def parse_timestamp(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fetch_stale_issues(token, repository, now):
    """Open issues (not PRs) sorted oldest-updated-first, stopping once we
    reach one that's within the inactivity window (everything after it is
    even fresher). This is the candidate pool for both nudging and closing -
    an issue only drops out of GitHub's own `updated_at` bookkeeping once
    nobody (including our own bot) has touched it for NUDGE_INACTIVITY_DAYS."""
    cutoff = now - timedelta(days=NUDGE_INACTIVITY_DAYS)
    stale = []
    for page in range(1, 6):
        items = github_request(
            token,
            "GET",
            f"/repos/{repository}/issues",
            {"state": "open", "sort": "updated", "direction": "asc", "per_page": 100, "page": page},
        )
        if not items:
            break
        for issue in items:
            if issue.get("pull_request"):
                continue
            if parse_timestamp(issue["updated_at"]) >= cutoff:
                return stale
            stale.append(issue)
        if len(items) < 100:
            break
    return stale


def last_human_activity(token, repository, issue, bot_login):
    """Timestamp of the most recent comment not authored by our own bot
    account, falling back to issue creation if every comment is ours.

    The comments endpoint ignores `direction` and always returns oldest
    first, so we page through everything and scan from the end rather
    than trusting direction=desc to hand us newest-first."""
    comments = []
    for page in range(1, 6):
        batch = github_request(
            token,
            "GET",
            f"/repos/{repository}/issues/{issue['number']}/comments",
            {"per_page": 100, "page": page},
        )
        if not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
    for comment in reversed(comments):
        if comment.get("user", {}).get("login") != bot_login:
            return parse_timestamp(comment["created_at"])
    return parse_timestamp(issue["created_at"])


def build_nudge_comment(issue, now, maintainers, mention_maintainers):
    days_inactive = (now - parse_timestamp(issue["updated_at"])).days
    labels = sorted(label["name"] for label in issue.get("labels", []))
    labels_str = ", ".join(labels) if labels else "none"
    assignees = [f"@{a['login']}" for a in issue.get("assignees", [])]
    if assignees:
        ask = f"{' '.join(assignees)}, could you take a look when you have a chance and help figure out next steps?"
    elif mention_maintainers and maintainers:
        tags = " ".join(f"@{m}" for m in maintainers)
        ask = f"{tags}, could you take a look when you have a chance and help figure out next steps?"
    else:
        # No assignees and nobody safe to @mention: ask without naming anyone
        # rather than guessing. Also the fallback if MAINTAINERS is empty,
        # which would otherwise emit a comment starting with a bare comma.
        ask = "Could someone take a look when they have a chance and help figure out next steps?"
    return (
        f"[Automatic Post]: This issue has had no activity for {days_inactive} days "
        f"(labels: {labels_str}). {ask}\n\n"
        f"_Generated by OpenHands automation on behalf of the user._"
    )


def build_close_comment(days_inactive):
    return (
        f"[Automatic Post]: This issue was automatically closed due to {days_inactive} days "
        f"of inactivity. We do this to help keep the issue tracker manageable and focused "
        f"on active issues. If this is still relevant, feel free to reopen it.\n\n"
        f"_Generated by OpenHands automation on behalf of the user._"
    )


def close_issue(token, repository, issue, days_inactive):
    github_request(
        token,
        "POST",
        f"/repos/{repository}/issues/{issue['number']}/comments",
        body={"body": build_close_comment(days_inactive)},
    )
    github_request(
        token,
        "PATCH",
        f"/repos/{repository}/issues/{issue['number']}",
        body={"state": "closed", "state_reason": "not_planned"},
    )


def process_repo(token, config, now):
    repository = config["repo"]
    close_enabled = config.get("close_enabled", False)
    mention_maintainers = config.get("mention_maintainers", False)
    exempt_labels = config.get("exempt_labels", set())

    stale_issues = fetch_stale_issues(token, repository, now)
    if not stale_issues:
        print(f"[{repository}] No stale issues found")
        return

    # Only needed for the close path; skipping it keeps the nudge-only repo
    # from spending a request (and from failing) on GET /user.
    bot_login = get_current_login(token) if close_enabled else None
    maintainers = None
    closed = 0
    nudged = 0

    limit = (MAX_CLOSE_PER_RUN + MAX_ISSUES_PER_RUN) if close_enabled else MAX_ISSUES_PER_RUN
    for issue in stale_issues[:limit]:
        labels = {label["name"] for label in issue.get("labels", [])}

        if close_enabled and closed < MAX_CLOSE_PER_RUN and not (labels & exempt_labels):
            real_inactive_days = (now - last_human_activity(token, repository, issue, bot_login)).days
            if real_inactive_days >= CLOSE_INACTIVITY_DAYS:
                close_issue(token, repository, issue, real_inactive_days)
                closed += 1
                print(f"[{repository}] #{issue['number']}: closed ({real_inactive_days} days of genuine inactivity)")
                continue

        if nudged < MAX_ISSUES_PER_RUN:
            if mention_maintainers and maintainers is None:
                maintainers = fetch_maintainers(token, repository)
            comment = build_nudge_comment(issue, now, maintainers or [], mention_maintainers)
            github_request(
                token,
                "POST",
                f"/repos/{repository}/issues/{issue['number']}/comments",
                body={"body": comment},
            )
            days_inactive = (now - parse_timestamp(issue["updated_at"])).days
            nudged += 1
            print(f"[{repository}] #{issue['number']}: nudged ({days_inactive} days inactive)")

    skipped = len(stale_issues) - (closed + nudged)
    if skipped > 0:
        print(f"[{repository}] {skipped} additional stale issue(s) left for the next run")


def main():
    token = get_secret("GITHUB_TOKEN")
    now = datetime.now(timezone.utc)
    failures = []
    for config in REPOS:
        repository = config["repo"]
        print(f"=== {repository} ===")
        try:
            process_repo(token, config, now)
        except Exception as exc:
            failures.append(f"{repository}: {exc}")
            print(f"[{repository}] FAILED: {exc}")
    if failures:
        raise RuntimeError("; ".join(failures))


if __name__ == "__main__":
    try:
        main()
        fire_callback()
    except Exception as error:
        fire_callback("FAILED", str(error))
        raise
