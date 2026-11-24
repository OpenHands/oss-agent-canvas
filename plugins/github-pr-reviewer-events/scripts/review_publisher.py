"""Validate and submit the event reviewer's staged GitHub review."""

import argparse
import http.client
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

GITHUB_API = "https://api.github.com"
DISCLOSURE = "_This review was posted by an AI agent (OpenHands)._"
APPROVED = "✅ APPROVED"
CHANGES_REQUESTED = "🔄 CHANGES REQUESTED"
MAINTAINER_DECISION_VERDICT = "🛑 MAINTAINER DECISION REQUIRED"
EXPECTED_STATE = {"APPROVE": "APPROVED", "COMMENT": "COMMENTED"}


class PublicationError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_payload(stage, head):
    try:
        payload = json.loads(stage.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PublicationError("Staged review is missing, unreadable, or not valid JSON.") from None
    if not isinstance(payload, dict) or payload.keys() - {"body", "event", "commit_id", "comments"}:
        raise PublicationError("Stage a review object with body, event, commit_id and optional comments.")
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise PublicationError("The staged review body is missing or empty.")
    if payload.get("commit_id") != head:
        raise PublicationError("The staged commit_id does not match the reviewed head.")
    event = payload.get("event")
    if event not in ("APPROVE", "COMMENT"):
        raise PublicationError("The staged event must be APPROVE or COMMENT.")
    lines = body.strip().splitlines()
    if len(lines) < 3 or lines[0] != DISCLOSURE or not any(line.strip() for line in lines[1:-1]):
        raise PublicationError("The body must begin with the AI disclosure and include an assessment and verdict.")
    verdicts = (APPROVED,) if event == "APPROVE" else (APPROVED, CHANGES_REQUESTED, MAINTAINER_DECISION_VERDICT)
    if lines[-1] not in verdicts:
        raise PublicationError("Put a verdict compatible with the review event on the final line.")
    if "comments" in payload and not isinstance(payload["comments"], list):
        raise PublicationError("Inline comments must be an array.")
    return payload


def publish(stage, receipt, repository, number, head):
    if (
        not re.fullmatch(r"[\w.-]+/[\w.-]+", repository, flags=re.ASCII)
        or number <= 0
        or not re.fullmatch(r"[a-f0-9]{40}", head)
    ):
        raise PublicationError("Invalid pull request target.")
    payload = _read_payload(stage, head)
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise PublicationError("The configured GitHub credential is unavailable.")
    try:
        data = json.dumps(payload, allow_nan=False).encode()
    except ValueError:
        raise PublicationError("Staged review is not valid JSON.") from None
    request = Request(
        f"{GITHUB_API}/repos/{repository}/pulls/{number}/reviews",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    # Reserve the delivery before the POST: a timeout is not proof of failure.
    try:
        receipt.touch(exist_ok=False)
    except FileExistsError:
        raise PublicationError("A review submission was already attempted for this delivery. Do not retry.") from None
    except OSError:
        raise PublicationError("Could not reserve the review submission. No request was sent.") from None
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            review = json.load(response)
    except HTTPError as exc:
        if exc.code == 422:
            receipt.unlink()
            raise PublicationError("GitHub rejected the review (HTTP 422); only retry after correcting a known rejection.") from None
        raise PublicationError(f"Publication could not be verified (HTTP {exc.code}). Do not retry.") from None
    except (URLError, OSError, http.client.HTTPException, ValueError):
        raise PublicationError("Publication could not be verified. Do not retry.") from None
    review = review if isinstance(review, dict) else {}
    review_id = review.get("id")
    if (
        type(review_id) is not int or review_id <= 0
        or review.get("body") != payload["body"]
        or review.get("commit_id") != head
        or review.get("state") != EXPECTED_STATE[payload["event"]]
    ):
        identifier = f" (review {review_id})" if type(review_id) is int else ""
        raise PublicationError(f"GitHub returned an unverified review{identifier}. Do not retry.")
    return review_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repository")
    parser.add_argument("number", type=int)
    parser.add_argument("head")
    args = parser.parse_args()
    script = Path(__file__)
    try:
        review_id = publish(script.with_suffix(".json"), script.with_suffix(".receipt"), **vars(args))
    except PublicationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Verified GitHub review {review_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
