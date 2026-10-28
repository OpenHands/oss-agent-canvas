import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from uuid import uuid4

from review_gateway import ReviewGateway, serve_gateway
from review_publication import ReviewPublisher


def profile_agent(dispatcher):
    from openhands.sdk import LLM, Agent
    from openhands.sdk.tool import Tool

    def get(path):
        request = Request(
            dispatcher.agent_url.rstrip("/") + path,
            headers={
                "X-Session-API-Key": dispatcher.api_key,
                "X-Expose-Secrets": "plaintext",
            },
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    entries = get("/api/agent-profiles")["profiles"]
    selected = next(p for p in entries if p["id"] == str(dispatcher.profile_id))
    profile = get("/api/agent-profiles/" + quote(selected["name"], safe=""))["profile"]
    if profile["agent_kind"] != "openhands":
        raise ValueError("Controlled reviews require an OpenHands LLM profile")
    config = get("/api/profiles/" + quote(profile["llm_profile_ref"], safe=""))[
        "config"
    ]
    return Agent(
        llm=LLM.model_validate(
            {**config, "num_retries": 0, "timeout": 60, "max_output_tokens": 4096}
        ),
        tools=[Tool(name=name) for name in ("terminal", "file_editor", "task_tracker")],
        mcp_config={},
    )


def repository_archive(reviewer, sha):
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            from urllib.parse import urlsplit

            target = urlsplit(newurl)
            if target.scheme != "https" or target.hostname != "codeload.github.com":
                raise ValueError("Unexpected repository archive redirect")
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
            redirected.remove_header("Authorization")
            return redirected

    request = Request(
        f"https://api.github.com/repos/{reviewer.repository}/tarball/{sha}",
        headers={"Authorization": "Bearer " + reviewer.token},
    )
    with build_opener(Redirect()).open(request, timeout=60) as response:
        data = response.read(64_000_001)
    if len(data) > 64_000_000:
        raise ValueError("Repository archive exceeds the byte limit")
    return data


def analyze(dispatcher, gateway, prompt, metadata, *, timeout=180):
    from openhands.sdk import RemoteConversation
    from openhands.sdk.automation import automation_conversation_kwargs
    from openhands.sdk.conversation.request import (
        SendMessageRequest,
        StartConversationRequest,
    )
    from openhands.sdk.conversation.state import ConversationExecutionStatus
    from openhands.sdk.llm.message import TextContent
    from openhands.sdk.secret import StaticSecret
    from openhands.sdk.workspace import LocalWorkspace

    agent = profile_agent(dispatcher)
    with serve_gateway(
        gateway, os.environ.get("REVIEW_GATEWAY_HOST", "172.17.0.1")
    ) as url:
        conversation = RemoteConversation.create(
            dispatcher._workspace,
            StartConversationRequest(
                conversation_id=uuid4(),
                workspace=LocalWorkspace(working_dir="/workspace"),
                agent=agent,
                secrets={
                    "REVIEW_GATEWAY_URL": StaticSecret(value=url),
                    "REVIEW_GATEWAY_TOKEN": StaticSecret(value=gateway.token),
                },
                max_iterations=30,
                initial_message=SendMessageRequest(
                    content=[TextContent(text=prompt)], run=True
                ),
                **automation_conversation_kwargs(metadata=metadata),
            ),
            visualizer=None,
        )
        try:
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                status = conversation.state.execution_status
                if status == ConversationExecutionStatus.FINISHED:
                    return str(conversation.id), gateway.proposal
                if status in (
                    ConversationExecutionStatus.ERROR,
                    ConversationExecutionStatus.PAUSED,
                ):
                    return str(conversation.id), None
                time.sleep(1)
            conversation.pause()
            return str(conversation.id), None
        finally:
            conversation.close()


def run_review(reviewer, pr, delivery, prompt, metadata):
    publisher = ReviewPublisher(
        lambda method, path, body=None: reviewer.api(method, path, body=body),
        reviewer.repository,
        pr["number"],
        pr["head"]["sha"],
        reviewer.github_login,
        delivery,
        os.environ.get(
            "REVIEW_STATE_DIR", str(Path.home() / ".local/state/review-publication")
        ),
    )
    with publisher.locked():
        blocked = publisher.precheck()
        if blocked:
            return {
                "disposition": "deduplicated",
                "conversation_id": None,
                "publication": blocked,
            }
        gateway = ReviewGateway(
            reviewer.repository,
            pr["number"],
            pr["head"]["sha"],
            secrets.token_urlsafe(32),
            lambda path: reviewer.api("GET", path),
            lambda: repository_archive(reviewer, pr["head"]["sha"]),
        )
        conversation_id, proposal = analyze(
            reviewer.dispatcher, gateway, prompt, metadata
        )
        result = (
            publisher.publish_locked(proposal)
            if proposal is not None
            else {
                "status": "analysis_incomplete",
                "reason": "No accepted proposal from a finished agent",
            }
        )
        return {
            "disposition": "created",
            "conversation_id": conversation_id,
            "publication": result,
        }
