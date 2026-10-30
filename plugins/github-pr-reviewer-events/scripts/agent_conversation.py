"""Idempotently deliver automation work to profile-backed conversations."""

import json
import os
import sys
from collections.abc import Callable
from time import monotonic, sleep, time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from openhands.sdk import RemoteConversation
from openhands.sdk.automation import automation_conversation_kwargs
from openhands.sdk.conversation.request import (
    SendMessageRequest,
    StartConversationRequest,
)
from openhands.sdk.conversation.state import ConversationExecutionStatus, ConversationState
from openhands.sdk.event import Event, MessageEvent
from openhands.sdk.event.conversation_error import ConversationErrorEvent
from openhands.sdk.llm.exceptions import LLMRateLimitError, is_quota_exhaustion_error
from openhands.sdk.llm.message import TextContent
from openhands.sdk.workspace import LocalWorkspace, RemoteWorkspace

_CONVERSATION_KEY_PREFIX = "agent-conversation-"
_PROVIDER_RETRY_DELAYS = (30, 120)
# Discovery and dispatch share this observation window; worker.py bounds all I/O.
_RECOVERY_WINDOW_SECONDS = 900
_RETRY_START_RESERVE = 90
_PROVIDER_ERROR_CODES = frozenset({
    "InternalServerError", "ServiceUnavailableError", "BadGatewayError",
    "APIConnectionError", "RateLimitError", "Timeout", "OpenAIError", "APIError",
    "LLMServiceUnavailableError", "LLMTimeoutError", "LLMRateLimitError",
    "LLMNoResponseError",
})


def _register_tools() -> None:
    """Register tool models needed to deserialize an attached agent."""
    from openhands.tools import register_default_tools

    register_default_tools()


