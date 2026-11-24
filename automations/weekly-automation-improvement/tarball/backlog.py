import hashlib
import json
import re
from pathlib import Path

from common import read_json, redact
from github_io import (
    IMPROVEMENT_LABEL,
    command,
    github_pages,
    repository_private,
    work_item,
)
from repair_policy import permits, resolve
from topic_issue import SECTIONS, TopicIssue, public_issue_errors

TOPIC = "improvement-topic"
READY = "improvement-ready"
ACTIVE = "improvement-in-progress"
BLOCKED = "improvement-blocked"
EXTERNAL = "improvement-external"
CUSTOMER = "improvement-customer"
FACTORY = "improvement-factory"
RECURRENT = "improvement-recurrent"
EXTERNAL_STATUS = (
    "Check the issue labels for current eligibility. The external dispatcher's status comment records the latest handoff, any blocker, and the next actor. "
    "If no status comment exists, a maintainer can check whether this issue is enrolled for dispatch."
)
OUTCOME_LABELS = {
    outcome: f"improvement-outcome-{outcome}"
    for outcome in (
        "accepted",
        "blocked",
        "unrepaired",
        "revision-needed",
        "revised",
        "incomplete",
        "failed",
    )
}
PRIORITIES = tuple(f"priority:P{i}" for i in range(4))
LABELS = {
    TOPIC: "6056B3",
    READY: "0E8A16",
    ACTIVE: "1D76DB",
    BLOCKED: "B60205",
    EXTERNAL: "5319E7",
    CUSTOMER: "5319E7",
    FACTORY: "5319E7",
    RECURRENT: "006B75",
    **dict.fromkeys(OUTCOME_LABELS.values(), "CFD3D7"),
    **dict.fromkeys(PRIORITIES, "FBCA04"),
}
IDENTITY = re.compile(
    r"(?m)^<!-- automation-improvement-backlog: ([a-f0-9]{20})/([a-f0-9]{20}) -->$"
)
REFERENCE = re.compile(r"<!-- automation-improvement-input: ([a-f0-9]{64}\.json) -->")
BRIEF_FIELDS = ("problem", "example", "prevention", "validation")
PRIVATE_TEXT = re.compile(
    r"[<>\\`]|https?://|[\\/]|(?m:^\s*#)"
    r"|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|[a-f0-9]{32,}",
    re.I,
)


def on_hold(config, labels):
    def normalize(name):
        return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")

    holds = {
        BLOCKED,
        "needs-triage",
        "not-ready",
        "not-ready-for-review",
        "not-ready-for-rev",
        "needs-information",
    }
    holds.update(normalize(name) for name in config.get("issue_hold_labels", []))
    return any(normalize(label) in holds for label in labels)


def native_claimed(state_dir, issue_url):
    root = Path(state_dir)
    url = issue_url.casefold()
    for journal in (root / "native-claims").glob("*.json"):
        if read_json(journal)["topic"]["issue_url"].casefold() == url:
            return True
    ledger = root / "state.json"
    if not ledger.exists():
        return False
    return any(
        attempt.get("status") == "dispatched"
        and (attempt["topic"].get("issue_url") or "").casefold() == url
        for attempt in read_json(ledger, 32_000_000).get("topic_weeks", {}).values()
    )


def namespace(config, state_dir):
    values = [config["repository"].casefold()]
    if config.get("finding_source") == "portable":
        from finding_record import SCHEMA, portable_namespace

        values.extend([SCHEMA, portable_namespace(config)])
    else:
        values.extend([config["project_id"], config["signal_id"]])
    if config.get("isolated_validation"):
        values.append(str(state_dir))
    return hashlib.sha256("/".join(values).encode()).hexdigest()[:20]


def identity(item, scope):
    if item["kind"] != "issue":
        return None
    matches = IDENTITY.findall(item["body"])
    if len(matches) == 1 and matches[0][0] == scope:
        return matches[0][1]
    return None


def evidence_root(state_dir, repository, topic_id):
    return Path(state_dir) / "issue-evidence" / repository.casefold() / topic_id


