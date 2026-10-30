"""Real SDK dispatch against isolated HTTP/WebSocket boundaries, with no inference."""

import asyncio
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import importlib
import importlib.util
import inspect
import io
import ipaddress
import json
import os
import signal
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins/github-pr-reviewer-events/scripts/agent_conversation.py"
AUTOMATION = "synthetic-review-automation"
PROFILE = "00000000-0000-0000-0000-000000000001"
SUBJECT = "example/repository:pr:1"
DELIVERY = "synthetic-trigger:head-a"
HEAD = "head-a"
PROMPT = "Review the public synthetic example change exactly once."
CONVERSATION_ID = str(uuid5(NAMESPACE_URL, f"{AUTOMATION}:{SUBJECT}"))
STATE_KEY = f"agent-conversation-{CONVERSATION_ID}"


def setUpModule():
    stack = ExitStack()
    unittest.addModuleCleanup(stack.close)
    home = stack.enter_context(tempfile.TemporaryDirectory(prefix="review-recovery-"))
    stack.enter_context(patch.dict(os.environ, {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "XDG_CACHE_HOME": home,
        "XDG_CONFIG_HOME": home,
        "OPENHANDS_SUPPRESS_BANNER": "1",
        "LOG_LEVEL": "CRITICAL",
        "OPENHANDS_REMOTE_WS_READY_TIMEOUT": "3",
        "OTEL_SDK_DISABLED": "true",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "LITELLM_TELEMETRY": "False",
        "DO_NOT_TRACK": "1",
        "NO_PROXY": "*",
    }, clear=True))

    global blocked_connections
    blocked_connections = []
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo

    def check_host(host):
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        blocked_connections.append(str(host))
        raise AssertionError("Non-loopback network access forbidden in this suite")

    def local_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check_host(address[0])
        return connect(sock, address)

    def local_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check_host(address[0])
        return connect_ex(sock, address)

    def local_getaddrinfo(host, *args, **kwargs):
        check_host(host)
        return getaddrinfo(host, *args, **kwargs)

    # These are network safety guards, not replacements for SDK behavior.
    stack.enter_context(patch.object(socket.socket, "connect", local_connect))
    stack.enter_context(patch.object(socket.socket, "connect_ex", local_connect_ex))
    stack.enter_context(patch.object(socket, "getaddrinfo", local_getaddrinfo))

    # Import after removing telemetry credentials and redirecting SDK caches.
    global sdk, events, llm, builtins, web, dispatcher_module
    sdk = importlib.import_module("openhands.sdk")
    events = importlib.import_module("openhands.sdk.event")
    llm = importlib.import_module("openhands.sdk.llm")
    builtins = importlib.import_module("openhands.sdk.tool.builtins.think")
    web = importlib.import_module("aiohttp.web")
    spec = importlib.util.spec_from_file_location("reviewer_recovery_dispatch", SOURCE)
    dispatcher_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatcher_module)

    global worker_module
    # Standalone scripts use bare imports; do not borrow another plugin's modules.
    with patch.dict(sys.modules), patch.object(sys, "path", [str(SOURCE.parent), *sys.path]):
        for name in ("main", "github_client", "maintainer_handoff"):
            sys.modules.pop(name, None)
        sys.modules["agent_conversation"] = dispatcher_module
        with patch.dict(os.environ, {
            "AUTOMATION_CONFIG_PATH": str(Path(home) / "unused-config.json"),
        }):
            spec = importlib.util.spec_from_file_location(
                "reviewer_recovery_worker", SOURCE.with_name("worker.py")
            )
            worker_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker_module)


def provider_error(code="LLMServiceUnavailableError", detail="Synthetic outage", **kw):
    return events.conversation_error.ConversationErrorEvent(
        source="environment", code=code, detail=detail, **kw
    )


def user_message(content=None):
    return events.MessageEvent(
        source="user", llm_message=llm.Message(
            role="user", content=content if content is not None else [llm.TextContent(text=PROMPT)]
        )
    )


def pending_action():
    return events.ActionEvent(
        thought=[],
        action=builtins.ThinkAction(thought="Synthetic pending action"),
        tool_name="think",
        tool_call_id="synthetic-tool-call",
        tool_call=llm.MessageToolCall(
            id="synthetic-tool-call", name="think",
            arguments='{"thought":"example"}', origin="completion",
        ),
        llm_response_id="synthetic-completion",
    )