def _kv_request(
    key: str, method: str, value: dict | None = None, *, timeout: float = 90
) -> dict | None:
    base_url = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")
    token = os.environ.get("AUTOMATION_KV_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("Automation KV is required for agent conversation dispatch")
    request = Request(
        f"{base_url}/v1/kv/{key}",
        data=json.dumps(value).encode() if value is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except HTTPError as exc:
        if method == "GET" and exc.code == 404:
            return None
        raise
    return body.get("value") if method == "GET" else body


class AgentConversationDispatcher:
    """Deliver one revision at a time to a stable conversation for each subject."""

    def __init__(self) -> None:
        self.agent_url = os.environ["AGENT_SERVER_URL"]
        self.api_key = os.environ["SESSION_API_KEY"]
        self.profile_id = UUID(os.environ["AUTOMATION_AGENT_PROFILE_ID"])
        payload = json.loads(os.environ["AUTOMATION_EVENT_PAYLOAD"])
        self.automation_id = str(payload["automation_id"])
        self._workspace: RemoteWorkspace | None = None
        self._secrets = {}
        self._deadline = monotonic() + _RECOVERY_WINDOW_SECONDS
        self._pending = []

    def __enter__(self):
        _register_tools()
        self._workspace = RemoteWorkspace(
            host=self.agent_url,
            api_key=self.api_key,
            working_dir=os.environ.get("WORKSPACE_BASE", "/workspace"),
        )
        try:
            self._workspace.__enter__()
            self._secrets = self._workspace.get_secrets(
                agent_profile_id=str(self.profile_id)
            )
        except BaseException:
            try:
                self._workspace.__exit__(*sys.exc_info())
            finally:
                self._workspace.reset_client()
            raise
        return self

    def __exit__(self, *args):
        assert self._workspace is not None
        try:
            failures = (
                self._recover()
                if args[0] is None or issubclass(args[0], Exception)
                else []
            )
            if failures and args[0] is None:
                raise RuntimeError("Review recovery failed: " + ", ".join(failures))
        except BaseException:
            if args[0] is None:
                args = sys.exc_info()
            raise
        finally:
            try:
                self._workspace.__exit__(*args)
            finally:
                self._workspace.reset_client()

    def _request(self, method, path, **kwargs):
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Review recovery observation window elapsed")
        response = self._workspace.client.request(
            method, path, timeout=min(10, remaining), **kwargs,
        )
        if not (method == "POST" and path.endswith("/run") and response.status_code == 409):
            response.raise_for_status()
        return response

    def _provider_failure(self, conversation_id):
        history, page = [], None
        for _ in range(10):
            if monotonic() >= self._deadline:
                return None
            params = {"limit": 100}
            if page:
                params["page_id"] = page
            response = self._request(
                "GET", f"/api/conversations/{conversation_id}/events/search",
                params=params,
            )
            data = response.json()
            history.extend(Event.model_validate(item) for item in data["items"])
            page = data.get("next_page_id")
            if not page:
                break
        else:
            return None
        if ConversationState.get_unmatched_actions(history):
            return None
        for event in reversed(history):
            if isinstance(event, MessageEvent) and event.source == "user":
                break
            if isinstance(event, ConversationErrorEvent):
                classification = event.classification
                if (
                    event.code in _PROVIDER_ERROR_CODES
                    and classification and classification.retryable
                    and classification.kind in ("transient", "rate_limit")
                    and not is_quota_exhaustion_error(LLMRateLimitError(event.detail))
                ):
                    return event
                break
        return None

    def _recover(self):
        failures, notifications = [], []
        pending = list(self._pending)
        while pending and monotonic() < self._deadline:
            for job in list(pending):
                if monotonic() >= self._deadline:
                    break
                record = job["record"]
                path = f"/api/conversations/{record['conversation_id']}"
                try:
                    response = self._request("GET", path)
                    status = response.json()["execution_status"]
                    job["status"] = status
                    if status in ("running", "idle"):
                        continue
                    if status == "stuck":
                        raise RuntimeError("conversation stuck; provider recovery unsafe")
                    if status != "error":
                        pending.remove(job)
                        continue
                    if time() < record.get("provider_retry_at", 0):
                        continue
                    current = _kv_request(job["state_key"], "GET", timeout=10) or {}
                    if current.get("recovery_owner") != record["recovery_owner"]:
                        pending.remove(job)
                        self._report(job, "recovery_superseded")
                        continue
                    error = self._provider_failure(record["conversation_id"])
                    retries = record["provider_retries"]
                    if not error or retries >= len(_PROVIDER_RETRY_DELAYS) or not job["can_retry"]:
                        raise RuntimeError("provider recovery unavailable or exhausted")
                    if "provider_retry_at" not in record:
                        record["provider_retry_at"] = time() + _PROVIDER_RETRY_DELAYS[retries]
                        _kv_request(job["recovery_key"], "PUT", record, timeout=10)
                    if time() < record["provider_retry_at"]:
                        continue
                    if monotonic() + _RETRY_START_RESERVE >= self._deadline:
                        continue
                    if not job["can_retry"](record.get("review_started_at")):
                        pending.remove(job)
                        self._report(job, "recovery_skipped_pr_changed_or_reviewed")
                        continue
                    current = _kv_request(job["state_key"], "GET", timeout=10) or {}
                    if current.get("recovery_owner") != record["recovery_owner"]:
                        pending.remove(job)
                        self._report(job, "recovery_superseded")
                        continue
                    record["provider_retries"] = retries + 1
                    record.pop("provider_retry_at", None)
                    # Reserve before the run write; ambiguous writes never replenish retries.
                    _kv_request(job["recovery_key"], "PUT", record, timeout=10)
                    if monotonic() + 30 >= self._deadline:
                        continue
                    # Only resume inference; never append another review instruction.
                    response = self._request("POST", path + "/run")
                    self._report(job, "provider_retry_started")
                except Exception as exc:  # noqa: BLE001 - other reviews must still recover
                    pending.remove(job)
                    failures.append(record["subject"])
                    record["closed"] = True
                    try:
                        _kv_request(job["recovery_key"], "PUT", record, timeout=10)
                    except Exception:  # noqa: BLE001 - failure is still reported below
                        pass
                    self._report(job, "review_execution_failed", type(exc).__name__)
                    notifications.append(job)
            if pending:
                sleep(min(1, max(0, self._deadline - monotonic())))
        for job in pending:
            if job.get("status") == "error":
                failures.append(job["record"]["subject"])
                self._report(job, "provider_recovery_deadline_exhausted")
                notifications.append(job)
            else:
                self._report(job, "recovery_window_elapsed_execution_unconfirmed")
        notification_deadline = min(monotonic() + 60, self._deadline + 60)
        for job in notifications:
            self._notify_failure(job, notification_deadline)
        return failures

    def _notify_failure(self, job, deadline):
        if not job["on_failure"]:
            return

        def still_failed():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Review failure notification budget exhausted")
            current = _kv_request(job["state_key"], "GET", timeout=min(5, remaining)) or {}
            if current.get("recovery_owner") != job["record"]["recovery_owner"]:
                return False
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Review failure notification budget exhausted")
            # A lost run response may mean execution is still active, not failed.
            response = self._workspace.client.get(
                f"/api/conversations/{job['record']['conversation_id']}",
                timeout=min(5, remaining),
            )
            response.raise_for_status()
            return monotonic() < deadline and response.json()["execution_status"] == "error"

        try:
            if still_failed():
                job["on_failure"](job["record"].get("review_started_at"), still_failed, deadline)
        except Exception as exc:  # noqa: BLE001 - preserve the original failure
            self._report(job, "failure_notification_failed", type(exc).__name__)

    @staticmethod
    def _report(job, outcome, error_type=None):
        print(json.dumps({
            "subject": job["record"]["subject"],
            "conversation_id": job["record"]["conversation_id"],
            "outcome": outcome,
            "provider_retries": job["record"]["provider_retries"],
            "error_type": error_type,
        }), flush=True)

    def deliver(
        self,
        subject: str,
        delivery: str,
        prompt: str,
        head: str = "",
        metadata: dict[str, str | int] | None = None,
        can_retry: Callable[[float | None], bool] | None = None,
        on_failure: Callable[[float | None, Callable[[], bool], float], None] | None = None,
    ) -> dict[str, str]:
        """Deliver one revision of `subject`, deduping a repeated head.

        `delivery` keys the revision: a second trigger for the same revision
        reuses the conversation without a new turn, and a changed head becomes a
        new delivery. `head` is the revision's commit, and it is the guard that
        matters when two triggers name the same commit - a re-request after the
        bot's own handoff, say - because a delivery key that only counts triggers
        would send a second turn and publish a second review of identical code.
        A conversation already running for this head is reported `in_progress`
        and left alone, whatever the new delivery says. Callers with no revision
        identity leave `head` empty and keep the delivery-key-only behavior.
        """
        conversation_id = uuid5(NAMESPACE_URL, f"{self.automation_id}:{subject}")
        state_key = f"{_CONVERSATION_KEY_PREFIX}{conversation_id}"
        record = _kv_request(state_key, "GET") or {}
        review_started_at = record.get("review_started_at")
        if self._workspace is None:
            raise RuntimeError("AgentConversationDispatcher must be used as a context")

        same_delivery = record.get("delivery") == delivery
        same_head = bool(head) and record.get("head") == head

        try:
            conversation = RemoteConversation.attach(
                self._workspace, conversation_id, visualizer=None
            )
            disposition = "resumed"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            review_started_at = time()
            conversation = RemoteConversation.create(
                self._workspace,
                StartConversationRequest(
                    workspace=LocalWorkspace(working_dir="/workspace"),
                    conversation_id=conversation_id,
                    agent_profile_id=self.profile_id,
                    secrets=self._secrets,
                    initial_message=SendMessageRequest(
                        content=[TextContent(text=prompt)], run=True
                    ),
                    **automation_conversation_kwargs(metadata=metadata),
                ),
                visualizer=None,
            )
            disposition = "created"
        try:
            if disposition == "resumed":
                running = (
                    conversation.state.execution_status
                    == ConversationExecutionStatus.RUNNING
                )
                if running and (same_delivery or same_head):
                    # A turn is already running for this very revision, so a new
                    # trigger is not new work: report the live conversation
                    # rather than starting a second pass beside it.
                    conversation.update_secrets(self._secrets)
                    disposition = "in_progress"
                elif same_delivery or (
                    same_head and conversation.state.execution_status
                    == ConversationExecutionStatus.ERROR
                ):
                    if conversation.state.execution_status in (
                        ConversationExecutionStatus.IDLE,
                        ConversationExecutionStatus.PAUSED,
                    ):
                        conversation.update_secrets(self._secrets)
                        conversation.run(blocking=False)
                    elif (
                        conversation.state.execution_status
                        == ConversationExecutionStatus.ERROR
                    ):
                        # Only the original dispatch supervises its running/failed turn.
                        if record.get("recovery_owner"):
                            recovery = _kv_request(record["recovery_key"], "GET") or {}
                            if not recovery.get("closed") and record.get("recovery_until", 0) > time():
                                disposition = "in_progress"
                            elif not same_delivery:
                                review_started_at = time()
                                conversation.update_secrets(self._secrets)
                                conversation.send_message(prompt)
                                conversation.run(blocking=False)
                            else:
                                raise RuntimeError("Provider recovery ended; review execution failed")
                        else:
                            conversation.update_secrets(self._secrets)
                            disposition = "recovery_pending"
                    else:
                        disposition = "deduplicated"
                else:
                    # A changed head, or a head a human clarified since, is new
                    # work on the same subject: reuse the conversation and send
                    # the revision as its next turn.
                    review_started_at = time()
                    conversation.update_secrets(self._secrets)
                    conversation.send_message(prompt)
                    conversation.run(blocking=False)
            if disposition not in ("deduplicated", "in_progress"):
                # Recovery writes never replace a newer dispatch's shared delivery record.
                recovery_key = "provider-recovery-" + str(uuid4())
                recovery = record if same_delivery else {}
                record = {
                    "subject": subject,
                    "conversation_id": str(conversation_id),
                    "delivery": delivery,
                    "head": head or record.get("head") or "",
                    "review_started_at": review_started_at,
                    "recovery_owner": recovery_key,
                    "recovery_key": recovery_key,
                    "recovery_until": time() + max(0, self._deadline - monotonic()),
                    "provider_retries": recovery.get("provider_retries", 0),
                    **({"provider_retry_at": recovery["provider_retry_at"]}
                       if "provider_retry_at" in recovery else {}),
                }
                _kv_request(state_key, "PUT", record)
                self._pending.append({
                    "record": record,
                    "state_key": state_key, "recovery_key": recovery_key,
                    "can_retry": can_retry, "on_failure": on_failure,
                })
        finally:
            conversation.close()

        return {
            "disposition": disposition,
            "conversation_id": str(conversation_id),
        }
