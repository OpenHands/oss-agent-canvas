"""Run with the plugin's installed SDK; all HTTP endpoints are local fixtures."""

from email import policy
from email.parser import BytesParser
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
from uuid import UUID

from test_priority_candidates import REPO, SCRIPTS, github_server, issue


# Disable inherited tracing before the SDK import; fixture data must stay local.
for key in tuple(os.environ):
    if key.startswith(("LMNR_", "LAMINAR_", "OTEL_", "OPENHANDS_OBSERVABILITY_")):
        del os.environ[key]
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"

from openhands.sdk.conversation.impl.remote_conversation import RemoteState
from openhands.sdk.workspace.remote.base import RemoteWorkspace


class SDKHandoffTests(unittest.TestCase):
    def test_real_workspace_upload_is_readable_and_failure_is_closed(self):
        spec = importlib.util.spec_from_file_location("candidate_fetch", SCRIPTS / "github_candidates.py")
        fetcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fetcher)
        uploads = []
        fail_upload = False
        conversation_id = UUID("00000000-0000-0000-0000-000000000001")
        prefix = f"/api/conversations/{conversation_id}"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                if path == prefix:
                    payload = {"workspace": {"working_dir": "/workspace"}}
                elif path == prefix + "/events/search":
                    payload = {"items": [], "next_page_id": None}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path != prefix + "/file/upload":
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
                relative_path = Path(destination).relative_to("/workspace")
                (Path(directory) / relative_path).write_bytes(part.get_payload(decode=True))
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
                working_dir="/host-not-mounted-in-runtime",
                api_key="fixture-session-key",
            )
            conversation = SimpleNamespace(
                id=conversation_id,
                state=RemoteState(workspace.client, str(conversation_id)),
            )
            try:
                result = workspace.file_upload(b"{}", "/host-not-mounted-in-runtime/snapshot.json")
                self.assertFalse(result.success)
                for _ in range(2):
                    prompt = fetcher.prepare_context(workspace, "fixture-token", conversation)
                    destination = uploads[-1]
                    self.assertEqual(Path(destination).parent, Path("/workspace"))
                    self.assertIn(destination, prompt)
                    snapshot_path = Path(directory) / Path(destination).name
                    snapshot = json.loads(snapshot_path.read_bytes())
                    issues = snapshot["repositories"][REPO]["issues"]
                    self.assertEqual({item["number"] for item in issues}, set(range(1, 115)))
                self.assertEqual(len(set(uploads)), 2)
                fail_upload = True
                with self.assertRaisesRegex(RuntimeError, "upload"):
                    fetcher.prepare_context(workspace, "fixture-token", conversation)
            finally:
                workspace.client.close()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
