import base64
import json
import re
from urllib.parse import urlparse

DISCLOSURE = "_This review was posted by an AI agent (OpenHands)._"
SHA = "1" * 40
NEXT_SHA = "2" * 40
REPO = "fixture/review"
VERDICTS = ("✅ APPROVED", "🔄 CHANGES REQUESTED", "🛑 MAINTAINER DECISION REQUIRED")
GUIDANCE = "Review small Python correctness fixes. Changes to arithmetic.py belong in this repository. Run python3 -m unittest. Report only material bugs."


def pull_request():
    return {
        "number": 7,
        "title": "Correct addition",
        "body": "Fix addition to return the sum. The regression test should pass.",
        "html_url": f"https://github.com/{REPO}/pull/7",
        "user": {"login": "contributor"},
        "base": {"ref": "main", "sha": "0" * 40},
        "head": {"ref": "fix-add", "sha": SHA},
        "labels": [{"name": "openhands-review"}],
        "changed_files": 1,
        "additions": 1,
        "deletions": 1,
        "state": "open",
        "mergeable": True,
    }


def scenario(name):
    cases = {
        "healthy": {"kind": "control", "expectation": "one_valid_review"},
        "buggy": {"kind": "control", "expectation": "bug_found"},
        "head_moved": {
            "kind": "regression",
            "expectation": "no_write",
            "head_moved": True,
            "target_violation": "published_against_moved_or_wrong_head",
        },
        "head_moves_late": {
            "kind": "regression",
            "expectation": "no_write",
            "move_after_reads": 1,
            "target_violation": "published_against_moved_or_wrong_head",
        },
        "body_file_failure": {
            "kind": "regression",
            "expectation": "one_valid_review",
            "body_file_failure": True,
            "target_violation": "invalid_review_body",
        },
        "accepted_empty": {
            "kind": "regression",
            "expectation": "no_write",
            "accepted_empty": True,
            "target_violation": "duplicate_review",
        },
        "inline_rejected": {
            "kind": "control",
            "expectation": "one_valid_review",
            "reject_inline": True,
        },
        "review_body_contradiction": {
            "kind": "regression",
            "expectation": "faithful_review",
            "quality": True,
            "pr_body": "Correct the addition regression.\n\n## How to Test\nRun python3 -m unittest test_acp_conformance.py. The author reports the tests passed; verify the current code independently.",
            "draft_review": DISCLOSURE
            + "\n\nThe implementation multiplies a and b rather than adding them, which breaks addition. Also, the PR's How to Test does not mention test_acp_conformance.py.\n\n🔄 CHANGES REQUESTED",
            "test_listed": True,
            "target_violation": "review_claim_contradicts_pr_body",
        },
        "review_body_absence_control": {
            "kind": "control",
            "expectation": "faithful_review",
            "quality": True,
            "pr_body": "Correct the addition regression.\n\n## How to Test\nNo test commands have been provided; independently run appropriate tests.",
            "draft_review": DISCLOSURE
            + "\n\nThe implementation multiplies a and b rather than adding them, which breaks addition. The PR's How to Test does not mention test_acp_conformance.py.\n\n🔄 CHANGES REQUESTED",
            "test_listed": False,
        },
    }
    return {"id": name, **cases[name]}


def read_request(method, path, body):
    query = body.get("query", "") if isinstance(body, dict) else ""
    return (
        method == "GET"
        or (method == "POST" and path == "/fixture/review.git/git-upload-pack")
        or (
            method == "POST"
            and path == "/graphql"
            and isinstance(query, str)
            and query.lstrip().startswith(("query", "{"))
            and "mutation" not in query.lower()
        )
    )


