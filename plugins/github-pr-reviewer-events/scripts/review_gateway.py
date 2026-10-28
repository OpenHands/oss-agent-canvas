import hmac
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit

from review_publication import validate_proposal


class ReviewGateway:
    def __init__(self, repository, number, head, token, read, archive):
        self.repository, self.number, self.head = repository, number, head
        self.token, self.read, self.archive = token, read, archive
        self._proposal = None
        self._lock = threading.Lock()

    @property
    def proposal(self):
        with self._lock:
            return json.loads(json.dumps(self._proposal))

    def handle(self, method, target, raw, authorization):
        if not hmac.compare_digest(authorization or "", "Bearer " + self.token):
            return 401, {"error": "Invalid review gateway credential"}
        url = urlsplit(target)
        if url.scheme or url.netloc or len(raw) > 64000:
            return 400, {"error": "Invalid gateway request"}
        if method == "GET" and url.path == "/client.py":
            return 200, Path(__file__).with_name("review_client.py").read_bytes()
        if method == "GET" and url.path == "/checkout" and not url.query:
            data = self.archive()
            if len(data) > 64_000_000:
                return 413, {"error": "Repository archive exceeds the byte limit"}
            return 200, data
        if method == "GET" and url.path.startswith("/github/"):
            path = unquote(url.path[len("/github") :])
            prefix = "/repos/" + self.repository
            if ".." in path.split("/") or not (
                path.casefold() == prefix.casefold()
                or path.casefold().startswith(prefix.casefold() + "/")
                or path == "/user"
            ):
                return 403, {"error": "Repository is outside this review"}
            try:
                data = self.read(path + ("?" + url.query if url.query else ""))
                if len(json.dumps(data).encode()) > 8_000_000:
                    return 413, {
                        "error": "Read exceeds the byte limit; request a smaller page"
                    }
                return 200, data
            except HTTPError as error:
                return error.code, {"error": "GitHub read failed", "status": error.code}
        if method == "POST" and url.path == "/proposal" and not url.query:
            try:
                value = validate_proposal(json.loads(raw))
            except (ValueError, TypeError, KeyError):
                return 400, {
                    "error": "Require decision (approve/changes/scope_stop/skip), nonempty summary, and comments with path/line/body; no target or event fields"
                }
            with self._lock:
                if self._proposal is not None and self._proposal != value:
                    return 409, {"error": "A different proposal was already accepted"}
                self._proposal = value
            return 200, {"status": "proposal_recorded", "published": False}
        return 405, {"error": "Only scoped reads and proposal submission are permitted"}


@contextmanager
def serve_gateway(gateway, host):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 64000:
                self.send_error(413)
                return
            try:
                status, value = gateway.handle(
                    self.command,
                    self.path,
                    self.rfile.read(length),
                    self.headers.get("Authorization"),
                )
            except Exception:
                status, value = 502, {"error": "Review gateway operation failed"}
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/octet-stream"
                if isinstance(value, bytes)
                else "application/json",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_request

    server = ThreadingHTTPServer((host, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
