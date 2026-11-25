import argparse
import fcntl
import os
import re
from pathlib import Path

from backlog import (
    ACTIVE,
    CUSTOMER,
    EXTERNAL,
    FACTORY,
    OUTCOME_LABELS,
    PRIVATE_TEXT,
    READY,
    native_claimed,
    on_hold,
)
from common import encode, read_json, redact, replace_json
from factory_handoff import (
    GitHub,
    ISSUE,
    LOGIN,
    REPO,
    SHA,
    label_names,
    latest_label,
    login,
)
from public_status import make_status, markdown_text, validation_repository

MARKER = "<!-- automation-improvement-customer: 1 -->"
PULL = re.compile(rf"https://github\.com/({REPO.pattern})/pull/([1-9][0-9]*)")


def public_text(report, key):
    value = report.get(key)
    if (
        not isinstance(value, str)
        or not 1 <= len(value.strip()) <= 600
        or PRIVATE_TEXT.search(value)
        or redact(value) != value
        or "@" in value
        or not value.isprintable()
    ):
        raise ValueError(f"Supply sanitized single-line public text for {key}")
    return value.strip()


def validation_link(url, repo):
    try:
        target = validation_repository(url)
    except ValueError as error:
        raise ValueError(
            "Supply a GitHub Actions run or a commit-pinned file for validation_url"
        ) from error
    if target.casefold() != repo.casefold():
        raise ValueError("The validation_url must point to the issue repository")
    return f"[Customer validation evidence]({url})"


def verify_outcome(gh, report, repo, allowed, actor):
    if not isinstance(report, dict):
        raise ValueError("Supply a customer outcome object")
    outcome = report.get("outcome")
    if outcome not in OUTCOME_LABELS:
        raise ValueError("Unknown customer outcome")
    pulls = report.get("pull_requests", [])
    if not isinstance(pulls, list) or len(pulls) > 32:
        raise ValueError("Expected a bounded list of pull requests")
    validation = markdown_text(public_text(report, "validation"))
    if "validation_url" in report:
        validation += " " + validation_link(report["validation_url"], repo)
    lines = [
        "**Customer-reported validation:** " + validation,
        "**Limitations:** " + markdown_text(public_text(report, "limitations")),
        "**GitHub review and CI:** GitHub confirms PR state and revisions. See GitHub for review decisions and check results."
        if pulls
        else "**GitHub review and CI:** No pull request was supplied.",
    ]
    observed = {}
    visible_pulls = []
    drift = False
    for item in pulls:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError("Invalid pull request reference")
        match = PULL.fullmatch(item["url"])
        if (
            not match
            or match[1].casefold() not in allowed
            or not isinstance(item.get("head"), str)
            or not SHA.fullmatch(item["head"])
            or (
                "base" in item
                and (
                    not isinstance(item["base"], str) or not SHA.fullmatch(item["base"])
                )
            )
        ):
            raise ValueError("Invalid pull request repository or revision")
        target, number = match.groups()
        identity = item["url"].casefold()
        if identity in observed:
            raise ValueError("Duplicate pull request")
        live = gh.api(f"repos/{target}/pulls/{number}")
        if live["html_url"].casefold() != item["url"].casefold():
            raise ValueError("Pull request identity changed")
        head, base = live["head"]["sha"], live["base"]["sha"]
        if not SHA.fullmatch(head) or not SHA.fullmatch(base):
            raise ValueError("GitHub did not confirm the current revisions")
        drift |= head != item["head"] or ("base" in item and item["base"] != base)
        stage = (
            "merged"
            if live.get("merged_at")
            else "closed"
            if live["state"] == "closed"
            else "draft"
            if live["draft"]
            else "open for review"
        )
        observed[identity] = {
            "url": live["html_url"],
            "head": head,
            "base": base,
            "stage": stage,
        }
        if (
            target.casefold() != repo.casefold()
            and gh.api(f"repos/{target}").get("visibility") != "public"
        ):
            lines.append("A related pull request is recorded privately.")
            continue
        url = live["html_url"]
        reference = {
            "url": url,
            "state": "ready" if stage == "open for review" else stage,
            "head": head,
            "base": base,
            "reported_head": item["head"],
        }
        revisions = [f"head `{head}`", f"base `{base}`"]
        if head != item["head"]:
            revisions.append(f"previously reported head `{item['head']}`")
        if "base" in item:
            reference["reported_base"] = item["base"]
            if base != item["base"]:
                revisions.append(f"previously reported base `{item['base']}`")
        visible_pulls.append(reference)
        lines.append(
            f"- [PR {target}#{number}]({url}) ({stage}) · "
            f"[Checks for {target}#{number}]({url}/checks?sha={head}) · {', '.join(revisions)}."
        )
    record = make_status(
        outcome,
        actor,
        report.get("next_actor", "maintainer"),
        public_text(report, "next_action"),
        next_actor_login=report.get("next_actor_login"),
        pull_requests=visible_pulls,
        validation_url=report.get("validation_url"),
    )
    if drift:
        record.update(
            status="incomplete",
            next_actor="executor",
            next_actor_login=actor,
            next_action="Revalidate the changed revision and submit an updated outcome.",
        )
    return record, lines, list(observed.values())