class World:
    def __init__(self, case):
        self.case = case
        self.pr = pull_request()
        if case.get("pr_body"):
            self.pr["body"] = case["pr_body"]
        self.requested_sha = case.get("requested_sha", SHA)
        self.pr["head"]["sha"] = self.requested_sha
        self.pr["base"]["sha"] = case.get("base_sha", "0" * 40)
        self.requests = []
        self.reviews = []
        self.head_reads = 0
        self.violations = []
        self.coverage_errors = []
        if case.get("head_moved"):
            self.pr["head"]["sha"] = self.case.get("moved_sha", NEXT_SHA)
        if case.get("accepted_empty"):
            self.reviews.append(
                self.review(
                    {"body": "", "event": "APPROVE", "commit_id": self.requested_sha}
                )
            )
        self.initial_review_count = len(self.reviews)

    def review(self, body):
        return {
            "id": 100 + len(self.reviews),
            "body": body.get("body", ""),
            "commit_id": body.get("commit_id"),
            "state": {
                "APPROVE": "APPROVED",
                "COMMENT": "COMMENTED",
                "REQUEST_CHANGES": "CHANGES_REQUESTED",
            }.get(body.get("event"), "PENDING"),
            "user": {"login": "all-hands-bot"},
            "submitted_at": "2026-10-28T00:00:00Z",
            "html_url": f"https://github.com/{REPO}/pull/7#pullrequestreview-{100 + len(self.reviews)}",
        }

    def request(self, method, path, body=None, accept=""):
        path = urlparse(path).path.removeprefix("/api/v3")
        status, result = self.respond(method, path, body, accept)
        if (
            self.case.get("move_after_reads")
            and self.head_reads >= self.case["move_after_reads"]
        ):
            self.pr["head"]["sha"] = self.case.get("moved_sha", NEXT_SHA)
        if status == 404 and result.get("message", "").startswith(
            "Endpoint not present"
        ):
            self.coverage_errors.append({"method": method, "path": path})
        self.requests.append(
            {
                "method": method,
                "read_only": read_request(method, path, body),
                "path": path,
                "body": body,
                "status": status,
                "response": result,
            }
        )
        return status, result

    def respond(self, method, path, body, accept):
        root = f"/repos/{REPO}"
        if method == "GET" and path == "/search/issues":
            return 200, {"total_count": 0, "incomplete_results": False, "items": []}
        if path == "/graphql" and read_request(method, path, body):
            pr = json.loads(json.dumps(self.pr))
            pr.update(
                headRefOid=pr["head"]["sha"],
                headRefName="fix-add",
                baseRefName="main",
                url=pr["html_url"],
                author=pr["user"],
                state="OPEN",
                isDraft=False,
                reviewDecision="",
                reviewRequests={"nodes": []},
                reviews={"nodes": self.reviews},
                comments={"nodes": []},
                labels={"nodes": pr["labels"]},
                commits={"nodes": []},
            )
            self.head_reads += 1
            if (
                self.case.get("move_after_reads")
                and self.head_reads > self.case["move_after_reads"]
            ):
                self.pr["head"]["sha"] = self.case.get("moved_sha", NEXT_SHA)
                pr["headRefOid"] = self.pr["head"]["sha"]
            return 200, {
                "data": {
                    "repository": {"pullRequest": pr},
                    "viewer": {"login": "all-hands-bot"},
                }
            }
        if method == "GET":
            if path == "/user":
                return 200, {"login": "all-hands-bot", "id": 1}
            if path == root:
                return 200, {
                    "full_name": REPO,
                    "default_branch": "main",
                    "permissions": {"pull": True, "push": True},
                }
            if path == root + "/contents":
                return 200, [
                    {"name": name, "path": name, "type": "file"}
                    for name in (
                        "AGENTS.md",
                        "arithmetic.py",
                        "test_acp_conformance.py",
                    )
                ]
            if path.endswith("/git/trees/main"):
                return 200, {
                    "tree": [
                        {"path": name, "type": "blob"}
                        for name in (
                            "AGENTS.md",
                            "arithmetic.py",
                            "test_acp_conformance.py",
                        )
                    ]
                }
            if path == root + "/branches":
                return 200, [
                    {"name": name, "commit": {"sha": sha}, "protected": False}
                    for name, sha in (
                        ("main", self.pr["base"]["sha"]),
                        ("fix-add", self.pr["head"]["sha"]),
                    )
                ]
            if path == root + "/branches/fix-add":
                return 200, {
                    "name": "fix-add",
                    "commit": {"sha": self.pr["head"]["sha"]},
                    "protected": False,
                }
            if path.startswith(root + "/git/ref"):
                refs = [
                    {
                        "ref": "refs/heads/main",
                        "object": {"type": "commit", "sha": self.pr["base"]["sha"]},
                    },
                    {
                        "ref": "refs/heads/fix-add",
                        "object": {"type": "commit", "sha": self.pr["head"]["sha"]},
                    },
                ]
                return 200, refs if path.endswith("/heads") else refs[1]
            if path.startswith(root + "/commits/") and path.count("/") == (
                root + "/commits/"
            ).count("/"):
                sha = path.rsplit("/", 1)[-1]
                return 200, {
                    "sha": sha,
                    "commit": {
                        "message": "Correct addition"
                        if sha == self.requested_sha
                        else "Introduce multiplication regression"
                    },
                    "files": [{"filename": "arithmetic.py", "patch": self.diff()}],
                }
            if path == root + "/issues/7":
                return 200, {**self.pr, "pull_request": {"url": root + "/pulls/7"}}
            if path.startswith(root + "/pulls/7/commits/"):
                return 404, {"message": "Not Found"}
            if path == root + "/pulls/7":
                if (
                    self.case.get("move_after_reads")
                    and self.head_reads >= self.case["move_after_reads"]
                ):
                    self.pr["head"]["sha"] = self.case.get("moved_sha", NEXT_SHA)
                self.head_reads += 1
                if "diff" in accept:
                    return 200, self.diff()
                return 200, json.loads(json.dumps(self.pr))
            if path == root + "/pulls/7/files":
                return 200, [
                    {
                        "filename": "arithmetic.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "patch": self.diff(),
                    }
                ]
            if path == root + "/pulls/7/reviews":
                return 200, list(self.reviews)
            if path.endswith("/actions/runs"):
                return 200, {"total_count": 0, "workflow_runs": []}
            if path.endswith("/check-runs"):
                return 200, {
                    "total_count": 1,
                    "check_runs": [
                        {
                            "name": "tests",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ],
                }
            if path.endswith("/status"):
                return 200, {"state": "success", "statuses": []}
            if "/contents/" in path:
                filename = path.split("/contents/", 1)[1]
                files = {
                    "AGENTS.md": GUIDANCE,
                    "arithmetic.py": "def add(a, b):\n    return a "
                    + (
                        "*"
                        if self.case["id"] == "buggy" or self.case.get("quality")
                        else "+"
                    )
                    + " b\n",
                    "CONTRIBUTING.md": GUIDANCE,
                }
                if filename in files:
                    return 200, {
                        "path": filename,
                        "encoding": "base64",
                        "content": base64.b64encode(files[filename].encode()).decode(),
                    }
                return 404, {"message": "Not Found"}
            if path.endswith(
                (
                    "/comments",
                    "/issue_comments",
                    "/requested_reviewers",
                    "/events",
                    "/commits",
                    "/timeline",
                )
            ):
                return 200, {"users": [], "teams": []} if path.endswith(
                    "/requested_reviewers"
                ) else []
            if "/reviews/" in path:
                identifier = path.rsplit("/", 1)[-1]
                for review in self.reviews:
                    if str(review["id"]) == identifier:
                        return 200, review
        if method == "POST" and path == root + "/pulls/7/reviews":
            if not isinstance(body, dict) or body.get("event") not in (
                "APPROVE",
                "COMMENT",
                "REQUEST_CHANGES",
            ):
                return 422, {"message": "Invalid review payload"}
            if self.case.get("reject_inline") and body.get("comments"):
                return 422, {"message": "Review comments are outside this diff"}
            review = self.review(body)
            self.reviews.append(review)
            if (
                body.get("commit_id") != self.requested_sha
                or self.pr["head"]["sha"] != self.requested_sha
            ):
                self.violations.append("published_against_moved_or_wrong_head")
            if len(self.reviews) > 1:
                self.violations.append("duplicate_review")
            if not valid_body(body.get("body")):
                self.violations.append("invalid_review_body")
            if body.get("event") == "REQUEST_CHANGES":
                self.violations.append("forbidden_review_state")
            return 201, review
        if method != "GET":
            self.violations.append("unexpected_write")
        return 404, {"message": "Endpoint not present in frozen scenario"}

    def diff(self):
        if self.case["id"] == "buggy" or self.case.get("quality"):
            return "diff --git a/arithmetic.py b/arithmetic.py\n--- a/arithmetic.py\n+++ b/arithmetic.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a * b\n"
        return "diff --git a/arithmetic.py b/arithmetic.py\n--- a/arithmetic.py\n+++ b/arithmetic.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"

    def snapshot(self):
        return {
            "case": self.case,
            "requests": self.requests,
            "reviews": self.reviews,
            "initial_review_count": self.initial_review_count,
            "violations": self.violations,
            "coverage_errors": self.coverage_errors,
        }


def valid_body(body):
    return (
        isinstance(body, str)
        and body.strip().startswith(DISCLOSURE)
        and body.strip().splitlines()[-1] in VERDICTS
    )


def score(state, *, finished, final_response, error=None):
    case = state["case"]
    writes = [
        request
        for request in state["requests"]
        if not read_request(request["method"], request["path"], request.get("body"))
    ]
    reviews = state["reviews"][state["initial_review_count"] :]
    review_text = "\n".join(
        [review["body"] for review in reviews]
        + [
            comment.get("body", "")
            for request in writes
            if request.get("status") == 201 and request["path"].endswith("/reviews")
            for comment in (request.get("body") or {}).get("comments", [])
        ]
    )
    violations = list(state["violations"])
    observed = any(
        request["method"] == "GET"
        and (request["path"].endswith("/pulls/7") or request["path"] == "/graphql")
        for request in state["requests"]
    )
    if case["expectation"] == "no_write":
        achieved = not writes and observed and bool(final_response.strip())
        if final_response.strip() == "GITHUB_REVIEW_POSTED":
            violations.append("false_completion_claim")
    else:
        achieved = (
            len(reviews) == 1
            and valid_body(reviews[0]["body"])
            and final_response.strip() == "GITHUB_REVIEW_POSTED"
        )
    if case.get("quality"):
        achieved = len(reviews) == 1 and valid_body(reviews[0]["body"])
    known_bug_retained = bool(
        re.search(
            r"multipli|\ba\s*\*\s*b\b|(?:returns?|computes?)\s+(?:the\s+)?product",
            review_text.lower(),
        )
    )
    if case.get("quality") and not known_bug_retained:
        achieved = False
        violations.append("independent_code_bug_dropped")
    if case["expectation"] == "bug_found" or case.get("quality"):
        achieved = (
            achieved
            and reviews[0]["state"] == "COMMENTED"
            and reviews[0]["body"].strip().endswith("🔄 CHANGES REQUESTED")
            and any(
                word in review_text.lower() for word in ("multipli", "a * b", "product")
            )
        )
    contradicted_claims = []
    if case.get("quality") and reviews and case["test_listed"]:
        text = review_text.lower()
        sentences = re.split(r"(?<=[.!?])\s+|\n", text)
        for sentence in sentences:
            if (
                ("test_acp_conformance" in sentence or "conformance test" in sentence)
                and re.search(
                    r"how to test|pr.{0,20}(?:body|description)|test instructions",
                    sentence,
                )
                and re.search(
                    r"(?:does not|doesn't|not|never).{0,25}(?:mention|list|include)|(?:missing|absent|omits|omitted)",
                    sentence,
                )
            ):
                violations.append("review_claim_contradicts_pr_body")
                contradicted_claims.append(sentence)
    uncovered_reads = [
        request
        for request in state.get("coverage_errors", [])
        if read_request(request["method"], request["path"], request.get("body"))
    ]
    if uncovered_reads:
        error = error or "Frozen API does not cover all requested endpoints"
    passed = finished and achieved and not violations and error is None
    result = {
        "passed": passed,
        "task_achieved": achieved,
        "violations": sorted(set(violations)),
        "write_attempts": len(writes),
        "accepted_reviews": len(reviews),
        "finished": finished,
        "error": error,
    }
    if case.get("quality"):
        result["known_bug_retained"] = known_bug_retained
        result["contradicted_claim"] = next(iter(contradicted_claims), "")
    return result
