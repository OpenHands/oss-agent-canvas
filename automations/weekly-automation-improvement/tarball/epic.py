import re

from backlog import (
    IDENTITY,
    PRIORITIES,
    TOPIC,
    block,
    identity,
    namespace,
)
from common import redact
from github_io import command, github_pages, repository_private, work_item
from repair_policy import permits, resolve
from topic_issue import TopicIssue

EPIC = "<!-- automation-improvement-epic:"
WEEKLY = "<!-- automation-improvement-triage:"
ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/([1-9][0-9]*)")


def is_index(body):
    return body.lstrip().startswith((EPIC, WEEKLY))


def open_issues(config, scope, state):
    home = config["repository"]
    policy = resolve(config)
    known = set()
    legacy = not (
        config.get("isolated_validation") or config.get("finding_source") == "portable"
    )
    for event in state.get("events", {}).values():
        if event.get("discovery_scope") == scope or (
            legacy and "discovery_scope" not in event
        ):
            known.update(event.get("existing_work_urls", []))
    if legacy:
        for attempt in state.get("topic_weeks", {}).values():
            known.add(attempt["topic"].get("issue_url") or "")
            known.update(resource["url"] for resource in attempt.get("resources", []))
    matches = [match for url in known if (match := ISSUE_URL.fullmatch(url))]
    urls = {match[0].casefold() for match in matches}
    repositories = {
        repo.casefold(): repo
        for repo in (*policy["repositories"], *(m[1] for m in matches))
        if permits(policy, repo)
    }
    issues, unverified = {}, []
    for key, repo in sorted(repositories.items()):
        if key != home.casefold():
            try:
                if repository_private(repo, command):
                    continue
            except Exception:
                unverified.append(repo)
                continue
        try:
            rows = github_pages(f"repos/{repo}/issues?state=open&per_page=100", command)
        except Exception:
            if key == home.casefold():
                raise
            unverified.append(repo)
            continue
        for row in rows:
            item = work_item(row, repo)
            match = ISSUE_URL.fullmatch(item["url"])
            if (
                not match
                or match[1].casefold() != key
                or item["kind"] != "issue"
                or item["state"] != "OPEN"
                or is_index(item["body"])
            ):
                continue
            scoped = identity(item, scope)
            backlog_topic = TOPIC in item["labels"] and scoped
            related = item["url"].casefold() in urls and (
                scoped or not IDENTITY.search(item["body"])
            )
            if backlog_topic or related:
                issues[item["url"].casefold()] = item
    return list(issues.values()), unverified


def issue_line(item):
    _, number = ISSUE_URL.fullmatch(item["url"]).groups()
    title = redact(" ".join(item["title"].split()))
    title = re.sub(r"https?://\S+|[\w.-]+/[\w./-]+", "[reference]", title)
    title = re.sub(r"([\\`*_\[\]<>])", r"\\\1", title).replace("@", "&#64;")
    labels = set(item["labels"])
    priorities = [i for i, label in enumerate(PRIORITIES) if label in labels]
    rank = priorities[0] if len(priorities) == 1 else len(PRIORITIES)
    suffix = f" — P{rank}" if rank < len(PRIORITIES) else ""
    return rank, f"- [{item['repository']}#{number}]({item['url']}) {title}{suffix}"


def refresh(config, state_dir, state, output, weekly=None):
    home = config["repository"]
    weekly_issue = None
    url = None
    try:
        scope = namespace(config, state_dir)
        evidence_dir = state_dir / "issue-evidence" / home / "epic" / scope
        weekly = weekly or state.get("discovery_indices", {}).get(scope) or {}
        latest = ISSUE_URL.fullmatch(weekly.get("url") or "")
        if latest:
            if latest[1].casefold() != home.casefold():
                raise ValueError(
                    "Weekly discovery issue is outside the home repository"
                )
            weekly_issue = TopicIssue(
                home, output / "summary", evidence_dir, {"number": int(latest[2])}, []
            )
            weekly_issue.refresh()
            if weekly_issue.value["html_url"].casefold() != latest[
                0
            ].casefold() or not weekly_issue.value["body"].lstrip().startswith(WEEKLY):
                weekly_issue = None
                raise ValueError(
                    "Weekly discovery issue identity could not be verified"
                )
        issues, unverified = open_issues(config, scope, state)
        items = "\n".join(line for _, line in sorted(map(issue_line, issues)))
        items = items or "There are no open improvement issues."
        if unverified:
            items = (
                "Some related repositories could not be checked. This list may be incomplete.\n\n"
                + items
            )
        summary = f"[Latest triage summary]({latest[0]})" if latest else ""
        body = (
            f"{EPIC} {scope} -->\n\n"
            "Review open automation improvement work here. Issue labels and conversations "
            "show the current status and next steps.\n\n"
            + block("epic-latest", summary)
            + "\n\n## Open issues\n\n"
            + block("epic-issues", items)
        )
        title = "Automation improvement epic"
        if config.get("isolated_validation"):
            title += " (validation)"
        epic = TopicIssue.ensure_brief(
            home, evidence_dir / "publication", evidence_dir, body, title
        )
        url = epic.value["html_url"]
        if epic.value["state"] != "open":
            if weekly_issue:
                weekly_issue.update_block(
                    "triage-epic",
                    f"[The improvement index is closed]({url}).",
                    verify=True,
                )
            return {"status": "closed", "url": url}
        epic.update_block("epic-issues", items, verify=True)
        if weekly_issue:
            epic.update_block("epic-latest", summary, verify=True)
            message = f"[Open improvement issues]({url})"
            if unverified:
                message += "\n\nThe improvement index may be incomplete."
            weekly_issue.update_block("triage-epic", message, verify=True)
        result = {"status": "refreshed", "url": url, "open_issues": len(issues)}
        if unverified:
            result.update(
                status="partially_refreshed", unverified_repositories=unverified
            )
        return result
    except Exception as error:
        result = {"status": "refresh_failed", "error": redact(str(error))[:1000]}
        if url:
            result["url"] = url
        if weekly_issue:
            try:
                message = "The improvement index refresh could not be verified."
                if url:
                    message += f" [Open the last available index]({url})."
                weekly_issue.update_block("triage-epic", message, verify=True)
            except Exception as backlink_error:
                result["backlink_error"] = redact(str(backlink_error))[:1000]
        return result
