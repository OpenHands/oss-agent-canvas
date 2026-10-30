"""Run with the plugin's installed SDK; all HTTP endpoints are local fixtures."""

from email import policy
from email.parser import BytesParser
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

from test_priority_candidates import REPO, SCRIPTS, github_server, issue


# Disable inherited tracing before the SDK import; fixture data must stay local.
for key in tuple(os.environ):
    if key.startswith(("LMNR_", "LAMINAR_", "OTEL_", "OPENHANDS_OBSERVABILITY_")):
        del os.environ[key]
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"

from openhands.sdk.workspace.remote.base import RemoteWorkspace


class SDKHandoffTests(unittest.TestCase):
    def test_real_workspace_upload_is_readable_and_failure_is_closed(self):
        spec = importlib.util.spec_from_file_location("candidate_fetch", SCRIPTS / "github_candidates.py")
        fetcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fetcher)
        uploads = []
        fail_upload = False

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != "/api/file/upload":
                    self.send_error(404)
                    return
                content = self.rfile.read(int(self.headers["Content-Length"]))
                if fail_upload:
                    self.send_error(500)
                    return
                message = BytesParser(policy=policy.default).parsebytes(
                    f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + content
                )
                part = next(message.iter_parts())
                destination = urllib.parse.parse_qs(parsed.query)["path"][0]
                Path(destination).write_bytes(part.get_payload(decode=True))
                uploads.append(destination)
                data = json.dumps({"success": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with tempfile.TemporaryDirectory() as directory, github_server(
            {(REPO, "priority:medium"): [issue(n) for n in range(1, 115)]}
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            workspace = RemoteWorkspace(
                host=f"http://127.0.0.1:{server.server_port}",
                working_dir=directory,
                api_key="fixture-session-key",
            )
            try:
                for _ in range(2):
                    prompt = fetcher.prepare_context(workspace, "fixture-token")
                    destination = uploads[-1]
                    self.assertEqual(Path(destination).parent, Path(directory))
                    self.assertIn(destination, prompt)
                    snapshot = json.loads(Path(destination).read_bytes())
                    issues = snapshot["repositories"][REPO]["issues"]
                    self.assertEqual({item["number"] for item in issues}, set(range(1, 115)))
                self.assertEqual(len(set(uploads)), 2)
                fail_upload = True
                with self.assertRaisesRegex(RuntimeError, "upload"):
                    fetcher.prepare_context(workspace, "fixture-token")
            finally:
                workspace.client.close()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