def issue_guides(source):
    if not source or not source.get("revision"):
        return []
    files = command(
        ["git", "ls-tree", "-rz", "--name-only", source["revision"]],
        source["checkout"],
        strip=False,
    )
    guides = {"agents.md", "contributing.md", "readme.md", "issue_template.md"}
    return [
        path
        for path in files.split("\0")
        if path.casefold().startswith(
            (".github/issue_template/", "docs/issue_template/")
        )
        or path.casefold().removeprefix(".github/").removeprefix("docs/") in guides
    ]


def ensure_labels(repository):
    existing = {
        row["name"].casefold(): row["name"]
        for row in github_pages(f"repos/{repository}/labels?per_page=100", command)
    }
    for name, color in {IMPROVEMENT_LABEL: "5319E7", **LABELS}.items():
        if name.casefold() not in existing:
            command(
                ["gh", "label", "create", name, "--repo", repository, "--color", color]
            )
            existing[name.casefold()] = name
    return existing


def safe_brief(topic):
    brief = topic["brief"]
    sections = brief.get("submission", [])
    labels = brief.get("labels", [])
    if (
        not isinstance(sections, list)
        or len(sections) > 10
        or any(
            not isinstance(section, dict)
            or set(section) != {"heading", "text"}
            or not isinstance(section["heading"], str)
            or len(section["heading"]) > 100
            or "\n" in section["heading"]
            for section in sections
        )
        or not isinstance(labels, list)
        or len(labels) > 8
        or any(
            not isinstance(name, str) or not name.strip() or len(name) > 50
            for name in labels
        )
    ):
        raise ValueError("Invalid repository issue submission details")
    for value in [
        topic["title"],
        *(brief[field] for field in BRIEF_FIELDS),
        *(section[field] for section in sections for field in ("heading", "text")),
    ]:
        if (
            not isinstance(value, str)
            or not 1 <= len(value.strip()) <= 2000
            or redact(value) != value
            or PRIVATE_TEXT.search(value)
        ):
            raise ValueError(
                "Topic needs plain-language public text without private references"
            )
    if "\n" in topic["title"]:
        raise ValueError("Topic title must be a single line")
    return brief


def event_markers(topic):
    return "".join(
        "<!-- automation-improvement-finding: "
        + hashlib.sha256(value.encode()).hexdigest()
        + " -->\n"
        for value in sorted(set(topic["representative_event_ids"]))
    )


def priority_reason(topic):
    reason = " ".join(topic.get("reason", "").split())
    if not reason or redact(reason) != reason or PRIVATE_TEXT.search(reason):
        return "A public rationale was unavailable."
    return re.sub(r"([*_\[\]#])", r"\\\1", reason[:480]).replace("@", "&#64;")


def block(name, text):
    return f"<!-- {name}:start -->\n{text}\n<!-- {name}:end -->"


def input_text(reference, topic=None):
    markers = event_markers(topic) if topic else ""
    return (
        markers + f"<!-- automation-improvement-input: {reference} -->\n"
        f"Authorized workers can read the private topic index `{reference}` with "
        "`read_issue_artifact`. Original findings and preparation records are retained privately."
    )


def weekly_summary_link(home, destination, weekly):
    url = (weekly or {}).get("url") or ""
    if not re.fullmatch(
        r"https://github\.com/" + re.escape(home) + r"/issues/[1-9][0-9]*", url, re.I
    ):
        return ""
    if home.casefold() != destination.casefold():
        try:
            if repository_private(home, command):
                return ""
        except Exception:
            return ""
    return f"\n\n[Weekly discovery summary]({url})"


