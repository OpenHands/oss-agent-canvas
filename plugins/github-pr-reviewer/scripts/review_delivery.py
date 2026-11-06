"""Bound recovery without treating conversation completion as review delivery."""

import time
from urllib.error import HTTPError, URLError

import httpx
from openhands.sdk.conversation.exceptions import ConversationRunError
from openhands.sdk.event.conversation_error import ConversationErrorEvent

MAX_ATTEMPTS = 3
RETRY_WAITS = (20, 60)
DELIVERY_BUDGET = 1000


def complete_review(conversation, check_delivery, record, save, deadline, *,
                    clock=time.monotonic, sleep=time.sleep):
    """Resume only transient errors; keep the existing history and retry budget."""
    def finish(outcome, reason):
        record.update(outcome=outcome, reason=reason)
        save()
        return outcome

    while True:
        try:
            verdict = check_delivery()
            if verdict == "published":
                return finish("published", "review-verified")
            if verdict != "pending":
                return finish("superseded" if verdict == "superseded"
                              else "incomplete", verdict)
            remaining = deadline - clock()
            if remaining <= 0:
                return finish("incomplete", "deadline-exhausted")
            status = conversation.state.refresh_from_server()["execution_status"]
            attempts = record.get("attempts", 0)
            if status == "error":
                error = next((event for event in reversed(conversation.state.events)
                              if isinstance(event, ConversationErrorEvent)), None)
                classification = error.classification if error else None
                if not (classification and classification.kind == "transient"
                        and classification.retryable):
                    return finish("incomplete", "non-transient-error")
            elif status not in ("idle", "running"):
                return finish("incomplete", "conversation-" + status)

            if status != "running":
                if attempts >= MAX_ATTEMPTS:
                    return finish("incomplete", "attempts-exhausted")
                if status == "error":
                    wait = RETRY_WAITS[min(max(attempts, 1), 2) - 1]
                    if remaining <= wait:
                        return finish("incomplete", "deadline-exhausted")
                    sleep(wait)
                    # GitHub can accept a review or change the head during backoff.
                    verdict = check_delivery()
                    if verdict != "pending":
                        return finish("published" if verdict == "published"
                                      else "superseded" if verdict == "superseded"
                                      else "incomplete", verdict)
                record.update(attempts=attempts + 1, outcome="pending", reason="")
                save()
            try:
                # A running conversation returns 409; the SDK observes that turn.
                conversation.run(blocking=True, poll_interval=2,
                                 timeout=max(0.01, deadline - clock()))
            except ConversationRunError as exc:
                if exc.conversation_error is None:
                    return finish("incomplete", "run-unconfirmed")
                # Reconcile publication before deciding whether this error is safe.
        except (httpx.HTTPError, HTTPError, URLError):
            return finish("incomplete", "verification-unavailable")