class LoopbackServices:
    """Minimal agent-server and KV wire contracts, all state held in memory."""

    def __init__(self, outcomes, *, status=None, history=(), record=None,
                 subject=SUBJECT):
        self.peers = {}
        self.delays = {}
        self.heartbeat_period = .05
        self.mute_updates = False
        self.github_pr = {
            "number": 1, "state": "open", "head": {"sha": HEAD},
            "title": "Synthetic example", "body": "Synthetic change",
            "user": {"login": "contributor"}, "labels": [],
            "requested_reviewers": [{"login": "all-hands-bot"}],
        }
        self.github_reviews = []
        self.github_login = "all-hands-bot"
        self.on_comments = None
        self.github_comments = []
        self.github_writes = []
        self.subject = subject
        self.conversation_id = str(uuid5(NAMESPACE_URL, f"{AUTOMATION}:{subject}"))
        self.state_key = f"agent-conversation-{self.conversation_id}"
        self.outcomes = list(outcomes)
        self.status = status
        self.history = list(history)
        self.records = {self.state_key: deepcopy(record)} if record is not None else {}
        self.requests = []
        self.creates = []
        self.messages = []
        self.runs = []
        self.callbacks = []
        self.completions = []
        self.sockets = set()
        self.tasks = set()
        self.failures = []
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.agent = sdk.Agent(llm=sdk.LLM(
            model="openai/gpt-4o-mini", api_key="synthetic-unused",
            num_retries=0, log_completions=False,
        ), tools=[]).model_dump(mode="json")

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("Loopback service did not start")
        if self.failures:
            raise self.failures[0]
        return self

    def __exit__(self, *args):
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
        if self.thread.is_alive():
            raise RuntimeError("Loopback service did not stop")
        if self.failures:
            raise self.failures[0]

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._start())
        except Exception as exc:
            self.failures.append(exc)
            self.ready.set()
        else:
            self.ready.set()
            self.loop.run_forever()
        finally:
            self.loop.close()

    async def _start(self):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def _shutdown(self):
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for ws in tuple(self.sockets):
            await ws.close()
        await self.runner.cleanup()

    def info(self):
        return {
            "id": self.conversation_id,
            "agent": self.agent,
            "workspace": {"kind": "LocalWorkspace", "working_dir": "/workspace"},
            "max_iterations": 5,
            "execution_status": self.status,
            "persistence_dir": None,
        }

    def _state_event(self):
        return events.ConversationStateUpdateEvent(key="full_state", value=self.info())

    async def _broadcast(self, event):
        if self.mute_updates:
            return
        for ws in tuple(self.sockets):
            if not ws.closed:
                await ws.send_json(event.model_dump(mode="json"))

    async def _socket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        auth = await ws.receive_json()
        if auth != {"type": "auth", "session_api_key": "synthetic-session"}:
            self.failures.append(AssertionError("Unexpected WebSocket authentication"))
            await ws.close(code=4001)
            return ws
        self.sockets.add(ws)
        try:
            await ws.send_json(self._state_event().model_dump(mode="json"))
            while not ws.closed:
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=self.heartbeat_period)
                    if message.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED,
                                        web.WSMsgType.ERROR):
                        break
                except asyncio.TimeoutError:
                    # Wake native SDK close() without queuing a stale terminal status.
                    heartbeat = events.ConversationStateUpdateEvent(
                        key="_test_heartbeat", value=None,
                    )
                    await ws.send_json(heartbeat.model_dump(mode="json"))
        finally:
            self.sockets.discard(ws)
        return ws

    async def _begin_run(self, origin, request=None):
        record = self.records.get(self.state_key) or {}
        self.runs.append({
            "origin": origin,
            "record": deepcopy(self.records.get(record.get("recovery_key")) or record),
        })
        outcome = self.outcomes.pop(0) if self.outcomes else provider_error()
        if outcome in ("transport", "accepted_transport"):
            if outcome == "accepted_transport":
                self.status = "running"
                task = self.loop.create_task(self._complete("finished"))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            if request is not None:
                request.transport.close()
            return web.json_response({"detail": "Synthetic run endpoint unavailable"}, status=503)
        self.status = "running"
        await self._broadcast(events.ConversationStateUpdateEvent(
            key="execution_status", value="running",
        ))
        if outcome != "hang":
            task = self.loop.create_task(self._complete(outcome))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return web.json_response({})

    async def _complete(self, outcome):
        # Finish after the POST reply so SDK run() can arm its WS completion queue.
        await asyncio.sleep(0.04)
        try:
            if outcome == "finished":
                self.status = "finished"
                self.completions.append({"conversation_id": self.conversation_id, "head": HEAD})
            else:
                self.status = "error"
                if outcome != "unstructured":
                    outcome = provider_error(outcome.code, outcome.detail,
                                             classification=outcome.classification)
                    self.history.append(outcome)
                    await self._broadcast(outcome)
            await self._broadcast(self._state_event())
        except Exception as exc:
            self.failures.append(exc)

    async def _handle(self, request):
        method, path = request.method, request.path
        self.requests.append((method, path))
        await asyncio.sleep(self.delays.get((method, path), 0))
        if path.startswith("/github/"):
            return await self._github(request)
        if method == "POST" and path == "/api/conversations":
            target = (await request.json())["conversation_id"]
        else:
            target = path.split("/")[3] if path.startswith("/api/conversations/") else ""
            if path.startswith("/sockets/events/"):
                target = path.rsplit("/", 1)[-1]
        if target in self.peers:
            return await self.peers[target]._handle(request)
        if path.startswith("/sockets/events/"):
            return await self._socket(request)
        if path == "/completion" and method == "POST":
            self.callbacks.append(await request.json())
            return web.json_response({})

        if path.startswith("/v1/kv/"):
            if request.headers.get("Authorization") != "Bearer synthetic-kv":
                return web.json_response({}, status=401)
            key = path.removeprefix("/v1/kv/")
            if method == "GET":
                if key not in self.records:
                    return web.json_response({}, status=404)
                return web.json_response({"value": self.records[key]})
            if method == "PUT":
                self.records[key] = await request.json()
                return web.json_response({"ok": True})
        if request.headers.get("X-Session-API-Key") != "synthetic-session":
            return web.json_response({}, status=401)
        if path == "/api/settings/secrets":
            return web.json_response({"secrets": []})
        base = f"/api/conversations/{self.conversation_id}"
        if method == "POST" and path == "/api/conversations":
            payload = await request.json()
            self.creates.append(payload)
            if payload["conversation_id"] != self.conversation_id:
                return web.json_response({}, status=400)
            self.status = "idle"
            initial = payload.get("initial_message")
            if initial:
                self.messages.append(initial)
                self.history.append(user_message(initial["content"]))
                if initial.get("run"):
                    await self._begin_run("initial_message")
            return web.json_response(self.info(), status=201)
        if self.status is None:
            return web.json_response({"detail": "Synthetic missing conversation"}, status=404)
        if method == "GET" and path == base:
            return web.json_response(self.info())
        if method == "GET" and path == base + "/events/search":
            return web.json_response({
                "items": [event.model_dump(mode="json") for event in self.history],
                "next_page_id": None,
            })
        if method == "POST" and path == base + "/events":
            payload = await request.json()
            self.messages.append(payload)
            event = user_message(payload["content"])
            self.history.append(event)
            await self._broadcast(event)
            return web.json_response({})
        if method == "POST" and path == base + "/secrets":
            await request.json()
            return web.json_response({})
        if method == "POST" and path == base + "/run":
            if self.outcomes and self.outcomes[0] == "busy":
                self.outcomes.pop(0)
                return web.json_response({}, status=409)
            return await self._begin_run("run", request)
        self.failures.append(AssertionError(f"Unexpected fake endpoint: {method} {path}"))
        return web.json_response({}, status=404)


    async def _github(self, request):
        method = request.method
        path = request.path.removeprefix("/github")
        if request.headers.get("Authorization") != "Bearer synthetic-review":
            return web.json_response({}, status=401)
        if method == "GET":
            if path == "/user":
                return web.json_response({"login": self.github_login})
            if path == "/repos/example/repository":
                return web.json_response({"id": "example/repository"})
            if path == "/repos/example/repository/pulls/1":
                return web.json_response(self.github_pr)
            if path.endswith("/pulls/1/reviews"):
                return web.json_response(self.github_reviews)
            if path.endswith("/issues/1/comments"):
                if self.on_comments:
                    self.on_comments()
                return web.json_response(self.github_comments)
            if path.endswith("/issues/1/events"):
                return web.json_response([{
                    "id": 1, "event": "review_requested",
                    "requested_reviewer": {"login": "all-hands-bot"},
                    "created_at": "2026-01-01T00:00:00Z",
                }])
        if method == "POST" and path.endswith("/issues/1/comments"):
            payload = await request.json()
            self.github_writes.append((method, path, payload))
            comment = dict(payload, id=len(self.github_comments) + 1,
                           user={"login": "all-hands-bot"})
            self.github_comments.append(comment)
            return web.json_response(comment, status=201)
        self.failures.append(AssertionError(f"Unexpected GitHub boundary: {method} {path}"))
        return web.json_response({}, status=404)


class RecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(
            dispatcher_module, "_PROVIDER_RETRY_DELAYS", (0, 0), create=True,
        ))
        # Only backoff/poll time is accelerated; native SDK methods remain real.
        self.stack.enter_context(patch.object(
            dispatcher_module, "sleep", lambda seconds: time.sleep(min(seconds, .02)),
            create=True,
        ))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))

    def tearDown(self):
        self.assertEqual(blocked_connections, [])

    def services(self, outcomes, *, status=None, history=None, retries=0, delivery=DELIVERY):
        record = None if status is None else {
            "subject": SUBJECT, "conversation_id": CONVERSATION_ID,
            "delivery": delivery, "head": HEAD, "provider_retries": retries,
        }
        server = self.stack.enter_context(LoopbackServices(
            outcomes, status=status,
            history=history or ([user_message()] if status else []), record=record,
        ))
        self.stack.enter_context(patch.dict(os.environ, {
            "AGENT_SERVER_URL": server.url,
            "SESSION_API_KEY": "synthetic-session",
            "AUTOMATION_AGENT_PROFILE_ID": PROFILE,
            "AUTOMATION_EVENT_PAYLOAD": json.dumps({"automation_id": AUTOMATION}),
            "AUTOMATION_API_URL": server.url,
            "AUTOMATION_KV_TOKEN": "synthetic-kv",
            "WORKSPACE_BASE": "/workspace",
        }))
        return server

    def worker_services(self, outcomes):
        server = self.services(outcomes)
        workspace = self.stack.enter_context(tempfile.TemporaryDirectory())
        config = Path(workspace) / "config.json"
        config.write_text(json.dumps({
            "repos": ["example/repository"],
            "github_token_secret": "GITHUB_TOKEN_REVIEW",
            "trigger_reviewer": "all-hands-bot",
        }))
        self.stack.enter_context(patch.dict(os.environ, {
            "AUTOMATION_CONFIG_PATH": str(config),
            "GITHUB_TOKEN_REVIEW": "synthetic-review",
            "WORKSPACE_BASE": workspace,
            "AUTOMATION_CALLBACK_URL": server.url + "/completion",
            "AUTOMATION_EVENT_PAYLOAD": json.dumps({
                "automation_id": AUTOMATION,
                "event": {"payload": {
                    "action": "review_requested", "pull_request": {"number": 1},
                    "repository": {"full_name": "example/repository"},
                    "requested_reviewer": {"login": "all-hands-bot"},
                }},
            }),
        }))
        self.stack.enter_context(patch.object(sys, "argv", ["worker.py"]))

        def github_urlopen(request, **kwargs):
            parts = urlsplit(request.full_url)
            self.assertEqual(parts.netloc, "api.github.com")
            target = server.url + "/github" + parts.path
            if parts.query:
                target += "?" + parts.query
            return urlopen(Request(target, data=request.data,
                                   headers=dict(request.header_items()),
                                   method=request.get_method()), **kwargs)

        # Redirect only GitHub's transport. Worker, discovery and SDK stay real.
        self.stack.enter_context(patch.dict(worker_module.github_request.__globals__,
                                           urlopen=github_urlopen))
        return server

    def dispatcher(self):
        dispatcher = self.stack.enter_context(dispatcher_module.AgentConversationDispatcher())
        self.addCleanup(dispatcher._workspace.reset_client)
        return dispatcher

    def submit(self, dispatcher, *, guard=lambda started_at: True, **kwargs):
        args = dict(subject=SUBJECT, delivery=DELIVERY, prompt=PROMPT, head=HEAD)
        if "can_retry" in inspect.signature(dispatcher.deliver).parameters:
            args["can_retry"] = guard
        args.update(kwargs)
        return dispatcher.deliver(**args)

    def deliver(self, **kwargs):
        with dispatcher_module.AgentConversationDispatcher() as dispatcher:
            self.addCleanup(dispatcher._workspace.reset_client)
            return self.submit(dispatcher, **kwargs)

    def wait(self, server):
        end = time.monotonic() + 2
        while server.status in (None, "running", "idle") and time.monotonic() < end:
            time.sleep(.01)

    def assert_one_prompt(self, server):
        self.assertEqual(len(server.messages), 1, "Recovery must not append a duplicate task")
        self.assertEqual(len(server.creates), 1)


