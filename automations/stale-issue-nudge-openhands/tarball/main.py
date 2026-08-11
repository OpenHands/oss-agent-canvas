"""Stale issue nudge for OpenHands/OpenHands.

Posts one nudge comment on each open issue that has had no activity for
INACTIVITY_DAYS, tagging its assignees if any exist. Unlike the
software-agent-sdk sibling of this automation, this repo has no
MAINTAINERS/CODEOWNERS file and a large (21-person) admin/maintain
collaborator list, so unassigned issues get a summary comment with no
@mention rather than guessing at who's relevant. Because posting a
comment bumps GitHub's `updated_at` for the issue, an issue naturally
won't qualify again until it has been quiet for another
INACTIVITY_DAYS - no separate dedup state needed.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REPOSITORY = "OpenHands/OpenHands"
INACTIVITY_DAYS = 10
MAX_ISSUES_PER_RUN = 6
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


def parse_timestamp(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fetch_stale_issues(token, now):
    """Open issues (not PRs) sorted oldest-updated-first, stopping once we
    reach one that's within the inactivity window (everything after it is
    even fresher)."""
    cutoff = now - timedelta(days=INACTIVITY_DAYS)
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


def build_comment(issue, now):
    days_inactive = (now - parse_timestamp(issue["updated_at"])).days
    labels = sorted(label["name"] for label in issue.get("labels", []))
    labels_str = ", ".join(labels) if labels else "none"
    assignees = [f"@{a['login']}" for a in issue.get("assignees", [])]
    if assignees:
        ask = f"{' '.join(assignees)}, could you take a look when you have a chance and help figure out next steps?"
    else:
        ask = "Could someone take a look when they have a chance and help figure out next steps?"
    return (
        f"[Automatic Post]: This issue has had no activity for {days_inactive} days "
        f"(labels: {labels_str}). {ask}\n\n"
        f"_Generated by OpenHands automation on behalf of the user._"
    )


def main():
    token = get_secret("GITHUB_TOKEN")
    now = datetime.now(timezone.utc)
    stale_issues = fetch_stale_issues(token, now)
    if not stale_issues:
        print("No stale issues found")
        return
    for issue in stale_issues[:MAX_ISSUES_PER_RUN]:
        comment = build_comment(issue, now)
        github_request(
            token,
            "POST",
            f"/repos/{REPOSITORY}/issues/{issue['number']}/comments",
            body={"body": comment},
        )
        days_inactive = (now - parse_timestamp(issue["updated_at"])).days
        print(f"#{issue['number']}: nudged ({days_inactive} days inactive)")
    skipped = len(stale_issues) - MAX_ISSUES_PER_RUN
    if skipped > 0:
        print(f"{skipped} additional stale issue(s) left for the next run")


if __name__ == "__main__":
    try:
        main()
        fire_callback()
    except Exception as error:
        fire_callback("FAILED", str(error))
        raise
