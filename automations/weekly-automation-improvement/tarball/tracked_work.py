import hashlib
import json
import re
import uuid

from common import encode, redact, write_json
from github_io import command, github_pages
from repair_policy import permits


def matching(topic, existing):
    selected = set(topic.get("existing_work_urls", []))
    identities = {topic["id"]}
    while True:
        before = (len(selected), len(identities))
        for item in existing:
            markers = set(
                re.findall(
                    r"<!-- automation-improvement-topic: ([a-f0-9]{20}) -->",
                    item.get("body", ""),
                )
            )
            if item["url"] in selected or markers.intersection(identities):
                selected.add(item["url"])
                identities.update(markers)
        if before == (len(selected), len(identities)):
            return sorted(selected)


def comment(topic, existing, records, issue, output, policy=None):
    targets = matching(topic, existing)
    allowed = {item["url"] for item in existing}
    if not targets or not set(targets) <= allowed:
        raise ValueError("Recurrence must reference discovered issues or PRs")
    if policy and any(
        not permits(policy, "/".join(target.split("/")[3:5])) for target in targets
    ):
        raise ValueError("Recurrence target outside customer_stack repair scope")
    events = sorted(set(topic["representative_event_ids"]))
    by_id = {row["event"]["id"]: row for row in records}
    selected = [by_id[identifier] for identifier in events]
    evidence = []
    for row in selected:
        event = row["event"]
        identifier, trace = (
            str(uuid.UUID(event["id"])),
            str(uuid.UUID(event["trace_id"])),
        )
        evidence.append(f"- Signal event `{identifier}`; trace `{trace}`.")
    artifact = issue.artifact(
        "recurrence-evidence.json", {"topic": topic, "records": selected}
    )
    brief = issue.value["html_url"]
    if brief is not None and not re.fullmatch(
        r"https://github.com/[\w.-]+/[\w.-]+/issues/[1-9][0-9]*", brief
    ):
        raise ValueError("Invalid weekly evidence issue URL")
    body = (
        "Weekly triage identified new findings matching the problem tracked here. "
        "The problem may still be occurring; these findings are investigation leads, not proof of a regression or of an ineffective fix. "
        "No new repair agent was dispatched for this topic.\n\n"
        + "\n".join(evidence)
        + f"\n\n[Current triage and private-data access instructions]({brief or '#preview'}). "
        + f"Private evidence reference: `{artifact['reference']}` (read with `read_issue_artifact`). "
        + "Raw inputs, original Signals and logs remain private.\n\nRelated work: "
        + ", ".join(targets)
    )
    if redact(body) != body or len(body) > 60000:
        raise ValueError("Unsafe or oversized recurrence comment")
    receipts = []
    for target in targets:
        match = re.fullmatch(
            r"https://github.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/([1-9][0-9]*)",
            target,
        )
        if match is None:
            raise ValueError("Invalid tracked work URL")
        marker = (
            "<!-- automation-improvement-recurrence: "
            + hashlib.sha256(encode([target, events]).encode()).hexdigest()
            + " -->"
        )
        receipt = {"target": target, "url": None, "preview": issue.preview}
        if not issue.preview:
            endpoint = f"repos/{match[1]}/issues/{match[2]}/comments"
            comments = github_pages(endpoint + "?per_page=100", command)
            found = next((c for c in comments if marker in c.get("body", "")), None)
            if found is None:
                path = output / (hashlib.sha256(marker.encode()).hexdigest() + ".json")
                if not path.exists():
                    write_json(path, {"body": marker + "\n\n" + body})
                found = json.loads(
                    command(
                        [
                            "gh",
                            "api",
                            "--method",
                            "POST",
                            endpoint,
                            "--input",
                            str(path),
                        ]
                    )
                )
            receipt["url"] = found["html_url"]
        receipts.append(receipt)
    return receipts
