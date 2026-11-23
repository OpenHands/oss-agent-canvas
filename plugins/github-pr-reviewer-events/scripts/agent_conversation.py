"""Idempotently deliver automation work to profile-backed conversations."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from openhands.sdk import RemoteConversation
from openhands.sdk.automation import automation_conversation_kwargs
from openhands.sdk.conversation.request import StartConversationRequest
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.workspace import LocalWorkspace, RemoteWorkspace

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


def _runtime_archived(agent_url: str, api_key: str, conversation_id: UUID) -> bool:
    request = Request(
        f"{agent_url.rstrip('/')}/api/conversations/{conversation_id}/runtime",
        headers={"X-Session-API-Key": api_key},
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response).get("can_resume") is False
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise


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

    def _upload_publisher(self, conversation_id: UUID, destination: Path) -> None:
        with RemoteWorkspace(
            host=self.agent_url,
            api_key=self.api_key,
            working_dir="/workspace",
            runtime_conversation_id=conversation_id,
        ) as runtime:
            result = runtime.file_upload(
                Path(__file__).with_name("review_publisher.py"), destination
            )
            if not result.success:
                raise RuntimeError("Could not stage the review publisher in the conversation")
            with TemporaryDirectory() as temp:
                stage = Path(temp) / "review.json"
                stage.write_text("{}\n", encoding="utf-8")
                result = runtime.file_upload(stage, destination.with_suffix(".json"))
            if not result.success:
                raise RuntimeError("Could not prepare the review body file in the conversation")

    def _deliver_once(
        self,
        conversation_id: UUID,
        prompt: str,
        metadata: dict[str, str | int] | None,
        same_delivery: bool,
        same_head: bool,
        publisher_path: Path,
    ) -> str:
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
                    **automation_conversation_kwargs(metadata=metadata),
                ),
                visualizer=None,
            )
            disposition = "created"

        try:
            if disposition == "created":
                # An initial_message can start the agent before its helper is available.
                self._upload_publisher(conversation_id, publisher_path)
                conversation.send_message(prompt)
                conversation.run(blocking=False)
            else:
                running = (
                    conversation.state.execution_status
                    == ConversationExecutionStatus.RUNNING
                )
                if running and (same_delivery or same_head):
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
                        self._upload_publisher(conversation_id, publisher_path)
                        conversation.update_secrets(self._secrets)
                        conversation.send_message(prompt)
                        conversation.run(blocking=False)
                        disposition = "retried"
                    else:
                        disposition = "deduplicated"
                else:
                    self._upload_publisher(conversation_id, publisher_path)
                    conversation.update_secrets(self._secrets)
                    conversation.send_message(prompt)
                    conversation.run(blocking=False)
        finally:
            conversation.close()
        return disposition

    def deliver(
        self,
        subject: str,
        delivery: str,
        prompt: str,
        head: str = "",
        metadata: dict[str, str | int] | None = None,
        *,
        publisher_path: Path,
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
        initial_id = uuid5(NAMESPACE_URL, f"{self.automation_id}:{subject}")
        state_key = f"{_CONVERSATION_KEY_PREFIX}{initial_id}"
        record = _kv_request(state_key, "GET") or {}
        if self._workspace is None:
            raise RuntimeError("AgentConversationDispatcher must be used as a context")

        same_delivery = record.get("delivery") == delivery
        same_head = bool(head) and record.get("head") == head
        conversation_id = UUID(record.get("conversation_id") or str(initial_id))

        # Rotate only the conversation ID: the KV key remains tied to the subject.
        for _ in range(5):
            if _runtime_archived(self.agent_url, self.api_key, conversation_id):
                conversation_id = uuid5(NAMESPACE_URL, f"{conversation_id}:replacement")
                same_delivery = same_head = False
            try:
                disposition = self._deliver_once(
                    conversation_id, prompt, metadata, same_delivery, same_head, publisher_path
                )
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 410:
                    raise
                # Archiving can race with the preflight check and delivery.
                conversation_id = uuid5(NAMESPACE_URL, f"{conversation_id}:replacement")
                same_delivery = same_head = False
        else:
            raise RuntimeError(f"Too many archived runtimes for {subject}")

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