class RecoveryRegressionTests(RecoveryTestCase):
    def test_transient_provider_error_recovers_without_another_event(self):
        server = self.services([
            provider_error("InternalServerError", "<html>Service Temporarily Unavailable</html>"),
            "finished",
        ])
        self.deliver()
        self.wait(server)
        self.assertEqual(server.status, "finished", "Initial dispatch stranded a transient provider error")
        self.assert_one_prompt(server)
        self.assertEqual(len(server.runs), 2)
        self.assertEqual(server.runs[1]["record"]["provider_retries"], 1)

    def test_error_redelivery_resumes_without_repeating_task(self):
        server = self.services(["finished"], status="error", history=[
            user_message(), provider_error(),
        ])
        self.deliver()
        self.wait(server)
        self.assertEqual(server.status, "finished")
        users = [e for e in server.history if isinstance(e, events.MessageEvent)]
        self.assertEqual(len(users), 1, "ERROR recovery repeated the initial task")


class DispatchControlTests(RecoveryTestCase):
    def test_interrupted_dispatch_never_starts_recovery(self):
        server = self.services([provider_error(), "finished"])
        with self.assertRaises(KeyboardInterrupt):
            with dispatcher_module.AgentConversationDispatcher() as dispatcher:
                self.addCleanup(dispatcher._workspace.reset_client)
                self.submit(dispatcher)
                self.wait(server)
                raise KeyboardInterrupt
        self.assertEqual(len(server.runs), 1)

    def test_healthy_delivery(self):
        server = self.services(["finished"])
        result = self.deliver()
        self.wait(server)
        self.assertEqual(result["disposition"], "created")
        self.assertEqual(server.status, "finished")
        self.assertEqual(len(server.runs), 1)
        self.assert_one_prompt(server)

    def test_finished_delivery_deduplicates(self):
        server = self.services([], status="finished")
        result = self.deliver()
        self.assertEqual(result["disposition"], "deduplicated")
        self.assertEqual(server.runs, [])
        self.assertEqual(server.messages, [])

    def test_running_same_head_new_trigger_is_not_started_twice(self):
        server = self.services([], status="running")
        result = self.deliver(delivery="another-trigger:" + HEAD)
        self.assertEqual(result["disposition"], "in_progress")
        self.assertEqual(server.runs, [])
        self.assertEqual(server.messages, [])

    def test_scan_keeps_global_limit_and_nonblocking_dispatch(self):
        server = self.services(["hang"])
        intake = worker_module.ReviewIntake()
        dispatcher = self.dispatcher()
        calls = []
        for number in (3, 1, 2):
            def start(number=number):
                calls.append(number)
                if number == 1:
                    return self.submit(dispatcher)
                self.assertEqual(server.status, "running")
                return {"disposition": "created"}
            intake.register({
                "priority": 0, "created_at": "2026-01-01T00:00:00Z",
                "repository": "example/repository", "number": number,
                "config": {"max_new_per_run": 2}, "start": start,
            })
        intake.drain()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(intake._started, 2)
        if hasattr(dispatcher, "_deadline"):
            dispatcher._deadline = time.monotonic()


