import json
import re
from pathlib import Path

from backlog import (
    ACTIVE,
    BLOCKED,
    PRIORITIES,
    READY,
    REFERENCE,
    TOPIC,
    evidence_root,
    identity,
    namespace,
)
from common import redact, write_json
from github_io import IMPROVEMENT_LABEL, command, github_pages, work_item
from repair_policy import permits
from topic_issue import TopicIssue
from triage_workflow import discovery_repositories

ISSUE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/([1-9][0-9]*)")
PULL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)")
RUN = re.compile(r"(?m)^<!-- automation-improvement-run: [\w-]+ -->\n?")


def normalize_label(name):
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def priority(item, config=None):
    labels_to_hold = {
        "needs-triage",
        "not-ready",
        "not-ready-for-review",
        "not-ready-for-rev",
        "needs-information",
        *[
            normalize_label(name)
            for name in (config or {}).get("issue_hold_labels", [])
        ],
    }
    labels = set(item["labels"])
    values = [index for index, label in enumerate(PRIORITIES) if label in labels]
    if (
        item["state"] == "OPEN"
        and {IMPROVEMENT_LABEL, TOPIC, READY} <= labels
        and not {ACTIVE, BLOCKED} & labels
        and not labels_to_hold.intersection(map(normalize_label, labels))
        and len(values) == 1
    ):
        return values[0]
    return None


def covers(issue, pr, topic_id):
    if pr["kind"] != "pull" or pr["state"] != "OPEN":
        return False
    body = pr["body"]
    url = issue["url"]
    short = "/".join(url.rsplit("/", 4)[1:3]) + "#" + url.rsplit("/", 1)[1]
    return (
        f"<!-- automation-improvement-topic: {topic_id} -->" in body
        or re.search(re.escape(url) + r"(?!\w)", body, re.I)
        or re.search(re.escape(short) + r"(?!\w)", body, re.I)
        or (
            pr["repository"].casefold() == issue["repository"].casefold()
            and re.search(
                r"\b(?:clos(?:e[sd]?|ing)|fix(?:es|ed|ing)?|resolv(?:e[sd]?|ing))\s+#"
                + url.rsplit("/", 1)[1]
                + r"(?!\w)",
                body,
                re.I,
            )
        )
        or pr["url"] in {match[0] for match in PULL.finditer(issue["body"])}
    )


def issue_for(url, state_dir, topic_id, output):
    match = ISSUE.fullmatch(url)
    if not match:
        raise ValueError("Invalid backlog issue URL")
    repository, number = match.groups()
    return TopicIssue(
        repository,
        output,
        evidence_root(state_dir, repository, topic_id),
        {"number": int(number)},
        [],
    )


