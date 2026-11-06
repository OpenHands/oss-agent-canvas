"""Bound recovery without treating conversation completion as review delivery."""

import fcntl
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError

import httpx
from openhands.sdk.conversation.exceptions import ConversationRunError
from openhands.sdk.event.conversation_error import ConversationErrorEvent

MAX_ATTEMPTS = 3
RETRY_WAITS = (20, 60)
DELIVERY_BUDGET = 1000


@contextmanager
def delivery_lock(key):
    directory = Path(tempfile.gettempdir()) / f"review-delivery-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise RuntimeError("Untrusted review delivery lock directory")
    fd = os.open(directory / key, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def complete_review(conversation, check_delivery, record, save, deadline, *,
                    fresh_delivery=False, clock=time.monotonic, sleep=time.sleep):
    """Resume only transient errors; keep the existing history and retry budget."""
    def finish(outcome, reason):
        record.update(outcome=outcome, reason=reason)
        save()
        return outcome

    while True:
        try:
            status = conversation.state.refresh_from_server()["execution_status"]
            verdict = check_delivery()
            if verdict == "published":
                return finish("published", "review-verified")
            if verdict != "pending":
                return finish("superseded" if verdict == "superseded"
                              else "incomplete", verdict)
            remaining = deadline - clock()
            if remaining <= 0:
                return finish("incomplete", "deadline-exhausted")
            attempts = record.get("attempts", 0)
            if status == "running":
                if deadline > clock():
                    sleep(min(2, deadline - clock()))
                continue
            if status == "error" and not fresh_delivery:
                error = next((event for event in reversed(conversation.state.events)
                              if isinstance(event, ConversationErrorEvent)), None)
                classification = error.classification if error else None
                if not (classification and classification.kind == "transient"
                        and classification.retryable):
                    return finish("incomplete", "non-transient-error")
            elif status not in ("idle", "error"):
                return finish("incomplete", "conversation-" + status)
            if attempts >= MAX_ATTEMPTS:
                return finish("incomplete", "attempts-exhausted")
            if status == "error" and not fresh_delivery:
                wait = RETRY_WAITS[min(max(attempts, 1), 2) - 1]
                if deadline - clock() <= wait:
                    return finish("incomplete", "deadline-exhausted")
                sleep(wait)
                # GitHub can accept a review or change the head during backoff.
                verdict = check_delivery()
                if verdict != "pending":
                    return finish("published" if verdict == "published"
                                  else "superseded" if verdict == "superseded"
                                  else "incomplete", verdict)
            if clock() >= deadline:
                return finish("incomplete", "deadline-exhausted")
            record.update(attempts=attempts + 1, outcome="pending", reason="")
            save()
            if clock() >= deadline:
                return finish("incomplete", "deadline-exhausted")
            fresh_delivery = False
            try:
                conversation.run(blocking=True, poll_interval=2,
                                 timeout=deadline - clock())
            except ConversationRunError as exc:
                if exc.conversation_error is None:
                    return finish("incomplete", "run-unconfirmed")
                # Reconcile publication before deciding whether this error is safe.
        except (httpx.HTTPError, HTTPError, URLError):
            return finish("incomplete", "verification-unavailable")