class RecoverySafetyTests(RecoveryTestCase):
    def test_persistent_outage_exhausts_budget_and_reports_failure(self):
        server = self.services([provider_error()] * 3)
        with self.assertRaisesRegex(RuntimeError, "Review recovery failed"):
            self.deliver()
        self.assertEqual(len(server.runs), 3)
        self.assertEqual(server.records[server.records[STATE_KEY]["recovery_key"]]["provider_retries"], 2)
        self.assertIn('"outcome": "review_execution_failed"', self.output.getvalue())
        with self.assertRaisesRegex(RuntimeError, "review execution failed"):
            self.deliver()
        self.assertEqual(len(server.runs), 3, "Redelivery must not replenish the retry budget")
        self.assert_one_prompt(server)
        server.outcomes.append("finished")
        self.deliver(delivery="new-trigger:" + HEAD)
        self.assertEqual(server.status, "finished")
        self.assertEqual(len(server.runs), 4)
        self.assertEqual(len(server.messages), 2)

    def test_permanent_and_unstructured_errors_are_not_restarted(self):
        for error in (
            provider_error("LLMAuthenticationError", "invalid api key"),
            provider_error("LLMRateLimitError", "insufficient_quota"),
            provider_error("KeyError", "service temporarily unavailable"),
            provider_error("UnknownFailure", "Synthetic failure"),
            "unstructured",
        ):
            with self.subTest(error=str(error)), ExitStack() as stack:
                saved = self.stack
                self.stack = stack
                try:
                    server = self.services([error])
                    with self.assertRaisesRegex(RuntimeError, "Review recovery failed"):
                        self.deliver()
                    self.assertEqual(len(server.runs), 1)
                finally:
                    self.stack = saved

    def test_old_turn_error_and_unmatched_action_are_not_restarted(self):
        for history in (
            [user_message(), provider_error(), user_message()],
            [user_message(), pending_action(), provider_error()],
        ):
            with self.subTest(history=history), ExitStack() as stack:
                saved = self.stack
                self.stack = stack
                try:
                    server = self.services([], status="error", history=history)
                    with self.assertRaises(RuntimeError):
                        self.deliver()
                    self.assertEqual(server.runs, [])
                finally:
                    self.stack = saved

    def test_fresh_guard_vetoes_restart(self):
        server = self.services([provider_error()])
        self.deliver(guard=lambda started_at: False)
        self.assertEqual(len(server.runs), 1)
        self.assertIn("recovery_skipped_pr_changed_or_reviewed", self.output.getvalue())

    def test_ambiguous_run_write_is_not_repeated(self):
        server = self.services([provider_error(), "transport", "finished"])
        with self.assertRaises(RuntimeError):
            self.deliver()
        self.assertEqual(len(server.runs), 2)
        self.assertEqual(server.records[server.records[STATE_KEY]["recovery_key"]]["provider_retries"], 1)

    def test_accepted_restart_with_lost_response_is_not_sent_twice(self):
        server = self.services([provider_error(), "accepted_transport", "finished"])
        with self.assertRaises(RuntimeError):
            self.deliver()
        self.wait(server)
        self.assertEqual(len(server.runs), 2)
        self.assertEqual(server.status, "finished")
        self.assert_one_prompt(server)

    def test_shared_window_includes_discovery_and_does_not_cancel_running_work(self):
        server = self.services(["hang"])
        with dispatcher_module.AgentConversationDispatcher() as dispatcher:
            self.addCleanup(dispatcher._workspace.reset_client)
            deadline = dispatcher._deadline
            self.submit(dispatcher)
            self.assertEqual(dispatcher._deadline, deadline)
            dispatcher._deadline = time.monotonic() + .05
            start = time.monotonic()
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(server.status, "running")
        self.assertIn("execution_unconfirmed", self.output.getvalue())

    def test_retries_do_not_start_without_trigger_and_guard_overhead_reserve(self):
        server = self.services([], status="error", history=[user_message(), provider_error()])
        with self.assertRaises(RuntimeError):
            with dispatcher_module.AgentConversationDispatcher() as dispatcher:
                self.addCleanup(dispatcher._workspace.reset_client)
                self.submit(dispatcher)
                dispatcher._deadline = time.monotonic() + .05
        self.assertEqual(server.runs, [])

    def test_backoff_and_counter_survive_redelivery(self):
        server = self.services([], status="error", history=[user_message(), provider_error()])
        server.records[STATE_KEY]["provider_retries"] = 1
        server.records[STATE_KEY]["provider_retry_at"] = time.time() + 100
        with self.assertRaises(RuntimeError):
            with dispatcher_module.AgentConversationDispatcher() as dispatcher:
                self.addCleanup(dispatcher._workspace.reset_client)
                self.submit(dispatcher)
                dispatcher._deadline = time.monotonic() + .05
        self.assertEqual(server.runs, [])
        self.assertEqual(server.records[STATE_KEY]["provider_retries"], 1)
        self.assertGreater(server.records[STATE_KEY]["provider_retry_at"], time.time())

    def test_recovery_failure_reaches_native_completion_callback(self):
        server = self.services([provider_error()] * 3)
        with patch.dict(os.environ, {"AUTOMATION_CALLBACK_URL": server.url + "/completion"}):
            with self.assertRaises(RuntimeError):
                self.deliver()
        self.assertEqual([item["status"] for item in server.callbacks], ["FAILED"])

    def test_busy_run_response_is_reconciled_and_bounded(self):
        server = self.services([provider_error(), "busy", "finished"])
        self.deliver()
        self.assertEqual(server.status, "finished")
        self.assertEqual(len(server.runs), 2)
        self.assertEqual(server.runs[-1]["record"]["provider_retries"], 2)

    def test_active_error_supervisor_is_not_duplicated(self):
        server = self.services([], status="error", history=[user_message(), provider_error()])
        with dispatcher_module.AgentConversationDispatcher() as dispatcher:
            self.addCleanup(dispatcher._workspace.reset_client)
            self.submit(dispatcher)
            result = self.deliver(delivery="another-trigger:" + HEAD)
            self.assertEqual(result["disposition"], "in_progress")
            dispatcher._deadline = time.monotonic()
        self.assertEqual(server.runs, [])

    def test_superseded_supervisor_never_overwrites_new_revision(self):
        server = self.services([], status="error", history=[user_message(), provider_error()])
        with dispatcher_module.AgentConversationDispatcher() as dispatcher:
            self.addCleanup(dispatcher._workspace.reset_client)
            self.submit(dispatcher)
            replacement = dict(server.records[STATE_KEY],
                               head="new-head", delivery="new-delivery", recovery_owner="new-owner")
            server.records[STATE_KEY] = replacement
        self.assertEqual(server.records[STATE_KEY], replacement)
        self.assertEqual(server.runs, [])
        self.assertIn("recovery_superseded", self.output.getvalue())

    def test_multiple_jobs_recover_after_all_dispatches(self):
        server = self.services([provider_error(), "finished"])
        peer = LoopbackServices([provider_error(), "finished"], subject="example/repository:pr:2")
        peer.loop.close()
        peer.loop = server.loop
        peer.records = server.records
        server.peers[peer.conversation_id] = peer
        with dispatcher_module.AgentConversationDispatcher() as dispatcher:
            self.addCleanup(dispatcher._workspace.reset_client)
            self.submit(dispatcher)
            self.assertEqual(len(server.runs), 1)
            self.submit(dispatcher, subject=peer.subject)
            self.assertEqual(len(server.runs), 1)
            self.assertEqual(len(peer.runs), 1)
        self.assertEqual([server.status, peer.status], ["finished", "finished"])
        self.assertEqual([len(server.runs), len(peer.runs)], [2, 2])

    def test_worker_recovery_distinguishes_prior_pass_from_current_publication(self):
        for published_now in (False, True):
            with self.subTest(published_now=published_now), ExitStack() as stack:
                saved = self.stack
                self.stack = stack
                try:
                    server = self.services([provider_error(), "finished"])
                    reviewer = object.__new__(worker_module.PullRequestReviewer)
                    reviewer.repository, reviewer.token = "example/repository", "synthetic"
                    reviewer.token_name = "GITHUB_TOKEN_REVIEW"
                    reviewer.config = {"trigger_reviewer": "all-hands-bot"}
                    pr = {"number": 1, "state": "open", "head": {"sha": HEAD}}
                    def request(token, method, path, **kwargs):
                        self.assertEqual(method, "GET")
                        if path == "/user":
                            return {"login": "all-hands-bot"}, {}
                        if not path.endswith("/reviews"):
                            return pr, {}
                        submitted = time.time() if published_now else time.time() - 60
                        return [{
                            "commit_id": HEAD, "user": {"login": "all-hands-bot"},
                            "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(submitted)),
                        }], {}
                    with patch.object(worker_module, "github_request", request):
                        with dispatcher_module.AgentConversationDispatcher() as dispatcher:
                            self.addCleanup(dispatcher._workspace.reset_client)
                            reviewer.dispatcher = dispatcher
                            reviewer._start_review(
                                "example/repository", pr, {"id": 1}, HEAD, None,
                            )
                    self.assertIsInstance(server.records[STATE_KEY]["review_started_at"], float)
                    self.assertEqual(len(server.runs), 1 if published_now else 2)
                    self.assertEqual(server.status, "error" if published_now else "finished")
                    self.assert_one_prompt(server)
                finally:
                    self.stack = saved

    def test_worker_guard_is_bounded_and_fails_closed_on_full_page(self):
        reviewer = object.__new__(worker_module.PullRequestReviewer)
        reviewer.repository, reviewer.token = "example/repository", "synthetic"
        reviewer.config = {"trigger_reviewer": "all-hands-bot"}
        cases = [
            ({"state": "open", "head": {"sha": HEAD}}, [], True),
            ({"state": "closed", "head": {"sha": HEAD}}, [], False),
            ({"state": "open", "head": {"sha": "new"}}, [], False),
            ({"state": "open", "head": {"sha": HEAD}}, [
                {"commit_id": HEAD, "user": {"login": "ALL-HANDS-BOT"}},
            ], False),
            ({"state": "open", "head": {"sha": HEAD}}, [{}] * 100, False),
            ({"state": "open", "head": {"sha": HEAD}}, [
                {"commit_id": HEAD, "user": {"login": "all-hands-bot"},
                 "submitted_at": "2026-10-28T09:00:00Z"},
            ], True, 1793181600.9),
            ({"state": "open", "head": {"sha": HEAD}}, [
                {"commit_id": HEAD, "user": {"login": "all-hands-bot"},
                 "submitted_at": "2026-10-28T10:00:00Z"},
            ], False, 1793181600.9),
        ]
        for case in cases:
            pr, reviews, expected, *started_at = case
            calls = []
            def request(token, method, path, **kwargs):
                self.assertEqual(method, "GET")
                self.assertEqual(kwargs["timeout"], 10)
                calls.append(path)
                if path == "/user":
                    return {"login": "all-hands-bot"}, {}
                return (reviews if path.endswith("/reviews") else pr), {}
            # Only the GitHub wire boundary is replaced; no real writes are allowed.
            with patch.object(worker_module, "github_request", request):
                self.assertEqual(reviewer._can_retry_review(1, HEAD, *started_at), expected)
                self.assertLessEqual(len(calls), 3)