def selection(config, context, state):
    scope = namespace(config, context["state_dir"])
    repositories = context["repositories"]
    names = set(repositories) | set(discovery_repositories(config, repositories, state))
    candidates, pulls = [], []
    for repository in sorted(names):
        for row in github_pages(
            f"repos/{repository}/issues?state=open&per_page=100", command
        ):
            item = work_item(row, repository)
            if item["kind"] == "pull":
                pulls.append(item)
            elif (identifier := identity(item, scope)) is not None:
                candidates.append((item, identifier))

    ranked, excluded = [], []
    for item, identifier in candidates:
        rank = priority(item, config)
        fallback = {"id": identifier, "priority": rank, "issue_url": item["url"]}
        if rank is None:
            excluded.append({"topic": fallback, "status": "issue_ineligible"})
            continue
        try:
            related = [
                r["url"]
                for attempt in state.get("topic_weeks", {}).values()
                if attempt["topic"]["id"] == identifier
                for r in attempt.get("resources", [])
                if r["kind"] == "pull"
                and permits(context["repair_policy"], r["repository"])
            ]
            explicit = {
                match[0]
                for match in PULL.finditer(item["body"])
                if permits(context["repair_policy"], match[1])
            }
            open_urls = {pr["url"] for pr in pulls}
            unscanned = set(related) | explicit
            for url in sorted(unscanned - open_urls):
                repository, number = PULL.fullmatch(url).groups()
                if repository.casefold() not in {name.casefold() for name in names}:
                    row = json.loads(
                        command(["gh", "api", f"repos/{repository}/pulls/{number}"])
                    )
                    if row["state"] == "open":
                        open_urls.add(url)
            if unscanned & open_urls or any(
                covers(item, pr, identifier) for pr in pulls
            ):
                excluded.append({"topic": fallback, "status": "open_repair_pr"})
                continue
            refs = REFERENCE.findall(item["body"])
            if len(refs) != 1:
                raise ValueError("Backlog issue must have one private topic index")
            issue = issue_for(
                item["url"], context["state_dir"], identifier, Path(context["output"])
            )
            source = json.loads(issue.read_artifact(refs[0]))
            if (
                source["namespace"] != scope
                or source["repository"] != item["repository"].casefold()
                or source["topic"]["id"] != identifier
            ):
                raise ValueError("Private topic index does not belong to this issue")
            topic = source["topic"] | {
                "priority": rank,
                "issue_url": item["url"],
                "input_reference": refs[0],
            }
            ranked.append((rank, item["createdAt"] or "", item["url"], topic))
        except Exception as error:
            excluded.append(
                {
                    "topic": fallback,
                    "status": "backlog_evidence_unavailable",
                    "error": redact(str(error))[:1000],
                }
            )
    ordered, seen = [], set()
    for _, _, _, topic in sorted(ranked):
        if topic["id"] in seen:
            excluded.append({"topic": topic, "status": "duplicate_topic_identity"})
        else:
            ordered.append(topic)
            seen.add(topic["id"])
    return ordered, excluded


def label(issue, method, name):
    endpoint = issue.endpoint + "/labels"
    if method == "DELETE":
        command(["gh", "api", "--method", method, endpoint + "/" + name])
    else:
        command(["gh", "api", "--method", method, endpoint, "-f", "labels[]=" + name])


def claim(config, context, topic, root):
    issue = issue_for(
        topic["issue_url"], context["state_dir"], topic["id"], root / "claim"
    )
    issue.refresh()
    item = work_item(issue.value, issue.repository)
    if (
        identity(item, namespace(config, context["state_dir"])) != topic["id"]
        or priority(item, config) != topic["priority"]
        or REFERENCE.findall(item["body"]) != [topic["input_reference"]]
    ):
        return False
    label(issue, "POST", ACTIVE)
    label(issue, "DELETE", READY)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archived = issue.artifact("pre-repair-brief.json", item["body"])
    write_json(root / "claim" / "archived-brief.json", archived)
    issue.update_body(
        f"<!-- automation-improvement-run: {Path(context['output']).name} -->\n"
        + RUN.sub("", item["body"])
    )
    issue.update_block("backlog-status", "Repair investigation in progress.")
    return True


def finish(context, topic, outcome, root):
    issue = issue_for(
        topic["issue_url"], context["state_dir"], topic["id"], root / "release"
    )
    issue.refresh()
    status = {
        "reviewed": "Independent review completed.",
        "unrepaired": "Investigation completed without a repair.",
        "failed": "The repair attempt failed.",
        "review_incomplete": "The repair or independent review did not complete.",
    }.get(outcome["status"], "The repair run ended.")
    decision = (outcome.get("review_decision") or {}).get("decision")
    if decision == "accepted" and outcome["status"] == "reviewed":
        status += " See linked PRs for the verified scope and evidence."
    elif decision:
        status += f" Review decision: `{decision}`."
    issue.update_block(
        "backlog-status",
        status
        + " To request another attempt, reconcile any open repair PRs and add `improvement-ready`.",
    )
    labels = {row["name"] for row in issue.value.get("labels", [])}
    if READY in labels:
        label(issue, "DELETE", READY)
    if ACTIVE in labels:
        label(issue, "DELETE", ACTIVE)
