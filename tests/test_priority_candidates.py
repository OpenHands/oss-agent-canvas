import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/high-priority-issue-notification/scripts"
REPO = "OpenHands/OpenHands"
SDK_REPO = "OpenHands/software-agent-sdk"


def issue(number, label="priority:medium", repo=REPO):
    return {
        "id": number,
        "number": number,
        "title": f"Issue {number}",
        "body": f"Description for #{number}",
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "labels": [{"name": label}],
        "user": {"login": "reporter"},
        "assignees": [],
        "comments": 2,
        "reactions": {"total_count": 3},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
    }


@contextlib.contextmanager
def github_server(datasets, *, transform=None, stream_delay=0):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            requests.append((self.command, parsed.path, params, dict(self.headers)))
            status = 200
            if "/api/settings/secrets/" in parsed.path:
                data = b"fixture-token"
            elif parsed.path == "/search/issues":
                query = params["q"][0]
                repo = query.split("repo:", 1)[1].split()[0]
                label = query.split('label:"', 1)[1].split('"')[0]
                page = int(params.get("page", ["1"])[0])
                items = datasets.get((repo, label), [])
                payload = {
                    "total_count": len(items),
                    "incomplete_results": False,
                    "items": items[(page - 1) * 100:page * 100],
                }
                if transform:
                    payload = transform(payload, page)
                if isinstance(payload, tuple):
                    status, payload = payload
                data = json.dumps(payload).encode()
            else:
                self.send_error(404)
                return
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if stream_delay:
                for byte in data:
                    time.sleep(stream_delay)
                    try:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            else:
                self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    original_urlopen = urllib.request.urlopen

    def local_urlopen(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        if url.startswith("https://api.github.com/"):
            request = urllib.request.Request(
                base + url.removeprefix("https://api.github.com"),
                headers=dict(request.header_items()),
                method=request.get_method(),
            )
        elif not url.startswith(base + "/"):
            raise AssertionError(f"Unexpected external request: {url}")
        return original_urlopen(request, *args, **kwargs)

    try:
        with patch("urllib.request.urlopen", side_effect=local_urlopen):
            yield base, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Runner:
    """Stub only SDK/Slack boundaries; exercise the real runner and HTTP fetches."""

    def __init__(self, directory, requests):
        self.directory = directory
        self.requests = requests
        self.uploads = {}
        self.prompts = []
        self.requests_at_send = []
        self.exits = []
        self.events = []
        self.upload_success = True
        self.conversations = 0

    def modules(self):
        runner = self

        class Workspace:
            def __init__(self, **kwargs):
                runner.workspace_kwargs = kwargs
                self.working_dir = kwargs.get("working_dir", runner.directory)
                self.client = types.SimpleNamespace(patch=lambda *_a, **_k: None)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                runner.exits.append(args[0])

            def load_skills_from_agent_server(self, **_kwargs):
                return [], None

            def get_llm(self, **_kwargs):
                return types.SimpleNamespace(model="fixture-model", api_key="fixture-key")

            def get_secrets(self):
                return {"GITHUB_TOKEN": "fixture-token"}

            def get_mcp_config(self):
                return {}

            def file_upload(self, source, destination):
                if runner.upload_success:
                    Path(destination).write_bytes(source)
                    runner.uploads[destination] = json.loads(source)
                return types.SimpleNamespace(success=runner.upload_success)

        class Conversation:
            def __init__(self, **kwargs):
                runner.conversations += 1
                self.id = "fixture-conversation"
                self.conversation_stats = types.SimpleNamespace(
                    get_combined_metrics=lambda: types.SimpleNamespace(accumulated_cost=0)
                )

            def update_secrets(self, _secrets):
                runner.events.append("secrets")

            def send_message(self, prompt):
                runner.prompts.append(prompt)
                runner.requests_at_send = list(runner.requests)
                runner.events.append("send")

            def run(self):
                runner.events.append("run")

            def close(self):
                runner.events.append("close")

        def module(name, **attrs):
            result = types.ModuleType(name)
            result.__dict__.update(attrs)
            return result

        return {
            "openhands.sdk": module("openhands.sdk", Conversation=Conversation,
                                     RemoteConversation=Conversation),
            "openhands.sdk.workspace.remote.base": module(
                "openhands.sdk.workspace.remote.base", RemoteWorkspace=Workspace),
            "openhands.sdk.mcp.config": module("openhands.sdk.mcp.config",
                                              coerce_mcp_config=lambda x: x),
            "openhands.tools.preset.default": module(
                "openhands.tools.preset.default", get_default_agent=lambda **_k:
                types.SimpleNamespace(tools=[], condenser=None)),
            "openhands.workspace": module("openhands.workspace",
                                          OpenHandsCloudWorkspace=Workspace),
            "slack_directory": module("slack_directory", build_map=lambda *_a, **_k: {}),
        }

    def run(self, base, *, cloud=False):
        env = {"WORKSPACE_BASE": self.directory}
        if cloud:
            env.update(OPENHANDS_API_KEY="fixture-key", OPENHANDS_CLOUD_API_URL=base,
                       SANDBOX_ID="fixture-sandbox", OH_SESSION_API_KEYS_0="fixture-key")
        else:
            env.update(AGENT_SERVER_URL=base, OH_SESSION_API_KEYS_0="fixture-key")
        with (
            patch.dict(os.environ, env, clear=True),
            patch.dict(sys.modules, self.modules()),
            patch.object(sys, "path", [str(SCRIPTS), *sys.path]),
            patch("time.sleep"),
            patch("time.time", side_effect=range(10000)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(str(SCRIPTS / "main.py"), run_name="__main__")


class CandidateRegressionTests(unittest.TestCase):
    def test_all_114_candidates_are_available_before_ranking(self):
        items = [issue(n) for n in range(114, 0, -1)]
        with tempfile.TemporaryDirectory() as directory, github_server(
            {(REPO, "priority:medium"): items}
        ) as (base, requests):
            runner = Runner(directory, requests)
            runner.run(base)
            self.assertTrue(runner.uploads, "Ranking started without a complete candidate dataset")
            snapshot = next(iter(runner.uploads.values()))
            candidates = snapshot["repositories"][REPO]["issues"]
            self.assertEqual({x["number"] for x in candidates}, set(range(1, 115)))
            pages = [r[2]["page"][0] for r in runner.requests_at_send
                     if r[1] == "/search/issues" and 'label:"priority:medium"' in r[2]["q"][0]
                     and f"repo:{REPO} " in r[2]["q"][0]]
            self.assertEqual(pages, ["1", "2"])
            path = next(iter(runner.uploads))
            self.assertIn(path, runner.prompts[0])
            self.assertIn("114", runner.prompts[0])


class RunnerControlTests(unittest.TestCase):
    def test_local_runner_preserves_lifecycle_and_delivery_rules(self):
        with tempfile.TemporaryDirectory() as directory, github_server({}) as (base, requests):
            runner = Runner(directory, requests)
            runner.run(base)
            self.assertEqual(runner.events, ["secrets", "send", "run", "close"])
            self.assertEqual(runner.exits, [None])
            self.assertEqual(len(runner.prompts), 1)
            self.assertEqual(runner.workspace_kwargs["host"], base)
            prompt = runner.prompts[0]
            self.assertIn("C06QT0AGY4W", prompt)
            self.assertIn("C06R25BT5B2", prompt)
            self.assertIn("do not cross-post", prompt)
            self.assertIn("_Generated by OpenHands AI on behalf of the user._", prompt)
            self.assertIn("never comment on, label, close, assign", prompt)
            self.assertTrue(all(r[0] == "GET" for r in requests))


class CandidateFetchTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("candidate_fetch", SCRIPTS / "github_candidates.py")
        self.fetcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fetcher)

    def test_page_boundaries_and_authenticated_read_only_requests(self):
        for count in (0, 1, 100, 101, 1000):
            with self.subTest(count=count), github_server(
                {(REPO, "priority:medium"): [issue(n) for n in range(1, count + 1)]}
            ) as (_, requests):
                snapshot = self.fetcher.fetch_candidates("fixture-token")
                data = snapshot["repositories"][REPO]
                self.assertEqual(len(data["issues"]), count)
                search = data["searches"][1]
                self.assertEqual(search["fetched_count"], count)
                self.assertEqual(search["total_count"], count)
                pages = [r[2]["page"][0] for r in requests
                         if f"repo:{REPO} " in r[2]["q"][0]
                         and 'label:"priority:medium"' in r[2]["q"][0]]
                self.assertEqual(pages, [str(n) for n in range(1, max(1, (count + 99) // 100) + 1)])
                for method, _, params, headers in requests:
                    self.assertEqual(method, "GET")
                    self.assertIn("is:issue is:open", params["q"][0])
                    self.assertEqual(params["sort"], ["created"])
                    self.assertEqual(params["order"], ["asc"])
                    self.assertEqual(params["per_page"], ["100"])
                    self.assertEqual(headers["Authorization"], "Bearer fixture-token")
                    self.assertEqual(headers["Accept"], "application/vnd.github+json")

    def test_aliases_overlap_repository_collisions_and_pr_exclusion(self):
        sdk_issue = issue(1, repo=SDK_REPO)
        bot_issue = issue(2)
        bot_issue["user"]["login"] = "dependabot[bot]"
        pr = {**issue(3), "pull_request": {"url": "https://api.github.com/pulls/3"}}
        datasets = {
            (REPO, "priority:medium"): [issue(1), bot_issue, pr],
            (SDK_REPO, "priority:medium"): [sdk_issue],
            (SDK_REPO, "P0"): [sdk_issue],
            (SDK_REPO, "P1"): [sdk_issue, issue(2, "P1", SDK_REPO)],
        }
        with github_server(datasets):
            snapshot = self.fetcher.fetch_candidates("fixture-token")["repositories"]
        self.assertEqual([i["number"] for i in snapshot[REPO]["issues"]], [1, 2])
        self.assertEqual(snapshot[REPO]["issues"][0]["priority"], "medium")
        self.assertEqual(snapshot[SDK_REPO]["issues"][0]["priority"], "high")
        self.assertEqual(snapshot[SDK_REPO]["issues"][1]["priority"], "medium")
        self.assertEqual(snapshot[SDK_REPO]["issues"][0]["body"], sdk_issue["body"])
        # Bot importance remains the ranker's decision, as required by the prompt.
        self.assertEqual(snapshot[REPO]["issues"][1]["user"]["login"], "dependabot[bot]")

    def test_incomplete_or_invalid_searches_are_rejected(self):
        transforms = {
            "incomplete": lambda p, _: {**p, "incomplete_results": True},
            "missing completeness flag": lambda p, _: {k: v for k, v in p.items() if k != "incomplete_results"},
            "overflow": lambda p, _: {**p, "total_count": 1001},
            "short page": lambda p, _: {**p, "items": p["items"][:-1]},
            "count changed": lambda p, n: {**p, "total_count": 113} if n == 2 else p,
            "repeated page": lambda p, n: {**p, "items": [issue(i) for i in range(1, 15)]} if n == 2 else p,
            "missing count": lambda p, _: {k: v for k, v in p.items() if k != "total_count"},
            "negative count": lambda p, _: {**p, "total_count": -1},
            "boolean count": lambda p, _: {**p, "total_count": True},
            "invalid items": lambda p, _: {**p, "items": None},
            "invalid record": lambda p, _: {**p, "items": [None] * len(p["items"])},
            "invalid payload": lambda p, _: [],
        }
        items = [issue(i) for i in range(1, 115)]
        for name, transform in transforms.items():
            with self.subTest(name=name), github_server(
                {(REPO, "priority:high"): items}, transform=transform
            ), self.assertRaises(RuntimeError):
                self.fetcher.fetch_candidates("fixture-token")

    def test_missing_token_prevents_any_api_request(self):
        with github_server({}) as (_, requests):
            for token in (None, "", "  "):
                with self.subTest(token=token), self.assertRaisesRegex(RuntimeError, "GITHUB_TOKEN"):
                    self.fetcher.fetch_candidates(token)
            self.assertEqual(requests, [])

    def test_auth_and_rate_limit_failures_abort_without_retry(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), github_server(
                {}, transform=lambda _p, _n: (status, {"message": "API failure"})
            ) as (_, requests):
                with self.assertRaises(urllib.error.HTTPError):
                    self.fetcher.fetch_candidates("fixture-token")
                self.assertEqual(len(requests), 1)

    def test_slow_stream_cannot_extend_the_fetch_budget(self):
        with github_server({}, stream_delay=0.03), patch.object(
            self.fetcher, "FETCH_BUDGET", 0.2
        ):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "time budget"):
                self.fetcher.fetch_candidates("fixture-token")
            self.assertLess(time.monotonic() - started, 0.8)

    def test_socket_timeout_is_not_retried(self):
        with github_server({}, stream_delay=0.1) as (_, requests), patch.object(
            self.fetcher, "HTTP_TIMEOUT", 0.03
        ):
            with self.assertRaises(TimeoutError):
                self.fetcher.fetch_candidates("fixture-token")
            self.assertEqual(len(requests), 1)

    def test_one_time_budget_is_shared_across_label_searches(self):
        # A synthetic clock exercises expiry without sleeping through the budget.
        clock = unittest.mock.Mock(side_effect=[0, 0, 1, 2, 91])
        with github_server({}) as (_, requests), patch.object(
            self.fetcher, "time", types.SimpleNamespace(monotonic=clock)
        ), self.assertRaisesRegex(RuntimeError, "time budget"):
            self.fetcher.fetch_candidates("fixture-token")
        self.assertEqual(len(requests), 1)

    def test_deadline_is_checked_after_reading_a_page(self):
        clock = unittest.mock.Mock(side_effect=[0, 0, 91])
        with github_server({}) as (_, requests), patch.object(
            self.fetcher, "time", types.SimpleNamespace(monotonic=clock)
        ), self.assertRaisesRegex(RuntimeError, "time budget"):
            self.fetcher.fetch_candidates("fixture-token")
        self.assertEqual(len(requests), 1)


class RunnerSafetyTests(unittest.TestCase):
    def test_later_page_failure_prevents_ranking_and_delivery(self):
        def incomplete_second_page(payload, page):
            return {**payload, "incomplete_results": page == 2}

        with tempfile.TemporaryDirectory() as directory, github_server(
            {(REPO, "priority:medium"): [issue(n) for n in range(114)]},
            transform=incomplete_second_page,
        ) as (base, requests):
            runner = Runner(directory, requests)
            with self.assertRaisesRegex(RuntimeError, "Incomplete"):
                runner.run(base)
            self.assertEqual(runner.conversations, 0)
            self.assertEqual(runner.prompts, [])
            self.assertEqual(runner.uploads, {})
            self.assertEqual(runner.exits, [RuntimeError])

    def test_failed_upload_prevents_ranking_and_delivery(self):
        with tempfile.TemporaryDirectory() as directory, github_server({}) as (base, requests):
            runner = Runner(directory, requests)
            runner.upload_success = False
            with self.assertRaisesRegex(RuntimeError, "upload"):
                runner.run(base)
            self.assertEqual(runner.conversations, 0)
            self.assertEqual(runner.events, [])
            self.assertEqual(runner.exits, [RuntimeError])

    def test_cloud_handoff_uses_workspace_path_and_preserves_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory, github_server({}) as (base, requests):
            runner = Runner(directory, requests)
            runner.run(base, cloud=True)
            self.assertEqual(runner.events, ["secrets", "secrets", "send", "run", "close"])
            self.assertEqual(runner.exits, [None])
            self.assertTrue(runner.uploads)
            self.assertEqual(Path(next(iter(runner.uploads))).parent, Path(directory))
            self.assertIn(next(iter(runner.uploads)), runner.prompts[0])
            self.assertTrue(runner.workspace_kwargs["local_agent_server_mode"])
            self.assertEqual(runner.workspace_kwargs["cloud_api_url"], base)
            self.assertTrue(runner.workspace_kwargs["keep_alive"])


if __name__ == "__main__":
    unittest.main()
