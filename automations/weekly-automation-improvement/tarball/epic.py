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
    for event in state.get("events", {}).values():
        known.update(event.get("existing_work_urls", []))
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
        rows = github_pages(f"repos/{repo}/issues?state=open&per_page=100", command)
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
    try:
        scope = namespace(config, state_dir)
        issues, unverified = open_issues(config, scope, state)
        items = "\n".join(line for _, line in sorted(map(issue_line, issues)))
        items = items or "There are no open improvement issues."
        latest = ISSUE_URL.fullmatch((weekly or {}).get("url") or "")
        if latest and latest[1].casefold() != home.casefold():
            latest = None
        summary = f"[Latest triage summary]({latest[0]})" if latest else ""
        body = (
            f"{EPIC} {scope} -->\n\n"
            "Review open automation improvement work here. Issue labels and conversations "
            "show the current status and next steps.\n\n"
            + block("epic-latest", summary)
            + "\n\n## Open issues\n\n"
            + block("epic-issues", items)
        )
        evidence_dir = state_dir / "issue-evidence" / home / "epic" / scope
        title = "Automation improvement epic"
        if config.get("isolated_validation"):
            title += " (validation)"
        epic = TopicIssue.ensure_brief(home, output, evidence_dir, body, title)
        url = epic.value["html_url"]
        if epic.value["state"] != "open":
            return {"status": "closed", "url": url}
        epic.update_block("epic-issues", items)
        if latest:
            epic.update_block("epic-latest", summary)
            if "<!-- triage-epic:start -->" in (weekly.get("body") or ""):
                weekly_issue = TopicIssue(
                    home,
                    output / "summary",
                    evidence_dir,
                    {"number": int(latest[2])},
                    [],
                )
                weekly_issue.update_block(
                    "triage-epic", f"[Open improvement issues]({url})"
                )
        result = {"status": "refreshed", "url": url, "open_issues": len(issues)}
        if unverified:
            result.update(
                status="partially_refreshed", unverified_repositories=unverified
            )
        return result
    except Exception as error:
        return {"status": "refresh_failed", "error": redact(str(error))[:1000]}