class WorkerReportingTests(RecoveryTestCase):
    def test_persistent_provider_outage_posts_one_nonverdict_status(self):
        server = self.worker_services([provider_error()] * 3)
        with self.assertRaises(RuntimeError):
            worker_module.run_worker()
        self.assertEqual(len(server.runs), 3)
        self.assertEqual(len(server.github_writes), 1)
        body = server.github_writes[0][2]["body"]
        self.assertIn("not a review verdict", body)
        self.assertIn(HEAD, body)
        self.assertIn("AI agent (OpenHands)", body)
        self.assertEqual([x["status"] for x in server.callbacks], ["FAILED"])
        self.assert_one_prompt(server)

    def test_recovered_provider_outage_does_not_post_failure(self):
        server = self.worker_services([provider_error(), "finished"])
        worker_module.run_worker()
        self.assertEqual(server.status, "finished")
        self.assertEqual(len(server.runs), 2)
        self.assertEqual(server.github_writes, [])
        self.assert_one_prompt(server)

    def test_failure_note_is_suppressed_when_replaced_during_comment_lookup(self):
        server = self.worker_services([provider_error()] * 3)
        def replacement():
            server.records[STATE_KEY]["recovery_owner"] = "replacement-attempt"
        server.on_comments = replacement
        with self.assertRaises(RuntimeError):
            worker_module.run_worker()
        self.assertEqual(server.records[STATE_KEY]["recovery_owner"], "replacement-attempt")
        self.assertEqual(server.github_writes, [])

    def test_notification_waits_for_other_review_recovery(self):
        server = self.worker_services([provider_error("LLMAuthenticationError")])
        peer = LoopbackServices([provider_error(), "finished"], subject="example/repository:pr:2")
        peer.loop.close()
        peer.loop = server.loop
        peer.records = server.records
        server.peers[peer.conversation_id] = peer
        observed = []
        server.on_comments = lambda: observed.append(peer.status)
        server.delays[("GET", "/github/repos/example/repository/issues/1/comments")] = .2
        reviewer = worker_module.PullRequestReviewer(
            github_token_secret="GITHUB_TOKEN_REVIEW", repository="example/repository",
        )
        with self.assertRaises(RuntimeError):
            with dispatcher_module.AgentConversationDispatcher() as dispatcher:
                self.submit(dispatcher, on_failure=lambda started, current, deadline:
                            reviewer._report_review_failure(1, HEAD, started, current, deadline))
                self.submit(dispatcher, subject=peer.subject)
        self.assertEqual(peer.status, "finished")
        self.assertEqual(observed, ["finished"])
        self.assertEqual(len(server.github_writes), 1)

    def test_notification_reads_cannot_outlive_the_shared_deadline(self):
        paths = (
            "/repos/example/repository/issues/1/comments",
            "/repos/example/repository/pulls/1",
            "/repos/example/repository/pulls/1/reviews",
            "/user",
        )
        for path in paths:
            with self.subTest(path=path), ExitStack() as stack:
                saved, self.stack = self.stack, stack
                try:
                    server = self.worker_services([])
                    # A comment forces the uncached publishing-account read too.
                    server.github_comments = [{"body": "Human discussion", "user": {"login": "human"}}]
                    server.delays[("GET", "/github" + path)] = 1
                    reviewer = worker_module.PullRequestReviewer(
                        github_token_secret="GITHUB_TOKEN_REVIEW", repository="example/repository",
                    )
                    started = time.monotonic()
                    with self.assertRaises((TimeoutError, URLError)):
                        reviewer._report_review_failure(
                            1, HEAD, time.time(), lambda: True, deadline=started + .15,
                        )
                    self.assertLess(time.monotonic() - started, .8)
                    self.assertEqual(server.github_writes, [])
                finally:
                    self.stack = saved

    def test_notification_does_not_post_after_final_state_check_uses_remaining_time(self):
        server = self.worker_services([])
        reviewer = worker_module.PullRequestReviewer(
            github_token_secret="GITHUB_TOKEN_REVIEW", repository="example/repository",
        )
        def last_check():
            time.sleep(.2)
            return True
        with self.assertRaises(TimeoutError):
            reviewer._report_review_failure(
                1, HEAD, time.time(), last_check, deadline=time.monotonic() + .15,
            )
        self.assertEqual(server.github_writes, [])

    def test_retry_guard_uses_publishing_account_not_trigger_account(self):
        server = self.worker_services([])
        server.github_login = "another-publishing-bot"
        server.github_reviews = [{
            "commit_id": HEAD, "user": {"login": server.github_login},
            "submitted_at": "2999-01-01T00:00:00Z",
        }]
        reviewer = worker_module.PullRequestReviewer(
            github_token_secret="GITHUB_TOKEN_REVIEW", repository="example/repository",
        )
        self.assertNotEqual(reviewer.github_login, reviewer.trigger_reviewer)
        self.assertFalse(reviewer._can_retry_review(1, HEAD, time.time()))
        reviewer._report_review_failure(1, HEAD, time.time(), lambda: True)
        self.assertEqual(server.github_writes, [])

    def test_failure_notification_deduplicates_and_respects_changed_state(self):
        server = self.worker_services([])
        reviewer = worker_module.PullRequestReviewer(
            github_token_secret="GITHUB_TOKEN_REVIEW", repository="example/repository",
        )
        reviewer._report_review_failure(1, HEAD, time.time(), lambda: True)
        started = time.time()
        reviewer._report_review_failure(1, HEAD, started, lambda: True)
        reviewer._report_review_failure(1, HEAD, started, lambda: True)
        self.assertEqual(len(server.github_writes), 2)
        for change in ("moved", "reviewed", "closed"):
            server.github_pr["head"]["sha"] = HEAD
            server.github_pr["state"] = "open"
            server.github_reviews = []
            if change == "moved":
                server.github_pr["head"]["sha"] = "new-head"
            elif change == "closed":
                server.github_pr["state"] = "closed"
            else:
                server.github_reviews = [{
                    "commit_id": HEAD, "user": {"login": "all-hands-bot"},
                    "submitted_at": "2999-01-01T00:00:00Z",
                }]
            reviewer._report_review_failure(1, HEAD, started, lambda: True)
        self.assertEqual(len(server.github_writes), 2)


