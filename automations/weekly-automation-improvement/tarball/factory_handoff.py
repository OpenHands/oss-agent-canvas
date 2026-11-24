import argparse
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from backlog import (
    ACTIVE,
    BLOCKED,
    CUSTOMER,
    EXTERNAL,
    FACTORY,
    OUTCOME_LABELS,
    PRIORITIES,
    READY,
    native_claimed,
    on_hold,
)
from common import callback, encode, read_json, replace_json
from factory_linear import Linear, matches_target
from github_io import command, json_pages

REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}$)[A-Za-z0-9_.-]+")
ISSUE = re.compile(rf"https://github\.com/({REPO.pattern})/issues/([1-9][0-9]*)")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
SHA = re.compile(r"[a-f0-9]{40}")
STATUS = "<!-- automation-improvement-factory: 1 -->"
RESULT = "<!-- automation-improvement-resolver: {} -->"
TERMINAL = {"blocked", "unrepaired", "merged", "closed"}
NEXT = {
    "assigning": ("Assigning this issue to the configured resolver.", "Dispatcher"),
    "filing": (
        "Preparing this issue for the configured implementation queue.",
        "Dispatcher",
    ),
    "implementing": (
        "The resolver is investigating and testing this issue.",
        "Resolver",
    ),
    "checks": ("The current revision needs all configured checks to pass.", "CI"),
    "reviewing": (
        "The current revision is waiting for independent review.",
        "Reviewer",
    ),
    "revision-needed": (
        "The current revision requires changes or has failing checks.",
        "Maintainer",
    ),
    "incomplete": ("The reviewer could not verify this revision.", "Maintainer"),
    "accepted": (
        "The current revision passed the configured checks and independent review.",
        "Maintainer",
    ),
    "blocked": ("The external handoff needs maintainer intervention.", "Maintainer"),
    "unrepaired": ("The resolver finished without a patch.", "Maintainer"),
    "merged": ("GitHub reports the linked pull request was merged.", "Maintainer"),
    "closed": ("GitHub reports the linked work was closed.", "Maintainer"),
}


def options(config):
    value = config.get("factory_handoff", {})
    if not isinstance(value, dict) or type(value.get("enabled", False)) is not bool:
        raise ValueError("factory_handoff.enabled must be a boolean")
    if not value.get("enabled"):
        return None
    dispatcher = value.get("dispatcher_login", "")
    enrollers = value.get("trusted_enrollers", [])
    routes = value.get("repositories", {})
    if (
        not isinstance(dispatcher, str)
        or not LOGIN.fullmatch(dispatcher)
        or not isinstance(enrollers, list)
        or not enrollers
        or any(
            not isinstance(name, str) or not LOGIN.fullmatch(name) for name in enrollers
        )
        or not isinstance(routes, dict)
        or not routes
    ):
        raise ValueError(
            "Set a dispatcher login, trusted enrollers and repository routes"
        )
    repositories = {}
    token_env = value.get("linear_token_env", "LINEAR_API_KEY")
    if not isinstance(token_env, str) or not re.fullmatch(
        r"[A-Z_][A-Z_0-9]*", token_env
    ):
        raise ValueError("Configure linear_token_env as an environment variable name")
    for repo, route in routes.items():
        if (
            not REPO.fullmatch(repo)
            or repo.casefold() in repositories
            or not isinstance(route, dict)
        ):
            raise ValueError("Factory repositories must be unique owner/repo names")
        mode = route.get("mode", "assignment")
        if mode not in ("assignment", "linear"):
            raise ValueError("Factory route mode must be assignment or linear")
        actors = [route.get(key, "") for key in ("resolver_author", "reviewer")]
        if any(
            not isinstance(name, str) or not LOGIN.fullmatch(name) for name in actors
        ):
            raise ValueError(
                "Configure the resolver author and reviewer for each repository"
            )
        author, reviewer = map(str.casefold, actors)
        if author == reviewer or dispatcher.casefold() in (author, reviewer):
            raise ValueError(
                "Use separate dispatcher, resolver author and reviewer accounts"
            )
        checks = route.get("required_checks")
        scopes = route.get("path_scopes", ["*"])
        if any(
            not isinstance(names, list)
            or not names
            or any(not isinstance(name, str) or not name.strip() for name in names)
            for names in (checks, scopes)
        ):
            raise ValueError(
                "Configure required_checks and nonempty path_scopes for each repository"
            )
        selected = repositories[repo.casefold()] = {
            "repo": repo,
            "mode": mode,
            "resolver_author": author,
            "reviewer": reviewer,
            "checks": sorted(set(checks)),
            "scopes": scopes,
        }
        if mode == "assignment":
            assignee = route.get("assignee", "")
            if (
                not isinstance(assignee, str)
                or not LOGIN.fullmatch(assignee)
                or assignee.casefold() == dispatcher.casefold()
            ):
                raise ValueError(
                    "Configure a resolver assignee distinct from the dispatcher"
                )
            selected["assignee"] = assignee.casefold()
            continue
        for field, default in (
            ("team_key", "OHE"),
            ("linear_label", "ohe-beta:implement"),
            ("linear_state", "Todo"),
        ):
            setting = route.get(field, default)
            if not isinstance(setting, str) or not setting.strip():
                raise ValueError(
                    "Configure the Linear team, trigger label and initial state"
                )
            selected[field] = setting
        if not re.fullmatch(r"[A-Z][A-Z0-9]*", selected["team_key"]):
            raise ValueError("Use a Linear team key")
        selected["linear_resolver_user_id"] = (
            str(uuid.UUID(route["linear_resolver_user_id"]))
            if route.get("linear_resolver_user_id")
            else None
        )
    maximum = value.get("max_new_per_run", 3)
    timeout = value.get("timeout_seconds", 172800)
    if (
        type(maximum) is not int
        or maximum < 1
        or type(timeout) is not int
        or timeout < 1
    ):
        raise ValueError("Factory handoff limits must be positive integers")
    return (
        dispatcher.casefold(),
        {name.casefold() for name in enrollers},
        repositories,
        maximum,
        timeout,
        token_env,
        config,
    )


