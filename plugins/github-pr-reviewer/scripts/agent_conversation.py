"""Idempotently deliver automation work to profile-backed conversations."""

import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from openhands.sdk import RemoteConversation
from openhands.sdk.automation import automation_conversation_kwargs
from openhands.sdk.conversation.request import (
    SendMessageRequest,
    StartConversationRequest,
)
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.llm.message import TextContent
from openhands.sdk.workspace import LocalWorkspace, RemoteWorkspace

from review_delivery import DELIVERY_BUDGET, MAX_ATTEMPTS, complete_review

_CONVERSATION_KEY_PREFIX = "agent-conversation-"


def _register_tools() -> None:
    """Register tool models needed to deserialize an attached agent."""
    from openhands.tools import register_default_tools

    register_default_tools()


def _kv_request(key: str, method: str, value: dict | None = None) -> dict | None:
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
        with urlopen(request, timeout=90) as response:
            body = json.load(response)
    except HTTPError as exc:
        if method == "GET" and exc.code == 404:
            return None
        raise
    return body.get("value") if method == "GET" else body


@contextmanager
def delivery_claim(key):
    # A crashed owner leaves the claim held; automatic expiry could fence nobody.
    value = _kv_request(key + "/incr", "POST", {"by": 1})["value"]
    try:
        yield value == 1
    finally:
        _kv_request(key + "/decr", "POST", {"by": 1})


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
        self._delivery_deadline = time.monotonic() + DELIVERY_BUDGET

    def __enter__(self):
        _register_tools()
        self._workspace = RemoteWorkspace(
            host=self.agent_url,
            api_key=self.api_key,
            working_dir=os.environ.get("WORKSPACE_BASE", "/workspace"),
        )
        self._workspace.__enter__()
        self._secrets = self._workspace.get_secrets(
            agent_profile_id=str(self.profile_id)
        )
        return self

    def __exit__(self, *args):
        assert self._workspace is not None
        return self._workspace.__exit__(*args)

    def deliver(
        self,
        subject: str,
        delivery: str,
        prompt: str,
        head: str = "",
        metadata: dict[str, str | int] | None = None,
        check_delivery: Callable[[], str] | None = None,
        allow_same_head_refresh: bool = False,
    ) -> dict:
        """A clarified request may reuse a finished head, but not its spent budget."""
        if check_delivery is None:
            return self._dispatch(subject=subject, delivery=delivery, prompt=prompt,
                                  head=head, metadata=metadata)
        key = str(uuid5(NAMESPACE_URL, f"{self.automation_id}:{subject}"))
        with delivery_claim("review-delivery-claim-" + key) as acquired:
            if not acquired:
                return {"disposition": "resumed", "conversation_id": key,
                        "outcome": "incomplete", "reason": "delivery-active"}
            return self._dispatch(subject=subject, delivery=delivery, prompt=prompt,
                                  head=head, metadata=metadata,
                                  check_delivery=check_delivery,
                                  allow_same_head_refresh=allow_same_head_refresh)

    def _dispatch(self, *, subject, delivery, prompt, head="", metadata=None,
                  check_delivery=None, allow_same_head_refresh=False):
        conversation_id = uuid5(NAMESPACE_URL, f"{self.automation_id}:{subject}")
        state_key = f"{_CONVERSATION_KEY_PREFIX}{conversation_id}"
        record = _kv_request(state_key, "GET") or {}
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
            conversation = RemoteConversation.create(
                self._workspace,
                StartConversationRequest(
                    workspace=LocalWorkspace(working_dir="/workspace"),
                    conversation_id=conversation_id,
                    agent_profile_id=self.profile_id,
                    secrets=self._secrets,
                    initial_message=SendMessageRequest(
                        content=[TextContent(text=prompt)], run=check_delivery is None
                    ),
                    **automation_conversation_kwargs(metadata=metadata),
                ),
                visualizer=None,
            )
            disposition = "created"
        try:
            if check_delivery is not None:
                status = conversation.state.refresh_from_server()["execution_status"]
                verdict = check_delivery()
                if verdict != "pending":
                    return {
                        "disposition": disposition,
                        "conversation_id": str(conversation_id),
                        "outcome": "published" if verdict == "published" else
                                   "superseded" if verdict == "superseded" else "incomplete",
                        "reason": verdict,
                        "attempts": record.get("attempts", 0),
                    }
                if disposition == "resumed" and not same_delivery and status in (
                    "running", "paused"
                ):
                    return {
                        "disposition": disposition,
                        "conversation_id": str(conversation_id),
                        "outcome": "incomplete",
                        "reason": "previous-delivery-active",
                    }
                counter_key = "review-attempts-" + str(uuid5(
                    conversation_id, head or delivery
                ))
                counter = _kv_request(counter_key, "GET")
                if counter is None:
                    attempts = record.get("attempts", int(status in ("running", "error")))
                    counter = _kv_request(counter_key + "/incr", "POST", {
                        "by": attempts if same_head or same_delivery else 0
                    })["value"]
                new_turn = disposition == "resumed" and not same_delivery and (
                    not same_head or (allow_same_head_refresh and status == "finished")
                )
                if new_turn:
                    if counter >= MAX_ATTEMPTS or time.monotonic() >= self._delivery_deadline:
                        return {
                            "disposition": disposition,
                            "conversation_id": str(conversation_id),
                            "outcome": "incomplete",
                            "reason": "attempts-exhausted" if counter >= MAX_ATTEMPTS
                                      else "deadline-exhausted",
                            "attempts": counter,
                        }
                    conversation.send_message(prompt)
                    status = conversation.state.refresh_from_server()["execution_status"]
                    record = {"fresh_delivery": True, "attempts": counter}
                elif disposition == "created":
                    record = {"attempts": counter}
                record.update(subject=subject, conversation_id=str(conversation_id))
                if disposition == "created" or same_delivery or new_turn:
                    record.update(delivery=delivery, head=head)
                record.setdefault("attempts", counter)

                def save():
                    _kv_request(state_key, "PUT", record)

                def reserve_attempt():
                    return _kv_request(counter_key + "/incr", "POST", {"by": 1})["value"]

                save()
                conversation.update_secrets(self._secrets)
                outcome = complete_review(
                    conversation, check_delivery, record, save,
                    self._delivery_deadline,
                    fresh_delivery=record.get("fresh_delivery", False),
                    reserve_attempt=reserve_attempt,
                )
                return {
                    "disposition": disposition,
                    "conversation_id": str(conversation_id),
                    "outcome": outcome,
                    "reason": record["reason"],
                    "attempts": record["attempts"],
                }
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
                elif same_delivery:
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
                        # The stored delivery matched but the conversation died, so
                        # its work never finished. Re-send the prompt to retry it:
                        # treating ERROR as deduplicated would strand the subject
                        # forever, because the delivery never changes.
                        conversation.update_secrets(self._secrets)
                        conversation.send_message(prompt)
                        conversation.run(blocking=False)
                        disposition = "retried"
                    else:
                        disposition = "deduplicated"
                else:
                    # A changed head, or a head a human clarified since, is new
                    # work on the same subject: reuse the conversation and send
                    # the revision as its next turn.
                    conversation.update_secrets(self._secrets)
                    conversation.send_message(prompt)
                    conversation.run(blocking=False)
        finally:
            conversation.close()

        if disposition in ("deduplicated", "in_progress"):
            return {
                "disposition": disposition,
                "conversation_id": str(conversation_id),
            }

        _kv_request(
            state_key,
            "PUT",
            {
                "subject": subject,
                "conversation_id": str(conversation_id),
                "delivery": delivery,
                "head": head or record.get("head") or "",
            },
        )
        return {
            "disposition": disposition,
            "conversation_id": str(conversation_id),
        }
