"""Bound recovery without treating conversation completion as review delivery."""

import time
from urllib.error import HTTPError, URLError

import httpx
from openhands.sdk.conversation.exceptions import ConversationRunError

MAX_ATTEMPTS = 3
RETRY_WAITS = (20, 60)
DELIVERY_BUDGET = 1000


def complete_review(conversation, check_delivery, record, save, deadline, *,
                    fresh_delivery=False, reserve_attempt=None,
                    clock=time.monotonic, sleep=time.sleep):
    """Resume only transient errors; keep the existing history and retry budget."""
    def finish(outcome, reason):
        record.update(outcome=outcome, reason=reason)
        save()
        return outcome

    current_error = None
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
                # Only the SDK run exception establishes ownership of this error.
                if current_error is None:
                    return finish("incomplete", "unconfirmed-error")
                classification = current_error.classification
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
            reserved = reserve_attempt() if reserve_attempt else attempts + 1
            if reserved > MAX_ATTEMPTS:
                return finish("incomplete", "attempts-exhausted")
            record.update(attempts=max(attempts + 1, reserved),
                          outcome="pending", reason="")
            save()
            if clock() >= deadline:
                return finish("incomplete", "deadline-exhausted")
            was_fresh = fresh_delivery
            record.pop("fresh_delivery", None)
            save()
            if clock() >= deadline:
                if was_fresh:
                    record["fresh_delivery"] = True
                return finish("incomplete", "deadline-exhausted")
            fresh_delivery = False
            current_error = None
            try:
                conversation.run(blocking=True, poll_interval=2,
                                 timeout=deadline - clock())
            except ConversationRunError as exc:
                if exc.conversation_error is None:
                    return finish("incomplete", "run-unconfirmed")
                current_error = exc.conversation_error
                # Reconcile publication before deciding whether this error is safe.
        except (httpx.HTTPError, HTTPError, URLError):
            return finish("incomplete", "verification-unavailable")