def login(row):
    return (row or {}).get("login", "").casefold()


def label_names(row):
    return {label["name"] for label in row.get("labels", [])}


class GitHub:
    def __init__(self, request=None):
        self.request = request or command

    def api(self, endpoint, method="GET", fields=()):
        args = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        for field in fields:
            args.extend(("-f", field))
        result = self.request(args)
        return json.loads(result) if result else None

    def pages(self, endpoint):
        return json_pages(
            self.request(
                ["gh", "api", "--hostname", "github.com", "--paginate", endpoint]
            )
        )

    def write_comment(self, endpoint, author, body, marker=STATUS):
        comments = self.pages(endpoint + "/comments?per_page=100")
        owned = [
            row
            for row in comments
            if login(row.get("user")) == author
            and (row.get("body") or "").startswith(marker + "\n")
        ]
        if len(owned) > 1:
            raise ValueError("Multiple dispatcher status comments exist")
        if owned and owned[0]["body"] == body:
            return
        if not owned:
            self.api(endpoint + "/comments", "POST", ["body=" + body])
        else:
            self.api(
                endpoint.split("/issues/")[0] + f"/issues/comments/{owned[0]['id']}",
                "PATCH",
                ["body=" + body],
            )
        confirmed = [
            row.get("body")
            for row in self.pages(endpoint + "/comments?per_page=100")
            if login(row.get("user")) == author
            and (row.get("body") or "").startswith(marker + "\n")
        ]
        if confirmed != [body]:
            raise ValueError("GitHub did not confirm the status comment")

    def sync_labels(self, endpoint, issue, status):
        labels = label_names(issue)
        desired = {ACTIVE} if status not in (TERMINAL | {"accepted"}) else set()
        if status == "blocked":
            desired.add(BLOCKED)
        if status in OUTCOME_LABELS:
            desired.add(OUTCOME_LABELS[status])
        managed = {ACTIVE, BLOCKED, READY, *OUTCOME_LABELS.values()}
        add, remove = sorted(desired - labels), sorted((labels & managed) - desired)
        if add:
            self.api(endpoint + "/labels", "POST", ["labels[]=" + name for name in add])
        for name in remove:
            self.api(endpoint + "/labels/" + name, "DELETE")
        if add or remove:
            after = label_names(self.api(endpoint))
            if after & managed != desired:
                raise ValueError("GitHub did not confirm the issue labels")

    def checks(self, repo, sha, required):
        endpoint = f"repos/{repo}/commits/{sha}"
        states = {}
        for row in self.pages(endpoint + "/statuses?per_page=100"):
            states.setdefault(row["context"], [row["state"]])
        runs = json_pages(
            self.request(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--paginate",
                    "--jq",
                    ".check_runs",
                    endpoint + "/check-runs?filter=latest&per_page=100",
                ]
            )
        )
        for row in runs:
            states.setdefault(row["name"], []).append(
                row.get("conclusion") if row["status"] == "completed" else "pending"
            )
        if any(
            state
            in {
                "error",
                "failure",
                "action_required",
                "cancelled",
                "timed_out",
                "stale",
                "startup_failure",
            }
            for check in required
            for state in states.get(check, ())
        ):
            return "failed"
        if all(
            states.get(check) and set(states[check]) == {"success"}
            for check in required
        ):
            return "green"
        return "pending"