def ownership(gh, config, endpoint, url, actor):
    issue = gh.api(endpoint)
    labels = label_names(issue)
    if (
        "pull_request" in issue
        or issue["html_url"].casefold() != url.casefold()
        or FACTORY in labels
        or not {EXTERNAL, CUSTOMER} <= labels
    ):
        raise ValueError("Issue is outside customer ownership")
    if any(login(user) != actor for user in issue.get("assignees", [])):
        raise ValueError("The issue has another assignee")
    timeline = gh.pages(endpoint + "/timeline?per_page=100")
    event = latest_label(timeline, CUSTOMER)
    enrollers = {v.casefold() for v in config["customer_workflow"]["trusted_enrollers"]}
    if event.get("event") != "labeled" or login(event.get("actor")) not in enrollers:
        raise ValueError("Missing trusted customer enrollment")
    enrollment = event.get("node_id") or event.get("id") or event.get("created_at")
    if not enrollment:
        raise ValueError("Missing customer enrollment identity")
    return issue, labels, timeline, enrollment


def open_repair(timeline, repo, number):
    canonical = re.escape(f"https://github.com/{repo}/issues/{number}")
    short = re.escape(f"{repo}#{number}")
    for event in timeline:
        source = (event.get("source") or {}).get("issue") or {}
        pull = PULL.fullmatch(source.get("html_url") or "")
        if (
            event.get("event") != "cross-referenced"
            or not pull
            or source.get("state") != "open"
        ):
            continue
        patterns = [canonical, short]
        if pull[1].casefold() == repo.casefold():
            patterns.append("#" + number)
        if re.search(
            r"\b(?:clos(?:e[sd]?|ing)|fix(?:es|ed|ing)?|resolv(?:e[sd]?|ing))\s+(?:"
            + "|".join(patterns)
            + r")(?![\w])",
            source.get("body") or "",
            re.I,
        ):
            return True
    return False


def prior_pulls(receipts):
    return {
        pr["url"]
        for receipt in receipts
        for report in [*receipt.get("prior_reports", []), receipt.get("report", {})]
        for pr in report.get("pull_requests", [])
    }


def publish(gh, endpoint, issue_url, attempt, record, lines=(), *, start=False):
    role = record["next_actor"]
    next_actor = "CI" if role == "ci" else role.capitalize()
    if record.get("next_actor_login"):
        next_actor += f" (`{record['next_actor_login']}`)"
    actor = record["owner"]
    body = "\n\n".join(
        [
            f"**Customer workflow:** {record['status'].replace('_', ' ')} · attempt `{attempt}`",
            f"**Implementation owner:** `{actor}`",
            f"**Next actor:** {next_actor}",
            "**Next action:** " + markdown_text(record["next_action"]),
            *lines,
        ]
    )
    status_url = gh.write_status(endpoint, actor, body, record, marker=MARKER)
    return {
        "start": start,
        "issue": issue_url,
        "attempt": attempt,
        "status_url": status_url,
        **record,
    }


