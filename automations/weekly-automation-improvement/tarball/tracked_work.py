import hashlib
import json
import re

from backlog import IDENTITY, RECURRENT, TOPIC, ensure_labels, event_markers, identity
from common import encode, redact, replace_json
from epic import is_index
from finding_record import occurrence_id
from github_io import IMPROVEMENT_LABEL, command, github_pages, repository_private
from repair_policy import permits_work

WORK_URL = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/([1-9][0-9]*)"
    r"(?:#issuecomment-([1-9][0-9]*))?"
)
LEGACY_EVENT = re.compile(
    r"(?m)^<!-- automation-improvement-event: "
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}) -->$",
    re.IGNORECASE,
)


def safe_work_link(destination_repo, url, visibility, *, request=None):
    match = WORK_URL.fullmatch(url)
    if not match:
        return False
    repository = match[1]
    key = repository.casefold()
    if key == destination_repo.casefold():
        return True
    if key not in visibility:
        try:
            visibility[key] = repository_private(repository, request or command)
        except Exception:
            visibility[key] = None
    return visibility[key] is False


def matching(topic, existing):
    selected = set(topic.get("existing_work_urls", []))
    identities = {topic["id"]}
    findings = (
        event_markers(topic).splitlines()
        if topic.get("representative_event_ids")
        else []
    )
    occurrence_ids = {
        value.casefold() for value in topic.get("representative_event_ids", [])
    }
    while True:
        before = (len(selected), len(identities))
        for item in existing:
            markers = set(
                re.findall(
                    r"<!-- automation-improvement-topic: ([a-f0-9]{20}) -->",
                    item.get("body", ""),
                )
            )
            if (
                item["url"] in selected
                or markers.intersection(identities)
                or any(marker in item["body"] for marker in findings)
                or occurrence_ids.intersection(
                    value.casefold() for value in LEGACY_EVENT.findall(item["body"])
                )
            ):
                selected.add(item["url"])
                identities.update(markers)
        if before == (len(selected), len(identities)):
            return sorted(selected)


def comment(topic, existing, records, issue, output, policy=None, scope=None):
    targets = matching(topic, existing)
    allowed = {item["url"]: item for item in existing}
    matches = {target: WORK_URL.fullmatch(target) for target in targets}
    if not targets or any(
        target not in allowed or not match or match[4]
        for target, match in matches.items()
    ):
        raise ValueError("Recurrence must reference discovered issues or PRs")
    if policy and any(
        not permits_work(policy, match[1], match[2]) for match in matches.values()
    ):
        raise ValueError("Recurrence target outside customer_stack repair scope")
    events = sorted(set(topic["representative_event_ids"]))
    by_id = {occurrence_id(row): row for row in records}
    selected = [by_id[identifier] for identifier in events]
    brief = issue.value["html_url"]
    brief_match = WORK_URL.fullmatch(brief) if brief else None
    if brief and (not brief_match or brief_match[2] != "issues" or brief_match[4]):
        raise ValueError("Invalid weekly evidence issue URL")
    artifact = issue.artifact(
        "recurrence-evidence.json",
        {
            "topic": topic,
            "records": selected,
            "related_work": targets,
            "triage_issue": brief,
        },
    )
    visibility, receipts = {}, []
    label_repositories = set()
    for target in targets:
        receipt = {"target": target, "url": None, "preview": issue.preview}
        receipts.append(receipt)
        if issue.preview:
            continue
        match = matches[target]
        destination = match[1]
        links = [
            url
            for url in targets
            if url != target and safe_work_link(destination, url, visibility)
        ]
        safe_brief = (
            brief if brief and safe_work_link(destination, brief, visibility) else None
        )
        scoped_reference = (
            f" Private evidence reference: `{artifact['reference']}` "
            "(read with `read_issue_artifact` from the weekly summary)."
            if brief_match and destination.casefold() == brief_match[1].casefold()
            else ""
        )
        body = (
            "## Automation improvement recurrence\n\n"
            f"Weekly discovery matched {len(events)} finding(s) to this work. "
            "These are investigation leads. This update did not start a repair."
            + scoped_reference
            + (f"\n\n[Weekly discovery summary]({safe_brief})." if safe_brief else "")
            + ("\n\nRelated work: " + ", ".join(links) if links else "")
        )
        if redact(body) != body or len(body) > 60000:
            raise ValueError("Unsafe or oversized recurrence comment")
        marker = (
            "<!-- automation-improvement-recurrence: "
            + hashlib.sha256(encode([target, events]).encode()).hexdigest()
            + " -->"
        )
        endpoint = f"repos/{destination}/issues/{match[3]}"
        try:
            current = json.loads(command(["gh", "api", endpoint]))
            if current["html_url"].casefold() != target.casefold():
                raise ValueError("Recurrence destination changed since discovery")
            body_now = current.get("body") or ""
            if (
                is_index(body_now)
                or scope
                and IDENTITY.search(body_now)
                and not identity(
                    {"kind": allowed[target]["kind"], "body": body_now}, scope
                )
            ):
                raise ValueError(
                    "Recurrence destination is outside this discovery scope"
                )
            comments_endpoint = endpoint + "/comments"
            comments = github_pages(comments_endpoint + "?per_page=100", command)
            found = next(
                (c for c in comments if c.get("body", "").startswith(marker)), None
            )
            if found is None:
                path = output / (hashlib.sha256(marker.encode()).hexdigest() + ".json")
                replace_json(path, {"body": marker + "\n\n" + body})
                found = json.loads(
                    command(
                        [
                            "gh",
                            "api",
                            "--method",
                            "POST",
                            comments_endpoint,
                            "--input",
                            str(path),
                        ]
                    )
                )
                found = json.loads(
                    command(
                        [
                            "gh",
                            "api",
                            f"repos/{destination}/issues/comments/{found['id']}",
                        ]
                    )
                )
            link = WORK_URL.fullmatch(found["html_url"])
            if (
                not link
                or link[1].casefold() != destination.casefold()
                or link[3] != match[3]
                or link[4] != str(found["id"])
                or not found["body"].startswith(marker)
            ):
                raise ValueError("GitHub recurrence comment could not be verified")
            receipt["url"] = found["html_url"]
            if scope and identity(allowed[target], scope):
                labels = {label["name"] for label in current["labels"]}
                if (
                    identity({"kind": "issue", "body": current["body"]}, scope) is None
                    or not {TOPIC, IMPROVEMENT_LABEL} <= labels
                    or RECURRENT in labels
                ):
                    continue
                if destination not in label_repositories:
                    ensure_labels(destination)
                    label_repositories.add(destination)
                path = output / f"recurrence-label-{found['id']}.json"
                replace_json(path, {"labels": [RECURRENT]})
                labels = json.loads(
                    command(
                        [
                            "gh",
                            "api",
                            "--method",
                            "POST",
                            endpoint + "/labels",
                            "--input",
                            str(path),
                        ]
                    )
                )
                if RECURRENT not in {label["name"] for label in labels}:
                    raise ValueError("GitHub recurrence label could not be verified")
        except Exception as error:
            receipt["error"] = redact(str(error))[:1000]
    return receipts
