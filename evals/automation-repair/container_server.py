import argparse
import hmac
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from reviewer_world import World


def serve(args):
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.task_tracker import TaskTrackerTool
    from openhands.tools.terminal import TerminalTool

    world = World(json.loads(Path(args.case).read_text()))
    lock = threading.Lock()
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
            try:
                if private and self.path == "/snapshot":
                    with lock:
                        result = world.snapshot()
                elif private and self.path == "/tool":
                    tool = tools[body["tool"]]
                    action = tool.action_type.model_validate(body["action"])
                    result = tool.executor(action).model_dump(mode="json")
                elif private:
                    status, result = 404, {"error": "Unknown replay operation"}
                else:
                    with lock:
                        status, result = world.request(
                            self.command,
                            self.path,
                            body,
                            self.headers.get("Accept", ""),
                        )
                data = (
                    result.encode()
                    if isinstance(result, str)
                    else json.dumps(result).encode()
                )
                self.send_response(status)
                self.send_header(
                    "Content-Type",
                    "text/plain" if isinstance(result, str) else "application/json",
                )
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