class WorkerRuntimeTests(RecoveryTestCase):
    def test_complete_worker_bounds_slow_discovery_dispatch_and_cleanup(self):
        dispatcher_module._register_tools()
        for boundary in ("bootstrap", "discovery", "dispatch", "cleanup", "recovery"):
            with self.subTest(boundary=boundary), ExitStack() as stack:
                saved = self.stack
                self.stack = stack
                try:
                    server = self.worker_services(["finished" if boundary == "cleanup" else "hang"])
                    if boundary == "bootstrap":
                        server.delays[("GET", "/api/settings/secrets")] = 3
                    elif boundary == "discovery":
                        server.delays[("GET", "/github/repos/example/repository")] = 3
                    elif boundary == "dispatch":
                        server.delays[("POST", "/api/conversations")] = 3
                    elif boundary == "cleanup":
                        server.heartbeat_period = 3
                        server.mute_updates = True
                    else:
                        server.delays[("GET", "/api/conversations/" + CONVERSATION_ID)] = .1
                    stack.enter_context(patch.object(worker_module, "WORKER_TIMEOUT_SECONDS", .6))
                    before = time.monotonic()
                    with self.assertRaises(worker_module.WorkerDeadlineExceeded):
                        worker_module.run_worker()
                    elapsed = time.monotonic() - before
                    self.assertLess(elapsed, 1.5, f"Unbounded {boundary}: {elapsed:.2f}s")
                    self.assertEqual([x["status"] for x in server.callbacks], ["FAILED"])
                    self.assertEqual(server.github_writes, [])
                    if boundary in ("bootstrap", "discovery", "dispatch"):
                        self.assertEqual(server.creates, [])
                    if boundary == "bootstrap":
                        self.assertNotIn(("GET", "/github/repos/example/repository"), server.requests)
                    self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))
                finally:
                    self.stack = saved


if __name__ == "__main__":
    unittest.main()
