import argparse
import hmac
import json
import io
import sys
from urllib.error import HTTPError
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from scenarios import world as make_world
from git_http import GitHTTP


def serve(args):
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.task_tracker import TaskTrackerTool
    from openhands.tools.terminal import TerminalTool

    world = make_world(json.loads(Path(args.case).read_text()))
    lock = threading.RLock()
    source = Path("/tmp/replay/automation-source")
    controlled = (source / "controlled_review.py").exists()
    gateway_requests = []
    git = GitHTTP("/tmp/workspace/repo", "/tmp/git-fixture", world)
    if controlled:
        sys.path.insert(0, str(source))
        from review_gateway import ReviewGateway
        from review_publication import ReviewPublisher

        def api(method, path, body=None):
            status, result = world.request(method, path, body)
            if status >= 400:
                raise HTTPError(
                    path,
                    status,
                    "Fixture rejection",
                    {},
                    io.BytesIO(json.dumps(result).encode()),
                )
            return result

        def archive():
            import subprocess

            return subprocess.check_output(
                [
                    "git",
                    "-C",
                    "/tmp/workspace/repo",
                    "archive",
                    "--format=tar.gz",
                    "--prefix=source/",
                    world.requested_sha,
                ]
            )

        gateway = ReviewGateway(
            "fixture/review",
            7,
            world.requested_sha,
            "replay-only-not-a-credential",
            lambda path: api("GET", path),
            archive,
        )
        publisher = ReviewPublisher(
            api,
            "fixture/review",
            7,
            world.requested_sha,
            "all-hands-bot",
            "frozen-eval",
            "/tmp/publication-state",
        )
    state = SimpleNamespace(
        workspace=SimpleNamespace(working_dir="/tmp/workspace/repo"),
        env_observation_persistence_dir="/tmp/tool-output",
        persistence_dir="/tmp/tracker",
        agent=SimpleNamespace(llm=SimpleNamespace(vision_is_active=lambda: False)),
    )
    tools = {
        name: cls.create(state)[0]
        for name, cls in (
            ("terminal", TerminalTool),
            ("file_editor", FileEditorTool),
            ("task_tracker", TaskTrackerTool),
        )
    }
    token = Path(args.token).read_text()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *values):
            pass

        def handle_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 2_000_000:
                self.send_error(413)
                return
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = {"invalid_json": raw.decode(errors="replace")[:2000]}
            private = self.server.server_port == 8766
            if private and not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.send_error(403)
                return
            status = 200
            response_headers = {}
            try:
                if private and self.path == "/snapshot":
                    with lock:
                        result = {
                            **world.snapshot(),
                            "gateway_requests": gateway_requests,
                        }
                elif private and self.path == "/tool":
                    tool = tools[body["tool"]]
                    action = tool.action_type.model_validate(body["action"])
                    result = tool.executor(action).model_dump(mode="json")
                elif private and self.path == "/precheck" and controlled:
                    with lock:
                        result = publisher.precheck()
                elif private and self.path == "/publish" and controlled:
                    with lock:
                        if not body.get("finished") or gateway.proposal is None:
                            result = {
                                "status": "analysis_incomplete",
                                "reason": "No accepted proposal from a finished agent",
                            }
                        else:
                            result = publisher.publish(gateway.proposal)
                elif private:
                    status, result = 404, {"error": "Unknown replay operation"}
                elif self.path.startswith("/fixture/review.git/"):
                    with lock:
                        status, response_headers, result = git.request(
                            self.command, self.path, raw, self.headers
                        )
                    self.send_response(status)
                    for key, value in response_headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(result)))
                    self.end_headers()
                    self.wfile.write(result)
                    return
                elif controlled:
                    with lock:
                        status, result = gateway.handle(
                            self.command,
                            self.path,
                            raw,
                            self.headers.get("Authorization"),
                        )
                        gateway_requests.append(
                            {
                                "method": self.command,
                                "path": self.path,
                                "status": status,
                            }
                        )
                else:
                    with lock:
                        status, result = world.request(
                            self.command,
                            self.path,
                            body,
                            self.headers.get("Accept", ""),
                        )
                        response_headers = dict(getattr(world, "response_headers", {}))
                data = (
                    result
                    if isinstance(result, bytes)
                    else result.encode()
                    if isinstance(result, str)
                    else json.dumps(result).encode()
                )
                self.send_response(status)
                self.send_header(
                    "Content-Type",
                    "text/plain" if isinstance(result, str) else "application/json",
                )
                for name, value in response_headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as error:
                data = json.dumps(
                    {"error": type(error).__name__, "detail": str(error)}
                ).encode()
                self.send_response(500)
                self.end_headers()
                self.wfile.write(data)

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_request

    public = ThreadingHTTPServer(("127.0.0.1", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    public.socket = context.wrap_socket(public.socket, server_side=True)
    threading.Thread(target=public.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8766), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("case", "token", "cert", "key"):
        parser.add_argument("--" + name, required=True)
    serve(parser.parse_args())
