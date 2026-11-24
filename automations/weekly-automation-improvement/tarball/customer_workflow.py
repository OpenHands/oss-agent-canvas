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

MARKER = "<!-- automation-improvement-customer: 1 -->"
PULL = re.compile(rf"https://github\.com/({REPO.pattern})/pull/([1-9][0-9]*)")


def public_text(report, key):
    value = report.get(key)
    if (
        not isinstance(value, str)
        or not 1 <= len(value.strip()) <= 600
        or PRIVATE_TEXT.search(value)
        or redact(value) != value
        or any(c in value for c in ("\n", "\r", "@"))
    ):
        raise ValueError(f"Supply sanitized single-line public text for {key}")
    return value.strip()


def verify_outcome(gh, report, repo, allowed):
    if not isinstance(report, dict):
        raise ValueError("Supply a customer outcome object")
    outcome = report.get("outcome")
    if outcome not in OUTCOME_LABELS:
        raise ValueError("Unknown customer outcome")
    pulls = report.get("pull_requests", [])
    if not isinstance(pulls, list) or len(pulls) > 32:
        raise ValueError("Expected a bounded list of pull requests")
    lines = [
        "**Reported validation:** " + public_text(report, "validation"),
        "**Limitations:** " + public_text(report, "limitations"),
        "**Next action:** " + public_text(report, "next_action"),
    ]
    seen = set()
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
        identity = (target.casefold(), number)
        if identity in seen:
            raise ValueError("Duplicate pull request")
        seen.add(identity)
        live = gh.api(f"repos/{target}/pulls/{number}")
        if live["html_url"].casefold() != item["url"].casefold():
            raise ValueError("Pull request identity changed")
        head, base = live["head"]["sha"], live["base"]["sha"]
        if not SHA.fullmatch(head) or not SHA.fullmatch(base):
            raise ValueError("GitHub did not confirm the current revisions")
        drift |= head != item["head"] or ("base" in item and item["base"] != base)
        if (
            target.casefold() != repo.casefold()
            and gh.api(f"repos/{target}").get("visibility") != "public"
        ):
            lines.append("A related pull request is recorded privately.")
            continue
        stage = (
            "merged"
            if live.get("merged_at")
            else "closed"
            if live["state"] == "closed"
            else "draft"
            if live["draft"]
            else "open for review"
        )
        revisions = f"reported head `{item['head']}`, current head `{head}`"
        if "base" in item:
            revisions += f", reported base `{item['base']}`"
        lines.append(
            f"- {live['html_url']} ({stage}), {revisions}, current base `{base}`."
        )
    if drift:
        outcome = "incomplete"
        lines[2] = (
            "**Next action:** Revalidate the changed revision and submit an updated outcome."
        )
    return outcome, lines


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
    if on_hold(config, labels) or any(
        login(user) != actor for user in issue.get("assignees", [])
    ):
        raise ValueError("The issue has a maintainer hold or another assignee")
    timeline = gh.pages(endpoint + "/timeline?per_page=100")
    event = latest_label(timeline, CUSTOMER)
    enrollers = {v.casefold() for v in config["customer_workflow"]["trusted_enrollers"]}
    if event.get("event") != "labeled" or login(event.get("actor")) not in enrollers:
        raise ValueError("Missing trusted customer enrollment")
    return (
        issue,
        labels,
        timeline,
        (event.get("node_id") or event.get("id") or event.get("created_at")),
    )


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
        factory = root / "factory-handoff.json"
        if factory.exists() and endpoint.casefold() in {
            key.casefold() for key in read_json(factory)
        }:
            raise ValueError("Reconcile the recorded Factory attempt before transfer")
        if native_claimed(root, issue_url):
            raise ValueError("Reconcile the native repair before transfer")
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
        if start and (
            issue["state"] != "open"
            or not {ACTIVE, READY} & labels
            or open_repair(timeline, repo, number)
        ):
            raise ValueError("The issue is no longer eligible to start")
        if operation == "report":
            if not previous.get("start_delivered") and "report" not in previous:
                raise ValueError("Complete the claim before reporting")
            prior = previous.get("report")
            history = previous.setdefault("prior_reports", [])
            if report != prior and report not in history:
                if prior:
                    history.append(prior)
                previous["report"] = report
        outcome, lines = (
            "implementing",
            ["**Next action:** The customer executor investigates the issue."],
        )
        if "report" in previous:
            outcome, lines = verify_outcome(gh, previous["report"], repo, allowed)
        elif operation == "reconcile":
            raise ValueError("No reported outcome to reconcile")
        previous.update(outcome=outcome, publication_pending=True)
        replace_json(path, previous)
        body = "\n\n".join(
            [
                MARKER,
                f"**Customer workflow:** {actor} · attempt `{attempt}` · **{outcome}**",
                *lines,
            ]
        )
        gh.write_comment(endpoint, actor, body, marker=MARKER)
        desired = {ACTIVE} if outcome == "implementing" else {OUTCOME_LABELS[outcome]}
        add = desired - labels
        if add:
            gh.api(
                endpoint + "/labels",
                "POST",
                ["labels[]=" + label for label in sorted(add)],
            )
        remove = (labels & {ACTIVE, *OUTCOME_LABELS.values()}) - desired
        if outcome == "implementing" and READY in labels:
            remove.add(READY)
        for name in sorted(remove):
            gh.api(endpoint + "/labels/" + name, "DELETE")
        current, after, timeline, current_enrollment = ownership(
            gh, config, endpoint, issue_url, actor
        )
        if (
            not desired <= after
            or remove & after
            or current_enrollment != enrollment
            or start
            and (current["state"] != "open" or open_repair(timeline, repo, number))
        ):
            raise ValueError(
                "Customer lifecycle or enrollment changed during publication"
            )
        previous["publication_pending"] = False
        if start:
            previous["start_delivered"] = True
        replace_json(path, previous)
        return {
            "start": start,
            "issue": issue_url,
            "attempt": attempt,
            "owner": actor,
            "status": outcome,
        }
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
