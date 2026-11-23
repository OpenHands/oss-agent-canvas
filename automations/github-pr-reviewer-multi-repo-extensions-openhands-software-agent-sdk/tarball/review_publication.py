import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DISCLOSURE = "_This review was posted by an AI agent (OpenHands)._"
VERDICTS = ("✅ APPROVED", "🔄 CHANGES REQUESTED", "🛑 MAINTAINER DECISION REQUIRED")


class PublicationError(Exception):
    pass


class Rejected(PublicationError):
    def __init__(self, status, response):
        super().__init__(f"GitHub rejected the request (HTTP {status}); no success reported")
        self.status = status
        self.response = response


class GitHub:
    def __init__(self, token, origin="https://api.github.com"):
        self.token = token
        self.origin = origin

    def request(self, method, path, body=None):
        request = Request(
            self.origin + path,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            method=method,
        )
        try:
            with urlopen(request, timeout=90) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                response = json.loads(exc.read())
            except (ValueError, UnicodeError):
                response = {}
            raise Rejected(exc.code, response) from None

    def pages(self, path):
        results = []
        for page in range(1, 101):
            values = self.request("GET", f"{path}?per_page=100&page={page}")
            if not isinstance(values, list):
                raise PublicationError("Expected a GitHub list")
            results.extend(values)
            if len(values) < 100:
                return results
        raise PublicationError("GitHub pagination limit reached; publication stopped")


def validate_payload(payload, head):
    if not isinstance(payload, dict) or set(payload) - {"commit_id", "event", "body", "comments"}:
        raise PublicationError("Use only commit_id, event, body and optional comments")
    if payload.get("commit_id") != head or payload.get("event") not in ("APPROVE", "COMMENT"):
        raise PublicationError("Wrong head or unsupported review event")
    body = payload.get("body")
    lines = body.rstrip().splitlines() if isinstance(body, str) else []
    if (len(lines) < 3 or lines[0] != DISCLOSURE or lines[-1] not in VERDICTS
            or not "\n".join(lines[1:-1]).strip()):
        raise PublicationError("Review body needs disclosure, assessment and final verdict")
    if payload["event"] == "APPROVE" and lines[-1] != VERDICTS[0]:
        raise PublicationError("Only the approved verdict may use APPROVE")
    comments = payload.get("comments", [])
    if not isinstance(comments, list):
        raise PublicationError("comments must be an array")
    for comment in comments:
        if (not isinstance(comment, dict)
                or set(comment) - {"path", "line", "side", "body", "start_line", "start_side"}
                or not isinstance(comment.get("path"), str) or not comment["path"].strip()
                or type(comment.get("line")) is not int or comment["line"] < 1
                or comment.get("side") != "RIGHT"
                or not isinstance(comment.get("body"), str) or not comment["body"].strip()):
            raise PublicationError("Invalid inline finding")


def _fallback(payload, rejection):
    if rejection.status != 422 or not isinstance(rejection.response, dict):
        raise rejection
    message = str(rejection.response.get("message", "")).lower()
    if payload["event"] == "APPROVE" and "approve your own pull request" in message:
        return {**payload, "event": "COMMENT"}
    errors = rejection.response.get("errors", [])
    if not payload.get("comments") or not isinstance(errors, list) or not any(
        isinstance(error, dict) and error.get("resource") == "PullRequestReviewComment"
        and error.get("field") in ("line", "position", "path", "side", "start_line", "start_side")
        for error in errors
    ):
        raise rejection
    lines = payload["body"].rstrip().splitlines()
    assessment, verdict = "\n".join(lines[:-1]), lines[-1]
    findings = "\n\n".join(
        f"{comment['path']}:{comment['line']}\n{comment['body']}"
        for comment in payload["comments"]
    )
    return {"commit_id": payload["commit_id"], "event": payload["event"],
            "body": f"{assessment}\n\nInline findings:\n\n{findings}\n\n{verdict}"}