def publish(config, context, topic, existing, output, *, preview=False):
    scope = namespace(config, context["state_dir"])
    repository = existing["repository"] if existing else config["repository"]
    if not permits(resolve(config), repository):
        raise ValueError("Backlog destination is outside the configured repair scope")
    if existing and not re.fullmatch(
        r"https://github\.com/" + re.escape(repository) + r"/issues/[1-9][0-9]*",
        existing["url"],
        re.IGNORECASE,
    ):
        raise ValueError("Backlog issue URL differs from its repository")
    topic_id = identity(existing, scope) if existing else topic["id"]
    if not topic_id:
        raise ValueError("Backlog issue is outside the discovery scope")
    root = evidence_root(context["state_dir"], repository, topic_id)
    if preview:
        safe_brief(topic)
        return existing["url"] if existing else None
    prior_evidence = root.is_dir() and any(root.glob("*.json"))

    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    io = TopicIssue(repository, output, root, {"number": 0}, [], preview)
    if existing:
        number = int(existing["url"].rsplit("/", 1)[1])
        io = TopicIssue(repository, output, root, {"number": number}, [], preview)
        io.refresh()
        if (
            io.value["html_url"].casefold() != existing["url"].casefold()
            or identity({"kind": "issue", "body": io.value["body"]}, scope) != topic_id
        ):
            raise ValueError("Backlog issue identity changed during triage")
    old_refs = REFERENCE.findall(io.value.get("body", ""))
    if existing and len(old_refs) != 1:
        raise ValueError(
            "Backlog issue has a missing or ambiguous private evidence reference"
        )
    topic = topic | {"id": topic_id}
    data = {
        "topic": topic,
        "namespace": scope,
        "repository": repository.casefold(),
        "inventory_path": context["inventory_path"],
        "records_path": context["records_path"],
        "source_revision": context.get("source_revision"),
        "triage_issue": context["triage_issue"],
        "triage_evidence_dir": context["triage_evidence_dir"],
        "previous_reference": old_refs[0] if old_refs else None,
    }
    reference = None
    if old_refs:
        previous = json.loads(io.read_artifact(old_refs[0]))
        if (
            previous.get("namespace") != scope
            or previous.get("repository") != data["repository"]
            or previous["topic"]["id"] != topic_id
        ):
            raise ValueError("Private evidence does not belong to this backlog issue")
        if previous.get("records_path") == data["records_path"] and previous[
            "topic"
        ] == redact(topic):
            reference = old_refs[0]
    if not reference:
        reference = io.artifact("topic-input.json", data)["reference"]
    inputs = input_text(reference, topic) + weekly_summary_link(
        config["repository"], repository, context["triage_issue"]
    )
    brief = safe_brief(topic)
    if existing:
        io.update_block(
            "backlog-input",
            inputs,
            verify=True,
            expected=f"<!-- automation-improvement-input: {old_refs[0]} -->",
        )
        return existing["url"]

    external = (
        config.get("execution_owner") == "external"
        or config.get("finding_source") == "portable"
    )
    status = (
        EXTERNAL_STATUS
        if external
        else "Ready for investigation. No repair has been validated. The native dispatcher owns the next investigation."
    )
    texts = (
        brief["problem"],
        block("backlog-observation", brief["example"]),
        brief["prevention"],
        brief["validation"]
        + "".join(
            f"\n\n### {section['heading']}\n\n{section['text']}"
            for section in brief.get("submission", [])
        ),
        block("backlog-status", status),
        block("backlog-input", inputs) + "\n\n" + block("topic-evidence", ""),
        f"Initial discovery ranked this topic **P{topic['priority']}**. "
        + priority_reason(topic)
        + "\n\n"
        + block("topic-attempts", ""),
    )
    body = (
        f"<!-- automation-improvement-backlog: {scope}/{topic_id} -->\n"
        f"<!-- automation-improvement-topic: {topic_id} -->\n"
        + "\n"
        + "\n\n".join(
            f"## {heading}\n\n{text}"
            for heading, text in zip(SECTIONS, texts, strict=True)
        )
    )
    if public_issue_errors(body):
        raise ValueError("Backlog issue needs all public brief sections")
    labels = [IMPROVEMENT_LABEL, TOPIC, READY, PRIORITIES[topic["priority"]]]
    if external:
        labels.append(EXTERNAL)
    known_labels = ensure_labels(repository)
    reserved = {label.casefold() for label in LABELS} | {IMPROVEMENT_LABEL}
    for name in brief.get("labels", []):
        key = name.casefold()
        if key not in known_labels or key in reserved:
            raise ValueError(
                "Repository issue guide requested an unavailable or reserved label"
            )
        if known_labels[key] not in labels:
            labels.append(known_labels[key])
    issue = TopicIssue.ensure_brief(
        repository,
        output,
        root,
        body,
        topic["title"],
        preview,
        labels=labels,
        search_unlabeled=prior_evidence,
    )
    if REFERENCE.findall(issue.value["body"]) != [reference]:
        return publish(
            config, context, topic, work_item(issue.value, repository), output
        )
    return issue.value["html_url"]
