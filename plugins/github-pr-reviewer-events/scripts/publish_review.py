"""Validate and verify one native review without retrying uncertain writes."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DISCLOSURE = "_This review was posted by an AI agent (OpenHands)._"
VERDICTS = ("✅ APPROVED", "🔄 CHANGES REQUESTED", "🛑 MAINTAINER DECISION REQUIRED")
STATE_DIRECTORY = Path("/workspace/.openhands-review-publications")


class GitHub:
    def __init__(self, token, base_url="https://api.github.com"):
        self.token = token
        self.base_url = base_url

    def request(self, method, path, body=None):
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urlopen(request, timeout=90) as response:
            return json.load(response)

    def pages(self, path):
        result = []
        for page in range(1, 101):
            batch = self.request("GET", f"{path}?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("Expected a GitHub list")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise RuntimeError("GitHub pagination exceeded limit")


def validate_payload(payload, head):
    if (
        not isinstance(payload, dict)
        or not {"commit_id", "event", "body"} <= payload.keys()
    ):
        raise ValueError("Expected commit_id, event and body")
    if payload.keys() - {"commit_id", "event", "body", "comments"}:
        raise ValueError("Unsupported review payload fields")
    if payload["commit_id"] != head or payload["event"] not in ("APPROVE", "COMMENT"):
        raise ValueError("Review head or event is invalid")
    body = payload["body"]
    if not isinstance(body, str):
        raise ValueError("Review body must be text")
    lines = body.strip().splitlines()
    if (
        len(lines) < 3
        or lines[0] != DISCLOSURE
        or lines[-1] not in VERDICTS
        or not any(line.strip() for line in lines[1:-1])
    ):
        raise ValueError("Review needs disclosure, assessment and final verdict")
    if payload["event"] == "APPROVE" and lines[-1] != VERDICTS[0]:
        raise ValueError("Approval contradicts verdict")
    comments = payload.get("comments", [])
    if not isinstance(comments, list):
        raise ValueError("Review comments must be a list")
    for comment in comments:
        if (
            not isinstance(comment, dict)
            or set(comment) != {"path", "line", "side", "body"}
            or not isinstance(comment["path"], str)
            or not comment["path"].strip()
            or type(comment["line"]) is not int
            or comment["line"] <= 0
            or comment["side"] != "RIGHT"
            or not isinstance(comment["body"], str)
            or not comment["body"].strip()
        ):
            raise ValueError("Invalid inline finding")
    return payload


def check_existing(api, path, head, login):
    reviews = api.pages(path + "/reviews")
    own = [
        r
        for r in reviews
        if (r.get("user") or {}).get("login", "").casefold() == login
        and r.get("commit_id") == head
    ]
    if not own:
        return False
    for review in own:
        if review.get("state") == "PENDING":
            raise RuntimeError(
                f"Review {review.get('id')} is pending; do not replace it"
            )
        try:
            validate_payload(
                {"commit_id": head, "event": "COMMENT", "body": review.get("body")},
                head,
            )
        except ValueError as exc:
            raise RuntimeError(
                f"Review {review.get('id')} is incomplete; operator reconciliation required"
            ) from exc
    latest = max((r.get("submitted_at") or "" for r in own), default="")
    discussion = (
        reviews
        + api.pages(path.replace("/pulls/", "/issues/") + "/comments")
        + api.pages(path + "/comments")
    )
    external = [
        (item.get("submitted_at") or item.get("created_at") or "")
        for item in discussion
        if (item.get("user") or {}).get("login", "").casefold() != login
    ]
    if not latest or max(external, default="") <= latest:
        raise RuntimeError(
            "This head already has a review and no new external discussion"
        )

    return True


def inline_fallback(error, payload):
    if error.code != 422 or not payload.get("comments"):
        return None
    try:
        errors = json.load(error).get("errors", [])
    except (ValueError, AttributeError):
        return None
    if not errors or not all(
        isinstance(e, dict)
        and e.get("resource") == "PullRequestReviewComment"
        and e.get("field") in ("line", "position", "path")
        and e.get("code") == "invalid"
        for e in errors
    ):
        return None
    body, verdict = payload["body"].rstrip().rsplit("\n", 1)
    findings = "\n\n".join(
        f"{c['path']}:{c['line']}: {c['body']}" for c in payload["comments"]
    )
    return {
        "commit_id": payload["commit_id"],
        "event": payload["event"],
        "body": f"{body}\n\n{findings}\n\n{verdict}",
    }


def check_head(api, path, head):
    if api.request("GET", path)["head"]["sha"] != head:
        raise RuntimeError("PR head moved; publish no review")


def payload_path(repository, number, head, delivery):
    identity = json.dumps([repository.casefold(), number, head, delivery])
    return f"/tmp/openhands-review-{hashlib.sha256(identity.encode()).hexdigest()}.json"


def publish(
    payload_file,
    repository,
    number,
    head,
    delivery,
    api,
    state_directory=STATE_DIRECTORY,
):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected owner/repository")
    if not re.fullmatch(r"[a-f0-9]{40}", head) or number <= 0 or not delivery:
        raise ValueError("Invalid publication identity")
    payload = validate_payload(json.loads(Path(payload_file).read_text()), head)
    identity = json.dumps([repository.casefold(), number, head, delivery])
    state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_key = json.dumps([repository.casefold(), number, head])
    prefix = hashlib.sha256(lock_key.encode()).hexdigest()
    journal = state_directory / (
        prefix + "-" + hashlib.sha256(identity.encode()).hexdigest() + ".json"
    )
    lock = state_directory / (prefix + ".lock")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Publication is already in progress for this head"
            ) from exc
        previous = [
            (path, json.loads(path.read_text()))
            for path in state_directory.glob(prefix + "-*.json")
        ]
        for path, record in previous:
            if record.get("status") != "verified":
                raise RuntimeError(
                    f"An earlier publication for this head is unverified (review {record.get('id')}): "
                    f"{record.get('reason', record.get('status'))}; inspect {path}; operator reconciliation required"
                )
        return publish_locked(
            payload, repository, number, head, api, journal, identity, bool(previous)
        )


def publish_locked(payload, repository, number, head, api, journal, identity, previous):
    if journal.exists():
        raise RuntimeError(
            f"Publication already attempted; inspect {journal}; do not repost"
        )
    path = f"/repos/{repository}/pulls/{number}"
    login = api.request("GET", "/user")["login"].casefold()
    pr = api.request("GET", path)
    if pr["head"]["sha"] != head:
        raise RuntimeError("PR head moved; publish no review")
    existing = check_existing(api, path, head, login)
    if previous and not existing:
        raise RuntimeError("Earlier verified review is not visible; do not replace it")
    if (pr.get("user") or {}).get("login", "").casefold() == login:
        payload = {**payload, "event": "COMMENT"}
    # Keep the intent even on timeout, crash or a malformed receipt: no automatic replacement.
    with journal.open("x") as stream:
        json.dump({"status": "attempting", "identity": json.loads(identity)}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    response = None
    try:
        check_head(api, path, head)
        try:
            response = api.request("POST", path + "/reviews", payload)
        except HTTPError as exc:
            fallback = inline_fallback(exc, payload)
            if fallback is None:
                raise
            payload = validate_payload(fallback, head)
            check_head(api, path, head)
            response = api.request("POST", path + "/reviews", payload)
        expected_state = "APPROVED" if payload["event"] == "APPROVE" else "COMMENTED"
        if (
            not isinstance(response, dict)
            or not response.get("id")
            or response.get("body") != payload["body"]
            or response.get("commit_id") != head
            or response.get("state") != expected_state
            or (response.get("user") or {}).get("login", "").casefold() != login
        ):
            review_id = response.get("id") if isinstance(response, dict) else None
            raise RuntimeError(
                f"Accepted review {review_id} failed verification; do not repost"
            )
    except Exception as exc:
        review_id = response.get("id") if isinstance(response, dict) else None
        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        with journal.open("w") as stream:
            json.dump(
                {
                    "status": "unverified",
                    "id": review_id,
                    "reason": reason,
                    "identity": json.loads(identity),
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        raise RuntimeError(
            f"Publication unverified (review {review_id}): {reason}; inspect {journal}; operator action required; do not repost"
        ) from exc
    with journal.open("w") as stream:
        json.dump(
            {
                "status": "verified",
                "id": response["id"],
                "identity": json.loads(identity),
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--number", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--delivery", required=True)
    parser.add_argument("--token-env", required=True)
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    try:
        publish(
            args.payload,
            args.repository,
            args.number,
            args.head,
            args.delivery,
            GitHub(os.environ[args.token_env]),
        )
    except Exception as exc:
        print(f"Review not verified: {exc}", file=sys.stderr)
        return 1
    print("GITHUB_REVIEW_POSTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
