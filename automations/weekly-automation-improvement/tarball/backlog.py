import hashlib
import re
from pathlib import Path

from common import redact
from github_io import IMPROVEMENT_LABEL, command, github_pages
from topic_issue import SECTIONS, TopicIssue, public_issue_errors

TOPIC = "improvement-topic"
READY = "improvement-ready"
ACTIVE = "improvement-in-progress"
BLOCKED = "improvement-blocked"
PRIORITIES = tuple(f"priority:P{i}" for i in range(4))
LABELS = {
    TOPIC: "6056B3",
    READY: "0E8A16",
    ACTIVE: "1D76DB",
    BLOCKED: "B60205",
    **dict.fromkeys(PRIORITIES, "FBCA04"),
}
IDENTITY = re.compile(
    r"(?m)^<!-- automation-improvement-backlog: ([a-f0-9]{20})/([a-f0-9]{20}) -->$"
)
REFERENCE = re.compile(r"<!-- automation-improvement-input: ([a-f0-9]{64}\.json) -->")
BRIEF_FIELDS = ("problem", "example", "prevention", "validation")


def namespace(config, state_dir):
    values = [
        config["repository"].casefold(),
        config["project_id"],
        config["signal_id"],
    ]
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
        ["git", "ls-tree", "-r", "--name-only", source["revision"]],
        source["checkout"],
    )
    guides = {"agents.md", "contributing.md", "readme.md", "issue_template.md"}
    return [
        path
        for path in files.splitlines()
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
            or re.search(
                r"[<>\\`]|https?://|(?:^|[\s(])/?[\w.-]+/[\w./-]+"
                r"|(?m:^\s*#)|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}",
                value,
                re.I,
            )
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


def block(name, text):
    return f"<!-- {name}:start -->\n{text}\n<!-- {name}:end -->"


def input_text(reference):
    return (
        f"<!-- automation-improvement-input: {reference} -->\n"
        f"Authorized workers can read the private topic index `{reference}` with "
        "`read_issue_artifact`. Original Signals, traces and preparation records are retained privately."
    )


def publish(config, context, topic, existing, output, *, preview=False):
    scope = namespace(config, context["state_dir"])
    repository = existing["repository"] if existing else config["repository"]
    topic_id = identity(existing, scope) if existing else topic["id"]
    root = evidence_root(context["state_dir"], repository, topic_id)
    if preview:
        safe_brief(topic)
        return existing["url"] if existing else None

    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    io = TopicIssue(repository, output, root, {"number": 0}, [], preview)
    old_body = existing["body"] if existing else ""
    old_refs = REFERENCE.findall(old_body)
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
        "triage_issue": context["triage_issue"],
        "triage_evidence_dir": context["triage_evidence_dir"],
        "previous_reference": old_refs[0] if old_refs else None,
    }
    reference = io.artifact("topic-input.json", data)["reference"]
    brief = safe_brief(topic)
    if existing:
        number = int(existing["url"].rsplit("/", 1)[1])
        issue = TopicIssue(repository, output, root, {"number": number}, [], preview)
        issue.refresh()
        if REFERENCE.findall(issue.value["body"]) != old_refs:
            raise ValueError("Backlog issue evidence changed during triage")
        if "<!-- automation-improvement-run:" not in issue.value["body"]:
            issue.update_block("backlog-observation", brief["example"])
        issue.update_block("backlog-input", input_text(reference))
        return existing["url"]

    texts = (
        brief["problem"],
        block("backlog-observation", brief["example"]),
        brief["prevention"],
        brief["validation"]
        + "".join(
            f"\n\n### {section['heading']}\n\n{section['text']}"
            for section in brief.get("submission", [])
        ),
        block(
            "backlog-status", "Ready for investigation. No repair has been validated."
        ),
        block("backlog-input", input_text(reference))
        + "\n\n"
        + block("topic-evidence", ""),
        "Initial triage recorded this investigation lead."
        + "\n\n"
        + block("topic-attempts", ""),
    )
    body = (
        f"<!-- automation-improvement-backlog: {scope}/{topic_id} -->\n"
        f"<!-- automation-improvement-topic: {topic_id} -->\n"
        + event_markers(topic)
        + "\n"
        + "\n\n".join(
            f"## {heading}\n\n{text}"
            for heading, text in zip(SECTIONS, texts, strict=True)
        )
    )
    if public_issue_errors(body):
        raise ValueError("Backlog issue needs all public brief sections")
    labels = [IMPROVEMENT_LABEL, TOPIC, READY, PRIORITIES[topic["priority"]]]
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
    )
    return issue.value["html_url"]
