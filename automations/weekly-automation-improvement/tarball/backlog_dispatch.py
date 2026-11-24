import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from backlog import (
    ACTIVE,
    IDENTITY,
    CUSTOMER,
    EXTERNAL,
    FACTORY,
    OUTCOME_LABELS,
    PRIORITIES,
    READY,
    REFERENCE,
    TOPIC,
    ensure_labels,
    evidence_root,
    identity,
    namespace,
    on_hold,
)
from common import read_json, redact, replace_json, write_json
from github_io import IMPROVEMENT_LABEL, command, github_pages, work_item
from repair_policy import permits
from topic_issue import TopicIssue
from triage_workflow import discovery_repositories

ISSUE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/([1-9][0-9]*)")
PULL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)")
RUN = re.compile(r"(?m)^<!-- automation-improvement-run: ([\w-]+) -->\n?")


def external_owner(config):
    return (
        config.get("finding_source") == "portable"
        or config.get("execution_owner") == "external"
    )


def recorded_external_owner(state_dir, url, factory):
    repository, number = ISSUE.fullmatch(url).groups()
    repo = repository.casefold()
    return (
        f"repos/{repo}/issues/{number}" in factory
        or (Path(state_dir) / "customer-attempts" / repo / (number + ".json")).is_file()
    )


def factory_claims(state_dir):
    ledger = Path(state_dir) / "factory-handoff.json"
    return {key.casefold() for key in read_json(ledger)} if ledger.exists() else set()