def latest_label(timeline, name):
    return next(
        (
            event
            for event in reversed(timeline)
            if event.get("event") in {"labeled", "unlabeled"}
            and (event.get("label") or {}).get("name") == name
        ),
        {},
    )


def latest_assignment(timeline, assignee):
    return next(
        (
            event
            for event in reversed(timeline)
            if event.get("event") in {"assigned", "unassigned"}
            and login(event.get("assignee")) == assignee
        ),
        {},
    )


def review_decision(review):
    state = review.get("state")
    if state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
        return state
    if state == "COMMENTED":
        lines = [line.strip() for line in (review.get("body") or "").splitlines()]
        for line in reversed(lines):
            if line == "🔄 CHANGES REQUESTED":
                return "CHANGES_REQUESTED"
            if line == "⏸ INCONCLUSIVE":
                return "INCONCLUSIVE"
    return None


class Dispatcher:
    def __init__(
        self, opts, state_path, github, *, linear=None, dry_run=False, now=None
    ):
        (
            self.actor,
            self.enrollers,
            self.routes,
            self.maximum,
            self.timeout,
            token_env,
            self.config,
        ) = opts
        self.state_path = state_path
        self.state = read_json(state_path) if state_path.is_file() else {}
        self.gh = github
        self.linear = linear or Linear(token_env)
        self.dry_run = dry_run
        self.now = time.time() if now is None else now

    def save(self):
        replace_json(self.state_path, self.state)

    def publish(self, endpoint, issue, attempt):
        status = attempt["status"]
        text, actor = NEXT[status]
        extra = attempt.get("detail", "")
        if attempt.get("pr") is not None:
            repo = attempt["route"]["repo"]
            extra += (
                f"\n\n[Pull request](https://github.com/{repo}/pull/{attempt['pr']})"
            )
            if attempt.get("head"):
                extra += f" at `{attempt['head']}`"
            if "draft" in attempt:
                extra += " (draft)" if attempt["draft"] else " (ready for review)"
            extra += "."
        route = attempt["route"]
        if actor == "Reviewer":
            actor = "@" + route["reviewer"]
        elif actor == "Resolver":
            actor = "@" + route["resolver_author"]
        body = f"{STATUS}\n\n**Owner:** External Factory ({route['mode']}).\n\n{text}\n\n{extra}\n\n**Next actor:** {actor}."
        self.gh.write_comment(endpoint, self.actor, body)
        self.gh.sync_labels(endpoint, issue, status)

    def transition(self, endpoint, issue, attempt, status, detail=""):
        attempt.update(status=status, detail=detail)
        if status == "implementing":
            attempt["dispatched"] = True
        self.save()
        self.publish(endpoint, issue, attempt)
        return status

    def assign(self, endpoint, issue, attempt, route):
        repo = route["repo"]
        if self.gh.api(f"repos/{repo}").get("visibility") != "public":
            return self.transition(
                endpoint,
                issue,
                attempt,
                "blocked",
                "The configured OSS resolver requires a public repository.",
            )
        self.publish(endpoint, issue, attempt)
        timeline = endpoint + "/timeline?per_page=100"
        assignee = route["assignee"]
        assignment = latest_assignment(self.gh.pages(timeline), assignee)
        if not assignment and not issue.get("assignees"):
            attempt["dispatch_intent"] = True
            self.save()
            self.gh.api(endpoint + "/assignees", "POST", ["assignees[]=" + assignee])
            issue = self.gh.api(endpoint)
            assignment = latest_assignment(self.gh.pages(timeline), assignee)
        if assignee not in set(map(login, issue.get("assignees", []))):
            return self.transition(
                endpoint,
                issue,
                attempt,
                "blocked",
                "GitHub did not confirm the resolver assignment.",
            )
        if not assignment and self.now - attempt["started"] < self.timeout:
            return "awaiting_assignment_event"
        if (
            assignment.get("event") != "assigned"
            or login(assignment.get("actor")) != self.actor
        ):
            return self.transition(
                endpoint,
                issue,
                attempt,
                "blocked",
                "GitHub did not confirm a trusted assignment event.",
            )
        return self.transition(endpoint, issue, attempt, "implementing")

    def find_pulls(self, route, number):
        owner = route["repo"].split("/", 1)[0]
        branch = (
            f"openhands/{'factory-' if route['mode'] == 'linear' else ''}issue-{number}"
        )
        prefix = f"repos/{route['repo']}/pulls?state=all&head={owner}:"
        return self.gh.pages(prefix + branch + "&per_page=100")

    @staticmethod
    def competing_detail(route, numbers):
        links = ", ".join(
            f"[#{n}](https://github.com/{route['repo']}/pull/{n})"
            for n in sorted(numbers)
        )
        return "Reconcile the existing pull requests before continuing. " + links

    @staticmethod
    def linked_pulls(route, number, timeline):
        repo = route["repo"]
        refs = {}
        close = re.compile(
            rf"(?i)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+"
            rf"(?:#{number}\b|https://github\.com/{re.escape(repo)}/issues/{number}\b)"
        )
        for event in timeline:
            source = (event.get("source") or {}).get("issue") or {}
            if (
                event.get("event") == "cross-referenced"
                and source.get("pull_request")
                and source.get("state") == "open"
                and source.get("html_url", "").casefold()
                == f"https://github.com/{repo}/pull/{source.get('number')}".casefold()
                and close.search(source.get("body") or "")
            ):
                refs[source["number"]] = source
        return refs

    def resolver_outcome(self, endpoint, route, ticket):
        if ticket:
            labels = {row["name"] for row in ticket["labels"]["nodes"]}
            if (
                ticket.get("archivedAt")
                or ticket["open_blockers"]
                or ticket["state"]["type"] in {"canceled", "duplicate"}
                or route["linear_label"] not in labels
            ):
                return "blocked"
            user_id = route["linear_resolver_user_id"]
            for row in ticket["comments"]["nodes"]:
                if user_id and (row.get("user") or {}).get("id") == user_id:
                    for status in ("blocked", "unrepaired"):
                        if (row.get("body") or "").startswith(RESULT.format(status)):
                            return status
            return None
        comments = self.gh.pages(endpoint + "/comments?per_page=100")
        return next(
            (
                status
                for status in ("blocked", "unrepaired")
                if any(
                    login(c.get("user")) == route["resolver_author"]
                    and (c.get("body") or "").startswith(RESULT.format(status) + "\n")
                    for c in comments
                )
            ),
            None,
        )

    def advance(self, repo, number, *, dispatch):
        route = self.routes[repo.casefold()]
        endpoint = f"repos/{route['repo']}/issues/{number}"
        key = endpoint.casefold()
        issue = self.gh.api(endpoint)
        if (
            issue.get("html_url", "").casefold()
            != f"https://github.com/{route['repo']}/issues/{number}".casefold()
        ):
            return "issue_identity_changed"
        labels = label_names(issue)
        attempt = self.state.get(key)
        customer_claim = (
            self.state_path.parent
            / "customer-attempts"
            / repo.casefold()
            / f"{number}.json"
        )
        other_owner = None
        if native_claimed(self.state_path.parent, issue["html_url"]):
            other_owner = "native"
        elif CUSTOMER in labels or customer_claim.is_file():
            other_owner = "customer"
        if other_owner:
            if not attempt:
                return (
                    "ownership_conflict"
                    if other_owner == "customer"
                    and CUSTOMER in labels
                    and FACTORY in labels
                    else other_owner + "_owned"
                )
            if not self.dry_run:
                self.gh.write_comment(
                    endpoint,
                    self.actor,
                    STATUS
                    + f"\n\nA Factory claim exists and the issue also has a {other_owner} owner. "
                    "Reconcile the existing work and select one owner.\n\n**Next actor:** Maintainer.",
                )
            return "ownership_conflict"
        if "pull_request" in issue:
            return "not_enrolled"
        if not {EXTERNAL, FACTORY} <= labels:
            if not attempt:
                return "not_enrolled"
            if not self.dry_run:
                self.gh.write_comment(
                    endpoint,
                    self.actor,
                    STATUS
                    + "\n\nFactory enrollment was removed during a recorded attempt. "
                    "Reconcile the existing work before selecting an owner.\n\n**Next actor:** Maintainer.",
                )
            return "enrollment_removed"
        timeline = self.gh.pages(endpoint + "/timeline?per_page=100")
        enrollment = latest_label(timeline, FACTORY)
        if (
            enrollment.get("event") != "labeled"
            or login(enrollment.get("actor")) not in self.enrollers
        ):
            return "untrusted_enrollment"
        if on_hold(self.config, labels - {BLOCKED}):
            return "maintainer_hold"
        if BLOCKED in labels:
            hold = latest_label(timeline, BLOCKED)
            if (
                hold.get("event") == "labeled"
                and login(hold.get("actor")) != self.actor
            ):
                return "maintainer_hold"
        linked = self.linked_pulls(route, number, timeline)
        if not attempt:
            if (
                issue["state"] != "open"
                or READY not in labels
                or {ACTIVE, BLOCKED} & labels
            ):
                return "ineligible"
            if issue.get("assignees") or (
                route["mode"] == "assignment"
                and latest_assignment(timeline, route["assignee"])
            ):
                return "previous_assignment"
            if self.dry_run:
                return (
                    "existing_pull_request"
                    if linked or self.find_pulls(route, number)
                    else "eligible"
                )
            if not dispatch:
                return "deferred"
            attempt = self.state[key] = {
                "status": "filing" if route["mode"] == "linear" else "assigning",
                "started": self.now,
                "requests": {},
                "route": route,
                "dispatcher": self.actor,
            }
            self.save()
        elif self.dry_run:
            return attempt["status"]

        def land(status, detail=""):
            return self.transition(endpoint, issue, attempt, status, detail)

        if attempt["route"] != route or attempt["dispatcher"] != self.actor:
            return "configuration_changed"
        if (
            attempt.get("pr") is None
            and attempt["status"] in TERMINAL
            and not attempt.get("dispatched")
        ):
            return land(attempt["status"], attempt.get("detail", ""))
        if issue["state"] != "open" and attempt["status"] in {"assigning", "filing"}:
            return land("closed")
        if (
            attempt["status"] in {"assigning", "filing"}
            and "dispatch_intent" not in attempt
        ):
            existing = set(linked) | {
                pr["number"] for pr in self.find_pulls(route, number)
            }
            if existing:
                return land("blocked", self.competing_detail(route, existing))
        if attempt["status"] == "filing" and issue.get("assignees"):
            return land("blocked", "The issue already has an implementation owner.")
        if attempt["status"] == "assigning":
            return self.assign(endpoint, issue, attempt, route)
        ticket = None
        if route["mode"] == "linear":
            canonical = f"https://github.com/{route['repo']}/issues/{number}"
            if attempt["status"] == "filing":
                self.publish(endpoint, issue, attempt)
                attempt["dispatch_intent"] = True
                self.save()
                ticket = self.linear.ensure(route, canonical)
                attempt["ticket"] = {
                    name: ticket[name] for name in ("id", "identifier", "url")
                }
                issue = self.gh.api(endpoint)
                return land("implementing")
            ticket = self.linear.ticket(attempt["ticket"]["id"], route)
            if ticket is None:
                return land(
                    "blocked",
                    "The associated implementation ticket is unavailable.",
                )
            if (
                not matches_target(ticket, route, canonical)
                or ticket["team"]["key"] != route["team_key"]
                or any(
                    ticket[name] != attempt["ticket"][name]
                    for name in ("id", "identifier")
                )
            ):
                return land("blocked", "The implementation ticket identity changed.")

        pulls = self.find_pulls(route, number)
        repo_info = self.gh.api(f"repos/{route['repo']}")
        route = route | {"base_ref": repo_info["default_branch"]}
        matches = [pr for pr in pulls if self.valid_pr(pr, route, number, ticket)]
        candidates = {pr["number"] for pr in pulls} | set(linked)
        if (
            len(candidates) > 1
            or (pulls and len(matches) != 1)
            or (linked and not pulls)
        ):
            return land("blocked", self.competing_detail(route, candidates))
        if not attempt.get("pr"):
            if not matches:
                if issue["state"] != "open":
                    return land("closed")
                outcome = self.resolver_outcome(endpoint, route, ticket)
                if outcome:
                    return land(
                        outcome,
                        "The configured implementation adapter recorded this outcome.",
                    )
                if self.now - attempt["started"] >= self.timeout:
                    return land(
                        "blocked",
                        "No resolver pull request appeared before the handoff deadline.",
                    )
                return land("implementing")
            attempt["pr"] = matches[0]["number"]
            self.save()

        return self.review(route, number, ticket, issue, attempt, land)

    def review(self, route, number, ticket, issue, attempt, land):
        pr_endpoint = f"repos/{route['repo']}/pulls/{attempt['pr']}"
        pr = self.gh.api(pr_endpoint)
        if not self.valid_pr(pr, route, number, ticket):
            return land(
                "blocked",
                "The linked pull request no longer matches the resolver and issue.",
            )
        head = pr["head"]["sha"]
        attempt["draft"] = pr["draft"]
        if attempt.get("head") != head:
            attempt.update(head=head, head_seen=self.now)
            self.save()
        if pr.get("merged"):
            return land("merged")
        if pr["state"] != "open" or issue["state"] != "open":
            return land("closed")
        if ticket and self.resolver_outcome("", route, ticket):
            return land(
                "blocked",
                "The implementation ticket needs maintainer attention before review.",
            )
        if route["scopes"] != ["*"]:
            files = self.gh.pages(pr_endpoint + "/files?per_page=100")
            if (
                not files
                or len(files) >= 3000
                or any(
                    not any(
                        fnmatch.fnmatchcase(path, pattern)
                        for pattern in route["scopes"]
                    )
                    for file in files
                    for path in (file["filename"], file.get("previous_filename"))
                    if path
                )
            ):
                return land(
                    "blocked",
                    "The changes exceed the reviewer’s configured path scope.",
                )
        checks = self.gh.checks(route["repo"], head, route["checks"])
        if checks != "green":
            if checks == "failed":
                return land(
                    "revision-needed",
                    "At least one configured check failed on this revision.",
                )
            if self.now - attempt["head_seen"] >= self.timeout:
                return land(
                    "blocked", "The configured checks have not passed on this revision."
                )
            return land("checks")

        reviews = self.gh.pages(pr_endpoint + "/reviews?per_page=100")
        verdict = next(
            (
                decision
                for r in reversed(reviews)
                if r.get("commit_id") == head
                and login(r.get("user")) == route["reviewer"]
                and (decision := review_decision(r))
            ),
            None,
        )
        pr = self.gh.api(pr_endpoint)
        if pr.get("merged"):
            return land("merged")
        if pr["state"] != "open":
            return land("closed")
        if pr["head"]["sha"] != head:
            attempt.update(
                head=pr["head"]["sha"], head_seen=self.now, draft=pr["draft"]
            )
            return land(
                "checks",
                "A new revision arrived while the previous one was being checked.",
            )
        if not self.valid_pr(pr, route, number, ticket):
            return land(
                "blocked",
                "The linked pull request no longer matches the resolver and issue.",
            )
        attempt["draft"] = pr["draft"]
        if verdict == "APPROVED":
            checks = ", ".join(route["checks"])
            action = (
                "inspect the behavior evidence and mark the draft ready"
                if pr["draft"]
                else "inspect the behavior evidence and decide whether to merge"
            )
            return land(
                "accepted",
                f"Configured checks passed ({checks}). @{route['reviewer']} approved the code. "
                f"The maintainer should {action}.",
            )
        if verdict == "INCONCLUSIVE":
            return land(
                "incomplete",
                "Inspect the independent review and provide the missing evidence.",
            )
        if verdict == "CHANGES_REQUESTED":
            return land(
                "revision-needed",
                "Read the independent review on the linked pull request, then push a revision there.",
            )
        if verdict == "DISMISSED":
            return land(
                "blocked",
                "The independent review was dismissed. A maintainer can request a fresh review.",
            )
        if head not in attempt["requests"]:
            attempt["requests"][head] = self.now
            self.save()
            if route["reviewer"] not in set(
                map(login, pr.get("requested_reviewers", []))
            ):
                self.gh.api(
                    pr_endpoint + "/requested_reviewers",
                    "POST",
                    ["reviewers[]=" + route["reviewer"]],
                )
        if self.now - attempt["requests"][head] >= self.timeout:
            return land(
                "blocked",
                "No independent verdict arrived. Check the review request before requesting it again.",
            )
        return land("reviewing")

    @staticmethod
    def valid_pr(pr, route, number, ticket=None):
        head, base = pr.get("head") or {}, pr.get("base") or {}
        repo = route["repo"].casefold()
        body = pr.get("body") or ""
        if not (
            login(pr.get("user")) == route["resolver_author"]
            and (head.get("repo") or {}).get("full_name", "").casefold() == repo
            and (base.get("repo") or {}).get("full_name", "").casefold() == repo
            and base.get("ref") == route["base_ref"]
            and SHA.fullmatch(head.get("sha", ""))
        ):
            return False
        if not ticket:
            return head.get("ref") == f"openhands/issue-{number}" and bool(
                re.search(rf"(?mi)^Closes #{number}\s*$", body)
            )
        canonical = re.escape(f"https://github.com/{repo}/issues/{number}")
        text = (pr.get("title") or "") + "\n" + body
        return (
            head.get("ref") == f"openhands/factory-issue-{number}"
            and "linear.app/" not in text.casefold()
            and not re.search(rf"\b{re.escape(ticket['identifier'])}\b", text, re.I)
            and bool(
                re.search(
                    rf"(?mi)^Refs #{number}\s*$|(?<![\w/]){canonical}(?=[\s)>#]|$)",
                    body,
                )
            )
        )

    def run(self, issue_url=None):
        if login(self.gh.api("user")) != self.actor:
            raise ValueError(
                "The current GitHub credential is not the configured dispatcher"
            )
        targets = {}
        if issue_url:
            match = ISSUE.fullmatch(issue_url)
            if not match or match[1].casefold() not in self.routes:
                raise ValueError("The issue must be in a configured GitHub repository")
            targets[(match[1].casefold(), int(match[2]))] = (0, "")
        else:
            for repository, route in self.routes.items():
                for row in self.gh.pages(
                    f"repos/{route['repo']}/issues?state=open&labels={FACTORY}&per_page=100"
                ):
                    if "pull_request" not in row:
                        labels = label_names(row)
                        rank = next(
                            (i for i, name in enumerate(PRIORITIES) if name in labels),
                            len(PRIORITIES),
                        )
                        targets[(repository, row["number"])] = (
                            rank,
                            row.get("created_at", ""),
                        )
            for key in self.state:
                repo, number = key.removeprefix("repos/").split("/issues/")
                if repo in self.routes:
                    targets[(repo, int(number))] = (-1, "")
        statuses, count = {}, 0
        for repo, number in sorted(targets, key=lambda key: (targets[key], key)):
            key = f"repos/{repo}/issues/{number}"
            known = key in self.state
            try:
                statuses[key] = self.advance(
                    repo, number, dispatch=count < self.maximum
                )
            except (OSError, subprocess.SubprocessError, ValueError):
                statuses[key] = "readback_or_write_failed"
            if not known and key in self.state:
                count += 1
        return {"status": "ok", "issues": statuses}


def run(config, *, issue_url=None, dry_run=False, github=None, linear=None, now=None):
    opts = options(config)
    if opts is None:
        return {"status": "disabled", "issues": {}}
    state_dir = Path(config["state_dir"]).expanduser().resolve()
    if dry_run:
        return Dispatcher(
            opts,
            state_dir / "factory-handoff.json",
            github or GitHub(),
            linear=linear,
            dry_run=True,
            now=now,
        ).run(issue_url)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(state_dir / "weekly.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped_overlap", "issues": {}}
        return Dispatcher(
            opts,
            state_dir / "factory-handoff.json",
            github or GitHub(),
            linear=linear,
            now=now,
        ).run(issue_url)
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile opted-in GitHub Factory handoffs"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(
            os.environ.get(
                "AUTOMATION_CONFIG_PATH",
                Path.home() / ".config/automation-improvement/config.json",
            )
        ),
    )
    parser.add_argument("--issue")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = run(read_json(args.config), issue_url=args.issue, dry_run=args.dry_run)
        print(encode(result))
        failed = "readback_or_write_failed" in result["issues"].values()
    except Exception:
        callback("FAILED")
        raise
    callback("FAILED" if failed else "COMPLETED")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
