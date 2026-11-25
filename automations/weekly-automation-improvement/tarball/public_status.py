import html
import json
import re

from common import encode, redact

SCHEMA = "openhands.improvement-status.v1"
PREFIX = "<!-- automation-improvement-status: "
ROLES = ("maintainer", "executor", "reviewer", "ci", "dispatcher")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?")
SHA = re.compile(r"[0-9a-f]{40}")
REPO = r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]+"
PULL = re.compile(rf"https://github\.com/({REPO})/pull/[1-9][0-9]*")
VALIDATION = re.compile(
    rf"https://github\.com/({REPO})/(?:actions/runs/[1-9][0-9]*|blob/{SHA.pattern}/([A-Za-z0-9._/-]+))"
)
STATUS_START = "<!-- backlog-status:start -->"
STATUS_END = "<!-- backlog-status:end -->"


def pull_record(item):
    if (
        not isinstance(item, dict)
        or not isinstance(item.get("url"), str)
        or not PULL.fullmatch(item["url"])
    ):
        raise ValueError("Invalid public pull request URL")
    if item.get("state") not in ("draft", "ready", "closed", "merged"):
        raise ValueError("Invalid public pull request state")
    result = {"url": item["url"], "state": item["state"]}
    for key in ("head", "base", "reported_head", "reported_base", "reviewed_head"):
        if key == "head" or key in item:
            if not isinstance(item.get(key), str) or not SHA.fullmatch(item[key]):
                raise ValueError("Invalid public pull request revision")
            result[key] = item[key]
    result["checks_url"] = f"{result['url']}/checks?sha={result['head']}"
    if "checks_url" in item and item["checks_url"] != result["checks_url"]:
        raise ValueError("Checks URL differs from the public pull request revision")
    return result


def validation_repository(url):
    match = (
        VALIDATION.fullmatch(url) if isinstance(url, str) and len(url) <= 2048 else None
    )
    if (
        not match
        or (match[2] and {"", ".", ".."}.intersection(match[2].split("/")))
        or redact(url) != url
    ):
        raise ValueError("Invalid public validation URL")
    return match[1]


def make_status(
    status,
    owner,
    next_actor,
    next_action,
    *,
    next_actor_login=None,
    pull_requests=None,
    validation_url=None,
):
    if not isinstance(status, str) or not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,63}", status
    ):
        raise ValueError("Invalid improvement status")
    if not isinstance(owner, str) or not LOGIN.fullmatch(owner):
        raise ValueError("Invalid improvement owner")
    if next_actor not in ROLES:
        raise ValueError("Invalid next actor role")
    if next_actor_login is not None and (
        not isinstance(next_actor_login, str) or not LOGIN.fullmatch(next_actor_login)
    ):
        raise ValueError("Invalid next actor login")
    if (
        not isinstance(next_action, str)
        or not 1 <= len(next_action.strip()) <= 600
        or not next_action.isprintable()
        or redact(next_action) != next_action
    ):
        raise ValueError("Supply a public, single-line next action")
    record = {
        "schema": SCHEMA,
        "status": status,
        "owner": owner,
        "next_actor": next_actor,
        "next_action": next_action.strip(),
    }
    if next_actor_login is not None:
        record["next_actor_login"] = next_actor_login
    if pull_requests is not None:
        if not isinstance(pull_requests, list):
            raise ValueError("Invalid public pull request list")
        pulls = [pull_record(item) for item in pull_requests]
        if len({pr["url"].casefold() for pr in pulls}) != len(pulls):
            raise ValueError("Multiple entries for the same public pull request")
        if pulls:
            record["pull_requests"] = pulls[:32]
        if len(pulls) > 32:
            record["additional_pull_requests"] = len(pulls) - 32
    if validation_url is not None:
        validation_repository(validation_url)
        record["validation_url"] = validation_url
    return record


def status_marker(record):
    data = encode(record).replace("<", r"\u003c").replace(">", r"\u003e")
    return PREFIX + data + " -->"


