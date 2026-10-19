"""Stale issue nudge + auto-close for OpenHands/software-agent-sdk.

Two independent actions per run, both restricted to issues (not PRs):

1. Nudge: posts one comment on each open issue that has had no activity
   for NUDGE_INACTIVITY_DAYS, tagging its assignees (or the repo
   MAINTAINERS if unassigned). Replaces the 'needs-triage' tagging step
   in .github/workflows/assign-reviews.yml, which reposted the same
   maintainer ping on every scheduled run regardless of how recently an
   issue had already been nudged. Because posting a comment bumps
   GitHub's `updated_at` for the issue, an issue naturally won't qualify
   again until it has been quiet for another NUDGE_INACTIVITY_DAYS - no
   separate dedup state needed.

2. Close: closes issues that have had no *genuine* activity for
   CLOSE_INACTIVITY_DAYS, replacing the close side of
   .github/workflows/stale.yml. "Genuine" deliberately excludes our own
   nudge comments - since those bump `updated_at` every ~10 days, an
   issue nobody replies to would otherwise look "recently active"
   forever and never reach the close threshold. Instead this looks at
   the most recent comment not authored by our own bot account.
   Issues labeled with anything in EXEMPT_LABELS are never closed (but
   are still nudged).
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

REPOSITORY = "OpenHands/software-agent-sdk"
NUDGE_INACTIVITY_DAYS = 10
CLOSE_INACTIVITY_DAYS = 30
EXEMPT_LABELS = {"roadmap", "backlog", "tracker"}
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


def fetch_maintainers(token):
    data = github_request(token, "GET", f"/repos/{REPOSITORY}/contents/MAINTAINERS")
    content = base64.b64decode(data["content"]).decode()
    return sorted(set(re.findall(r"^-\s*@(\S+)", content, re.MULTILINE)))


def parse_timestamp(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fetch_stale_issues(token, now):
    """Open issues (not PRs) sorted oldest-updated-first, stopping once we
    reach one that's within the inactivity window (everything after it is
    even fresher). This is the candidate pool for both nudging and
    closing - an issue only drops out of GitHub's own `updated_at`
    bookkeeping once nobody (including our own bot) has touched it for
    NUDGE_INACTIVITY_DAYS."""
    cutoff = now - timedelta(days=NUDGE_INACTIVITY_DAYS)
    stale = []
    for page in range(1, 6):
        items = github_request(
            token,
            "GET",
            f"/repos/{REPOSITORY}/issues",
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


def last_human_activity(token, issue, bot_login):
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
            f"/repos/{REPOSITORY}/issues/{issue['number']}/comments",
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


def build_nudge_comment(issue, now, maintainers):
    days_inactive = (now - parse_timestamp(issue["updated_at"])).days
    labels = sorted(label["name"] for label in issue.get("labels", []))
    labels_str = ", ".join(labels) if labels else "none"
    assignees = [f"@{a['login']}" for a in issue.get("assignees", [])]
    tags = " ".join(assignees) if assignees else " ".join(f"@{m}" for m in maintainers)
    return (
        f"[Automatic Post]: This issue has had no activity for {days_inactive} days "
        f"(labels: {labels_str}). {tags}, could you take a look when you have a chance "
        f"and help figure out next steps?\n\n"
        f"_Generated by OpenHands automation on behalf of the user._"
    )


def build_close_comment(days_inactive):
    return (
        f"[Automatic Post]: This issue was automatically closed due to {days_inactive} days "
        f"of inactivity. We do this to help keep the issue tracker manageable and focused "
        f"on active issues. If this is still relevant, feel free to reopen it.\n\n"
        f"_Generated by OpenHands automation on behalf of the user._"
    )


def close_issue(token, issue, days_inactive):
    github_request(
        token,
        "POST",
        f"/repos/{REPOSITORY}/issues/{issue['number']}/comments",
        body={"body": build_close_comment(days_inactive)},
    )
    github_request(
        token,
        "PATCH",
        f"/repos/{REPOSITORY}/issues/{issue['number']}",
        body={"state": "closed", "state_reason": "not_planned"},
    )


def main():
    token = get_secret("GITHUB_TOKEN")
    now = datetime.now(timezone.utc)
    stale_issues = fetch_stale_issues(token, now)
    if not stale_issues:
        print("No stale issues found")
        return

    bot_login = get_current_login(token)
    maintainers = None
    closed = 0
    nudged = 0

    for issue in stale_issues[: MAX_CLOSE_PER_RUN + MAX_ISSUES_PER_RUN]:
        labels = {label["name"] for label in issue.get("labels", [])}

        if closed < MAX_CLOSE_PER_RUN and not (labels & EXEMPT_LABELS):
            real_inactive_days = (now - last_human_activity(token, issue, bot_login)).days
            if real_inactive_days >= CLOSE_INACTIVITY_DAYS:
                close_issue(token, issue, real_inactive_days)
                closed += 1
                print(f"#{issue['number']}: closed ({real_inactive_days} days of genuine inactivity)")
                continue

        if nudged < MAX_ISSUES_PER_RUN:
            if maintainers is None:
                maintainers = fetch_maintainers(token)
            comment = build_nudge_comment(issue, now, maintainers)
            github_request(
                token,
                "POST",
                f"/repos/{REPOSITORY}/issues/{issue['number']}/comments",
                body={"body": comment},
            )
            days_inactive = (now - parse_timestamp(issue["updated_at"])).days
            nudged += 1
            print(f"#{issue['number']}: nudged ({days_inactive} days inactive)")

    skipped = len(stale_issues) - (closed + nudged)
    if skipped > 0:
        print(f"{skipped} additional stale issue(s) left for the next run")


if __name__ == "__main__":
    try:
        main()
        fire_callback()
    except Exception as error:
        fire_callback("FAILED", str(error))
        raise