def priority(item, config=None):
    labels = set(item["labels"])
    values = [index for index, label in enumerate(PRIORITIES) if label in labels]
    if (
        item["state"] == "OPEN"
        and not item.get("assignees")
        and {IMPROVEMENT_LABEL, TOPIC, READY} <= labels
        and not {ACTIVE, CUSTOMER, EXTERNAL, FACTORY} & labels
        and not on_hold(config or {}, labels)
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


def competing_pr(context, state, item, identifier, pulls, names):
    related = [
        r["url"]
        for attempt in state.get("topic_weeks", {}).values()
        if attempt["topic"]["id"] == identifier
        for r in attempt.get("resources", [])
        if r["kind"] == "pull" and permits(context["repair_policy"], r["repository"])
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
    return bool(unscanned & open_urls) or any(
        covers(item, pr, identifier) for pr in pulls
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
    identities = Counter(identifier for _, identifier in candidates)
    factory = factory_claims(context["state_dir"])
    for item, identifier in candidates:
        rank = priority(item, config)
        fallback = {"id": identifier, "priority": rank, "issue_url": item["url"]}
        if (
            {CUSTOMER, EXTERNAL, FACTORY}.intersection(item["labels"])
            or external_owner(config)
            or recorded_external_owner(context["state_dir"], item["url"], factory)
        ):
            excluded.append({"topic": fallback, "status": "external_owner"})
            continue
        if identities[identifier] > 1:
            excluded.append({"topic": fallback, "status": "duplicate_topic_identity"})
            continue
        if rank is None:
            excluded.append({"topic": fallback, "status": "issue_ineligible"})
            continue
        try:
            if competing_pr(context, state, item, identifier, pulls, names):
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
    return [topic for _, _, _, topic in sorted(ranked)], excluded


def label(issue, method, name):
    endpoint = issue.endpoint + "/labels"
    if method == "DELETE":
        command(
            ["gh", "api", "--method", method, endpoint + "/" + quote(name, safe="")]
        )
    else:
        command(["gh", "api", "--method", method, endpoint, "-f", "labels[]=" + name])


def claim_path(context, topic):
    return Path(context["state_dir"]) / "native-claims" / (topic["id"] + ".json")


def claim(config, context, topic, root, state):
    journal = claim_path(context, topic)
    if (
        journal.exists()
        or external_owner(config)
        or recorded_external_owner(
            context["state_dir"],
            topic["issue_url"],
            factory_claims(context["state_dir"]),
        )
    ):
        return False
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
    names = discovery_repositories(config, context["repositories"], state)
    names = set(context["repositories"]) | set(names)
    pulls = [
        work_item(row | {"pull_request": True}, name)
        for name in sorted(names)
        for row in github_pages(f"repos/{name}/pulls?state=open&per_page=100", command)
    ]
    if competing_pr(context, state, item, topic["id"], pulls, names):
        return False
    replace_json(journal, {"topic": topic, "output": context["output"]})
    archived = issue.artifact("pre-repair-brief.json", item["body"])
    write_json(root / "claim" / "archived-brief.json", archived)
    label(issue, "POST", ACTIVE)
    issue.refresh()
    current = work_item(issue.value, issue.repository)
    if ACTIVE not in current["labels"]:
        raise ValueError("GitHub did not confirm the native claim")
    current["labels"] = [name for name in current["labels"] if name != ACTIVE]
    if (
        identity(current, namespace(config, context["state_dir"])) != topic["id"]
        or priority(current, config) != topic["priority"]
        or REFERENCE.findall(current["body"]) != [topic["input_reference"]]
    ):
        label(issue, "DELETE", ACTIVE)
        journal.unlink()
        return False
    label(issue, "DELETE", READY)
    for outcome_label in sorted(
        set(current["labels"]).intersection(OUTCOME_LABELS.values())
    ):
        label(issue, "DELETE", outcome_label)
    issue.refresh()
    run_marker = f"<!-- automation-improvement-run: {Path(context['output']).name} -->"
    body = RUN.sub("", issue.value["body"])
    issue.update_body(
        IDENTITY.sub(lambda match: match[0] + "\n" + run_marker, body, count=1)
    )
    issue.update_block(
        "backlog-status",
        "**Owner:** Native repair workflow.\n\nRepair investigation in progress.\n\n"
        "**Next action:** Native repair and independent review are running.",
        verify=True,
    )
    validate_claim(
        config, context["state_dir"], topic, Path(context["output"]).name, issue
    )
    return True


def validate_claim(config, state_dir, topic, run_id, issue):
    issue.refresh()
    item = work_item(issue.value, issue.repository)
    labels = set(item["labels"])
    item["labels"] = list((labels - {ACTIVE}) | {READY})
    if (
        ACTIVE not in labels
        or identity(item, namespace(config, state_dir)) != topic["id"]
        or RUN.findall(item["body"]) != [run_id]
        or priority(item, config) is None
    ):
        raise ValueError("The selected issue no longer has an eligible native claim")


def finish(context, topic, outcome, root):
    issue = issue_for(
        topic["issue_url"], context["state_dir"], topic["id"], root / "release"
    )
    issue.refresh()
    labels = {row["name"] for row in issue.value.get("labels", [])}
    journal = claim_path(context, topic)
    if ACTIVE not in labels:
        journal.unlink(missing_ok=True)
        return
    if not {CUSTOMER, EXTERNAL, FACTORY} & labels:
        publish_outcome(issue, outcome, labels)
        if READY in labels:
            label(issue, "DELETE", READY)
    if not outcome.get("resource_recovery_pending"):
        label(issue, "DELETE", ACTIVE)
        journal.unlink(missing_ok=True)


def publish_outcome(issue, outcome, labels):
    decision = (outcome.get("review_decision") or {}).get("decision")
    result = {
        "accepted": "accepted",
        "blocked": "blocked",
        "revise": "revision-needed",
    }.get(decision, "incomplete")
    result = {
        "unrepaired": "unrepaired",
        "failed": "failed",
        "review_incomplete": "incomplete",
    }.get(outcome["status"], result)
    status, next_action = {
        "accepted": (
            "Independent review accepted the scoped repair.",
            "A maintainer can review the linked changes and decide whether to merge.",
        ),
        "blocked": (
            "Independent review found an external prerequisite.",
            "A maintainer should resolve the prerequisite before requesting another attempt.",
        ),
        "revision-needed": (
            "Independent review requested changes.",
            "A maintainer should reconcile the draft PRs before requesting another attempt.",
        ),
        "unrepaired": (
            "Investigation completed without a repair.",
            "A maintainer should decide whether to pursue another investigation.",
        ),
        "incomplete": (
            "The repair, review or publication checks did not complete.",
            "A maintainer should reconcile the linked work and private attempt receipt before retrying.",
        ),
        "failed": (
            "The repair attempt failed.",
            "A maintainer should inspect the private attempt receipt and reconcile open work before retrying.",
        ),
    }[result]
    revisions = []
    pulls = [r for r in outcome.get("resources", []) if r["kind"] == "pull"]
    if result == "accepted" and pulls and all(pr.get("merged") for pr in pulls):
        next_action = "The reviewed changes have merged. A maintainer can close the topic after confirming the result."
    for pr in pulls:
        if (
            (
                not pr.get("private", True)
                or pr["repository"].casefold() == issue.repository.casefold()
            )
            and pr.get("base")
            and pr.get("head")
        ):
            state = "draft" if pr.get("draft") else "open for review on GitHub"
            if pr.get("state") == "closed":
                state = "merged" if pr.get("merged") else "closed without merging"
            details = (
                f"- {pr['url']} — {state}, base `{pr['base']}`, head `{pr['head']}`"
            )
            if (
                pr.get("state") == "open"
                and pr.get("current_head")
                and pr.get("current_base")
            ):
                details += f". Current base `{pr['current_base']}`, head `{pr['current_head']}` require review."
            revisions.append(details)
    desired = OUTCOME_LABELS[result]
    ensure_labels(issue.repository)
    issue.update_block(
        "backlog-status",
        "\n\n".join(
            [
                "**Owner:** Native repair workflow.",
                status,
                "**Next action:** " + next_action,
            ]
            + (["\n".join(revisions)] if revisions else [])
        ),
        verify=True,
    )
    if desired not in labels:
        label(issue, "POST", desired)
    for old in sorted(labels.intersection(OUTCOME_LABELS.values()) - {desired}):
        label(issue, "DELETE", old)
