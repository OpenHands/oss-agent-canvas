import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError

DISCLOSURE = "_This review was posted by an AI agent (OpenHands)._"
VERDICTS = {
    "approve": "✅ APPROVED",
    "changes": "🔄 CHANGES REQUESTED",
    "scope_stop": "🛑 MAINTAINER DECISION REQUIRED",
}


def validate_proposal(value):
    if not isinstance(value, dict) or set(value) != {"decision", "summary", "comments"}:
        raise ValueError(
            "Require decision, summary and comments; targets are not agent-controlled"
        )
    if not isinstance(value["decision"], str) or value["decision"] not in {
        *VERDICTS,
        "skip",
    }:
        raise ValueError("Invalid review decision")
    if (
        not isinstance(value["summary"], str)
        or not 1 <= len(value["summary"].strip()) <= 20000
    ):
        raise ValueError("Require a nonempty summary of at most 20000 characters")
    if not isinstance(value["comments"], list) or len(value["comments"]) > 10:
        raise ValueError("Require at most ten comments")
    if value["decision"] != "changes" and value["comments"]:
        raise ValueError("Only changes proposals can include inline findings")
    for comment in value["comments"]:
        if not isinstance(comment, dict) or set(comment) != {"path", "line", "body"}:
            raise ValueError("Inline comments require path, line and body")
        path = comment["path"]
        if (
            not isinstance(path, str)
            or not path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
        ):
            raise ValueError("Invalid comment path")
        if type(comment["line"]) is not int or comment["line"] < 1:
            raise ValueError("Invalid comment line")
        if (
            not isinstance(comment["body"], str)
            or not 1 <= len(comment["body"].strip()) <= 4000
        ):
            raise ValueError("Invalid comment body")
    return json.loads(json.dumps(value))