def _check_existing(api, path, head, login):
    reviews = api.pages(path + "/reviews")
    own = [review for review in reviews
           if review.get("commit_id") == head
           and (review.get("user", {}).get("login") or "").lower() == login]
    if any(review.get("state") == "PENDING" for review in own):
        raise PublicationError("A review is already underway on this head")
    if not own:
        return
    for review in own:
        event = "APPROVE" if review.get("state") == "APPROVED" else "COMMENT"
        validate_payload({"commit_id": head, "event": event, "body": review.get("body")}, head)
    latest = max(review.get("submitted_at") or "" for review in own)
    comments = api.pages(path.replace("/pulls/", "/issues/") + "/comments")
    comments += api.pages(path + "/comments")
    other_activity = comments + reviews
    if not any((item.get("user", {}).get("login") or "").lower() != login
               and (item.get("created_at") or item.get("submitted_at") or "") > latest
               for item in other_activity):
        raise PublicationError("This account already reviewed this head; no later clarification")


def _record(stream, record):
    stream.seek(0)
    json.dump(record, stream)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def publish(api, repo, number, head, reviewer, payload, receipt):
    validate_payload(payload, head)
    receipt = Path(receipt)
    receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(receipt, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as journal:
        try:
            fcntl.flock(journal, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublicationError("Publication is already underway") from None
        journal.seek(0)
        if journal.read().strip():
            raise PublicationError("An attempt is already recorded; do not submit another review")
        login = (api.request("GET", "/user").get("login") or "").lower()
        if not login or (reviewer and login != reviewer.lower()):
            raise PublicationError("Unexpected GitHub reviewer account")
        path = f"/repos/{repo}/pulls/{number}"
        _check_existing(api, path, head, login)
        pr = api.request("GET", path)
        if pr.get("state") != "open" or pr.get("head", {}).get("sha") != head:
            raise PublicationError("PR closed or head moved; publish nothing")
        if (pr.get("user", {}).get("login") or "").lower() == login:
            payload = {**payload, "event": "COMMENT"}
        record = {"repository": repo, "number": number, "head": head, "state": "intent",
                  "body_sha256": hashlib.sha256(payload["body"].encode()).hexdigest()}
        # Persist before POST: a crash or timeout must not enable a blind resubmission.
        _record(journal, record)
        try:
            try:
                result = api.request("POST", path + "/reviews", payload)
            except Rejected as exc:
                payload = _fallback(payload, exc)
                validate_payload(payload, head)
                _check_existing(api, path, head, login)
                pr = api.request("GET", path)
                if pr.get("state") != "open" or pr.get("head", {}).get("sha") != head:
                    raise PublicationError("PR closed or head moved before fallback")
                record["body_sha256"] = hashlib.sha256(payload["body"].encode()).hexdigest()
                _record(journal, record)
                result = api.request("POST", path + "/reviews", payload)
            expected_state = "APPROVED" if payload["event"] == "APPROVE" else "COMMENTED"
            if (not result.get("id") or result.get("body") != payload["body"]
                    or result.get("commit_id") != head or result.get("state") != expected_state
                    or (result.get("user", {}).get("login") or "").lower() != login):
                raise PublicationError("Accepted response mismatch; stop, do not replace the review")
            record.update(state="accepted", review_id=result["id"])
            _record(journal, record)
            return result
        except Exception:
            raise PublicationError("Publication failed or acceptance is uncertain; stop without resubmitting") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--number", required=True, type=int)
    parser.add_argument("--head", required=True)
    parser.add_argument("--reviewer", default="")
    parser.add_argument("--secret", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("payload", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.payload.read_text())
        token = os.environ.get(args.secret)
        if not token:
            raise PublicationError("Configured GitHub credential is unavailable")
        publish(GitHub(token), args.repo, args.number, args.head, args.reviewer, payload, args.receipt)
    except Exception:
        raise SystemExit("REVIEW_PUBLICATION_STOPPED: no success reported; preserve the draft and attempt record, do not post a replacement") from None
    print("GITHUB_REVIEW_POSTED")


if __name__ == "__main__":
    main()