def run(config, operation, issue_url, attempt, report=None, *, github=None):
    if operation not in ("claim", "report", "reconcile") or (operation == "report") != (
        report is not None
    ):
        raise ValueError("Supply an outcome only for the report operation")
    settings = config["customer_workflow"]
    gh = github or GitHub()
    match = ISSUE.fullmatch(issue_url)
    allowed = {
        name.casefold() for name in settings["repositories"] if REPO.fullmatch(name)
    }
    if (
        not match
        or match[1].casefold() not in allowed
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", attempt)
        or redact(attempt) != attempt
        or PRIVATE_TEXT.search(attempt)
    ):
        raise ValueError("Invalid customer handoff issue or attempt")
    actor = login(gh.api("user"))
    if not LOGIN.fullmatch(actor) or actor != settings["executor_login"].casefold():
        raise ValueError("The authenticated executor differs from configuration")
    repo, number = match.groups()
    endpoint = f"repos/{repo}/issues/{number}"
    root = Path(config["state_dir"]).expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / "weekly.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = root / "customer-attempts" / repo.casefold() / f"{number}.json"
        previous = read_json(path) if path.exists() else {}
        issue, labels, timeline, enrollment = ownership(
            gh, config, endpoint, issue_url, actor
        )

        def current_labels(*, start=False, held=False):
            current, live_labels, current_timeline, current_enrollment = ownership(
                gh, config, endpoint, issue_url, actor
            )
            if current_enrollment != enrollment:
                raise ValueError("Customer enrollment changed during publication")
            if on_hold(config, live_labels) != held or (
                held and current["state"] != "open"
            ):
                raise ValueError(
                    "The issue's maintainer hold changed during publication"
                )
            if start and (
                current["state"] != "open"
                or not {ACTIVE, READY} & live_labels
                or open_repair(current_timeline, repo, number)
            ):
                raise ValueError("The issue is no longer eligible to start")
            return live_labels

        factory = root / "factory-handoff.json"
        if factory.exists() and endpoint.casefold() in {
            key.casefold() for key in read_json(factory)
        }:
            raise ValueError("Reconcile the recorded Factory attempt before transfer")
        if native_claimed(root, issue_url):
            raise ValueError("Reconcile the native repair before transfer")
        if on_hold(config, labels):
            if (
                operation != "reconcile"
                or issue["state"] != "open"
                or previous.get("attempt") != attempt
                or previous.get("owner") != actor
                or previous.get("enrollment") != enrollment
            ):
                raise ValueError("The issue has a maintainer hold")
            record = make_status(
                "maintainer_hold",
                actor,
                "maintainer",
                "Resolve the blocker and remove the hold label when work can resume.",
            )
            current_labels(held=True)
            return publish(gh, endpoint, issue_url, attempt, record)
        if previous.get("attempt") != attempt:
            if (
                operation != "claim"
                or issue["state"] != "open"
                or READY not in labels
                or ACTIVE in labels
                or open_repair(timeline, repo, number)
            ):
                raise ValueError("No eligible new customer attempt")
            if not previous and any(
                login(comment.get("user")) == actor
                and (comment.get("body") or "").startswith(MARKER + "\n")
                for comment in gh.pages(endpoint + "/comments?per_page=100")
            ):
                raise ValueError(
                    "Recover the existing customer receipt before restarting"
                )
            if (path.parent / f"{number}-{attempt}.json").exists():
                raise ValueError("This attempt ID has already been used")
            if previous:
                if "report" not in previous or previous.get("publication_pending"):
                    raise ValueError("A customer attempt is already active")
                receipts = [
                    previous,
                    *(
                        read_json(archived)
                        for archived in path.parent.glob(f"{number}-*.json")
                    ),
                ]
                for prior_url in sorted(prior_pulls(receipts)):
                    m = PULL.fullmatch(prior_url)
                    if not m or m[1].casefold() not in allowed:
                        raise ValueError(
                            "A prior pull request is outside the configured scope"
                        )
                    if gh.api(f"repos/{m[1]}/pulls/{m[2]}")["state"] == "open":
                        raise ValueError(
                            "Reconcile the prior open pull request before requeue"
                        )
                replace_json(
                    path.parent / f"{number}-{previous['attempt']}.json", previous
                )
            previous = {"attempt": attempt, "owner": actor, "enrollment": enrollment}
            replace_json(path, previous)
        if actor != previous["owner"]:
            raise ValueError("The claimed attempt belongs to another executor")
        if previous.setdefault("enrollment", enrollment) != enrollment:
            raise ValueError("Customer enrollment changed during the attempt")
        start = (
            operation == "claim"
            and "report" not in previous
            and not previous.get("start_delivered")
        )
        if operation == "report":
            if not previous.get("start_delivered") and "report" not in previous:
                raise ValueError("Complete the claim before reporting")
            prior = previous.get("report")
            history = previous.setdefault("prior_reports", [])
            if report != prior and report not in history:
                if prior:
                    history.append(prior)
                previous["report"] = report
        if "report" in previous:
            record, lines, previous["observed_pulls"] = verify_outcome(
                gh, previous["report"], repo, allowed, actor
            )
        elif operation == "reconcile" and not previous.get("start_delivered"):
            raise ValueError("Complete the claim before reconciling")
        else:
            record = make_status(
                "implementing",
                actor,
                "executor",
                "Investigate the issue and report the outcome.",
                next_actor_login=actor,
            )
            lines = []
        outcome = record["status"]
        current_labels(start=start)
        previous.update(outcome=outcome, publication_pending=True)
        replace_json(path, previous)
        published = publish(
            gh, endpoint, issue_url, attempt, record, lines, start=start
        )
        labels = current_labels(start=start)
        desired = {ACTIVE} if outcome == "implementing" else {OUTCOME_LABELS[outcome]}
        add = desired - labels
        remove = (labels & {ACTIVE, *OUTCOME_LABELS.values()}) - desired
        if outcome == "implementing" and READY in labels:
            remove.add(READY)
        if add:
            gh.api(
                endpoint + "/labels",
                "POST",
                ["labels[]=" + label for label in sorted(add)],
            )
            labels = current_labels(start=start)
        for name in sorted(remove):
            if name in labels:
                gh.api(endpoint + "/labels/" + name, "DELETE")
                labels = current_labels(start=start)
        if not desired <= labels or remove & labels:
            raise ValueError(
                "Customer lifecycle or enrollment changed during publication"
            )
        previous["publication_pending"] = False
        if start:
            previous["start_delivered"] = True
        replace_json(path, previous)
        return published
    finally:
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["claim", "report", "reconcile"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if (args.operation == "report") != bool(args.report):
        parser.error("Supply --report only for the report operation")
    print(
        encode(
            run(
                read_json(args.config),
                args.operation,
                args.issue,
                args.attempt,
                read_json(args.report) if args.report else None,
            )
        )
    )