def write_record(path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class ReviewPublisher:
    def __init__(self, api, repository, number, head, login, delivery, state_dir):
        if (
            not re.fullmatch(r"[\w.-]+/[\w.-]+", repository)
            or type(number) is not int
            or number < 1
            or not re.fullmatch(r"[0-9a-f]{40}", head)
        ):
            raise ValueError("Invalid trusted review target")
        self.api, self.repository, self.number = api, repository, number
        self.head, self.login = head, login.casefold()
        self.root = f"/repos/{repository}/pulls/{number}"
        self.identity = hashlib.sha256(
            f"{repository.casefold()}:{number}:{head}:{delivery}".encode()
        ).hexdigest()
        self.marker = f"<!-- openhands-review-publication: {self.identity} -->"
        self.directory = Path(state_dir)
        self.record = self.directory / (self.identity + ".json")

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        key = hashlib.sha256(
            f"{self.repository.casefold()}:{self.number}".encode()
        ).hexdigest()
        with (self.directory / (key + ".lock")).open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Review publication already in progress") from None
            yield

    def pages(self, path):
        rows = []
        for page in range(1, 101):
            values = self.api("GET", f"{path}?per_page=100&page={page}")
            if not isinstance(values, list):
                raise ValueError("Expected a GitHub list")
            rows.extend(values)
            if len(values) < 100:
                return rows
        raise ValueError("GitHub pagination exceeded its limit")

    def live(self):
        pr = self.api("GET", self.root)
        return (
            pr
            if pr.get("state", "").lower() == "open"
            and pr.get("head", {}).get("sha") == self.head
            else None
        )

    def existing(self):
        reviews = [
            r
            for r in self.pages(self.root + "/reviews")
            if r.get("commit_id") == self.head
            and r.get("user", {}).get("login", "").casefold() == self.login
        ]
        matches = [r for r in reviews if self.marker in (r.get("body") or "")]
        if matches:
            if len(matches) != 1:
                return {
                    "status": "needs_operator",
                    "reason": "Multiple receipts for one publication",
                }
            record = json.loads(self.record.read_text()) if self.record.exists() else {}
            payload = record.get("payload")
            if not payload or not self.confirmed(matches[0], payload):
                return {
                    "status": "needs_operator",
                    "reason": "Existing receipt does not match the reserved payload",
                }
            return {
                "status": "already_published",
                "review_id": matches[0]["id"],
                "final_response": "Existing verified review retained; no new review posted",
            }
        if self.record.exists():
            return {
                "status": "needs_operator",
                "reason": "Publication was reserved; no automatic retry",
            }
        if reviews:
            latest = max(reviews, key=lambda r: r.get("submitted_at") or "")
            comments = self.pages(
                f"/repos/{self.repository}/issues/{self.number}/comments"
            )
            clarified = any(
                c.get("user", {}).get("login", "").casefold() not in ("", self.login)
                and (c.get("created_at") or "") > (latest.get("submitted_at") or "")
                for c in comments
            )
            if not clarified:
                body = latest.get("body") or ""
                valid = body.startswith(DISCLOSURE) and body.rstrip().splitlines()[
                    -1:
                ] in ([v] for v in VERDICTS.values())
                return {
                    "status": "already_reviewed" if valid else "needs_operator",
                    "reason": "An accepted review already exists on this head; no replacement posted",
                }
        return None

    def precheck(self):
        if self.live() is None:
            return {
                "status": "head_changed",
                "reason": "The requested head is no longer open/current",
            }
        return self.existing()

    def payload(self, proposal, pr, *, inline=True):
        comments, extra = [], []
        files = (
            self.pages(self.root + "/files") if proposal["comments"] and inline else []
        )
        positions = {}
        for file in files:
            line = None
            allowed = set()
            for text in (file.get("patch") or "").splitlines():
                match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", text)
                if match:
                    line = int(match[1])
                elif line is not None and text.startswith(("+", " ")):
                    allowed.add(line)
                    line += 1
            positions[file["filename"]] = allowed
        for comment in proposal["comments"]:
            if inline and comment["line"] in positions.get(comment["path"], set()):
                comments.append({**comment, "side": "RIGHT"})
            else:
                extra.append(
                    f"{comment['path']}:{comment['line']}: {comment['body'].strip()}"
                )
        body = "\n\n".join(
            [
                DISCLOSURE,
                self.marker,
                proposal["summary"].strip(),
                *extra,
                VERDICTS[proposal["decision"]],
            ]
        )
        if len(body.encode()) > 60000:
            raise ValueError("Review body exceeds its byte limit")
        own = pr.get("user", {}).get("login", "").casefold() == self.login
        return {
            "commit_id": self.head,
            "event": "APPROVE"
            if proposal["decision"] == "approve" and not own
            else "COMMENT",
            "body": body,
            "comments": comments,
        }

    def confirmed(self, review, payload):
        return (
            isinstance(review, dict)
            and type(review.get("id")) is int
            and review["id"] > 0
            and review.get("commit_id") == self.head
            and review.get("body", "").replace("\r\n", "\n") == payload["body"]
            and review.get("state")
            == {"APPROVE": "APPROVED", "COMMENT": "COMMENTED"}[payload["event"]]
            and review.get("user", {}).get("login", "").casefold() == self.login
        )

    def publish(self, proposal):
        with self.locked():
            return self.publish_locked(proposal)

    def publish_locked(self, proposal):
        proposal = validate_proposal(proposal)
        blocked = self.precheck()
        if blocked:
            return blocked
        if proposal["decision"] == "skip":
            return {"status": "skipped", "reason": proposal["summary"]}
        pr = self.live()
        if pr is None:
            return {"status": "head_changed"}
        payload = self.payload(proposal, pr)
        for attempt in range(2):
            blocked = self.precheck()
            if blocked:
                return blocked
            if self.live() is None:
                return {"status": "head_changed"}
            record = {"status": "reserved", "payload": payload, "attempt": attempt + 1}
            write_record(self.record, record)
            try:
                receipt = self.api("POST", self.root + "/reviews", payload)
            except HTTPError as error:
                if error.code == 422 and payload["comments"] and attempt == 0:
                    try:
                        reviews = self.pages(self.root + "/reviews")
                        if any(self.marker in (r.get("body") or "") for r in reviews):
                            return {
                                "status": "needs_operator",
                                "reason": "Review exists after rejected submission",
                            }
                        details = error.read(32000).decode(errors="replace").lower()
                        if not any(
                            word in details
                            for word in ("comment", "position", "diff", "line")
                        ):
                            return {
                                "status": "needs_operator",
                                "reason": "Review rejected",
                            }
                        self.record.unlink()
                        payload = self.payload(proposal, pr, inline=False)
                        continue
                    except Exception:
                        return {
                            "status": "needs_operator",
                            "reason": "Rejected review could not be reconciled",
                        }
                return {
                    "status": "needs_operator",
                    "reason": f"Publication returned HTTP {error.code}; no automatic retry",
                }
            except Exception:
                return {
                    "status": "needs_operator",
                    "reason": "Publication outcome is uncertain; no automatic retry",
                }
            try:
                if not self.confirmed(receipt, payload):
                    return {
                        "status": "needs_operator",
                        "reason": "Accepted response did not match the payload",
                    }
                review = self.api("GET", self.root + f"/reviews/{receipt['id']}")
                if not self.confirmed(review, payload):
                    return {
                        "status": "needs_operator",
                        "reason": "Review readback did not match the payload",
                    }
                result = {
                    "status": "published" if self.live() else "head_changed_after_post",
                    "review_id": review["id"],
                    "url": review.get("html_url"),
                }
                if result["status"] == "published":
                    result["final_response"] = "GITHUB_REVIEW_POSTED"
                write_record(self.record, {**record, **result})
                return result
            except Exception:
                return {
                    "status": "needs_operator",
                    "reason": "Publication readback failed; no automatic retry",
                }
        return {"status": "needs_operator"}