def read_marker(text):
    lines = [line for line in text.splitlines() if line.startswith(PREFIX)]
    if not lines:
        return None
    if len(lines) != 1 or not lines[0].endswith(" -->"):
        raise ValueError("Ambiguous or malformed improvement status")
    data = json.loads(lines[0][len(PREFIX) : -4])
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("Unsupported improvement status schema")
    record = make_status(
        data.get("status"),
        data.get("owner"),
        data.get("next_actor"),
        data.get("next_action"),
        next_actor_login=data.get("next_actor_login"),
        pull_requests=data.get("pull_requests"),
        validation_url=data.get("validation_url"),
    )
    if "additional_pull_requests" in data:
        count = data["additional_pull_requests"]
        if (
            type(count) is not int
            or count <= 0
            or len(record.get("pull_requests", [])) != 32
        ):
            raise ValueError("Invalid count of additional pull requests")
        record["additional_pull_requests"] = count
    return record


def markdown_text(value):
    escaped = html.escape(value, quote=False).replace("@", "&#64;")
    return re.sub(r"([\\`*_\[\]|~])", r"\\\1", escaped)


def managed_block(issue):
    from backlog import IDENTITY, TOPIC

    body = issue.get("body") or ""
    if (
        TOPIC not in {label["name"] for label in issue.get("labels", [])}
        or len(IDENTITY.findall(body)) != 1
    ):
        return None
    if STATUS_START not in body and STATUS_END not in body:
        return None
    if body.count(STATUS_START) != 1 or body.count(STATUS_END) != 1:
        raise ValueError("Ambiguous managed issue status section")
    start, end = body.index(STATUS_START) + len(STATUS_START), body.index(STATUS_END)
    if start > end:
        raise ValueError("Malformed managed issue status section")
    return start, end


def update_issue_status(gh, endpoint, comment_id, record, *, current_issue=None):
    issue = current_issue() if current_issue else gh.api(endpoint)
    bounds = managed_block(issue)
    if bounds is None:
        return
    issue_url = issue["html_url"]
    if (
        endpoint.casefold()
        != ("repos/" + issue_url.removeprefix("https://github.com/")).casefold()
        or not issue_url.startswith("https://github.com/")
        or type(comment_id) is not int
        or comment_id <= 0
    ):
        raise ValueError("Could not verify the improvement status link")
    actor = "CI" if record["next_actor"] == "ci" else record["next_actor"].capitalize()
    if record.get("next_actor_login"):
        actor += " (`" + record["next_actor_login"] + "`)"
    sections = [
        f"**Status:** {record['status'].replace('_', ' ').replace('-', ' ').capitalize()} · **Owner:** `{record['owner']}`",
        f"**Next actor:** {actor}.",
        "**Next action:** " + markdown_text(record["next_action"]),
    ]
    pulls = record.get("pull_requests", [])
    for pull in pulls[:3]:
        url = pull["url"]
        number = url.rsplit("/", 1)[1]
        sections.append(
            f"[PR #{number}]({url}) ({pull['state']}) · "
            f"[Checks for `{pull['head'][:12]}`]({pull['checks_url']})"
        )
    more = len(pulls[3:]) + record.get("additional_pull_requests", 0)
    if more:
        sections.append(f"The full handoff lists {more} more pull request(s).")
    if "validation_url" in record:
        sections.append(f"[Customer validation evidence]({record['validation_url']})")
    sections.extend(
        [
            f"[Current handoff and validation details]({issue_url}#issuecomment-{comment_id})",
            status_marker(record),
        ]
    )
    content = "\n\n".join(sections)
    start, end = bounds
    body = issue["body"]
    desired = "\n" + content + "\n"
    if body[start:end] != desired:
        gh.api(endpoint, "PATCH", ["body=" + body[:start] + desired + body[end:]])
        confirmed = gh.api(endpoint)
        bounds = managed_block(confirmed)
        if bounds is None or confirmed["body"][slice(*bounds)] != desired:
            raise ValueError("GitHub did not confirm the managed issue status")
