import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_factory_handoff import GitHub, LinearFixture, ISSUE, PULL, REPO, SUMMARY, URL


@pytest.mark.parametrize("mode", ["assignment", "linear"])
def test_opted_in_cli_recovers_and_reports_exact_review(tmp_path, mode):
    gh, linear = GitHub(), LinearFixture()
    errors = []

    class Server(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                if self.path == "/linear":
                    assert self.headers["Authorization"] == "fixture-only"
                    result = {
                        "data": linear.gql(payload["query"], **payload["variables"])
                    }
                else:
                    assert self.path == "/gh"
                    args = payload["args"]
                    assert args[:3] == ["api", "--hostname", "github.com"]
                    if "--paginate" in args:
                        endpoint = args[-1]
                        if "/statuses?" in endpoint:
                            result = []
                        elif "/check-runs?" in endpoint:
                            assert gh.pr["head"]["sha"] in endpoint and args[-3:-1] == [
                                "--jq",
                                ".check_runs",
                            ]
                            result = [
                                {
                                    "name": "test",
                                    "status": "completed",
                                    "conclusion": gh.check_state,
                                }
                            ]
                        else:
                            result = gh.pages(endpoint)
                    else:
                        assert args[3] == "--method"
                        result = gh.api(args[5], args[4], args[7::2])
                self.send_response(200)
            except OSError:
                result = {"error": "lost response"}
                self.send_response(503)
            except Exception as exc:
                errors.append(repr(exc))
                result = {"error": "unexpected request"}
                self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    shim = tmp_path / "gh"
    shim.write_text(
        f"#!{sys.executable} -S\n"
        + """import json, os, sys, urllib.request
data = json.dumps({"args": sys.argv[1:]}).encode()
with urllib.request.urlopen(os.environ["TEST_API"] + "/gh", data=data) as response:
    sys.stdout.buffer.write(response.read())
"""
    )
    shim.chmod(0o700)
    (tmp_path / "sitecustomize.py").write_text(
        f"import factory_linear\nfactory_linear.LINEAR = {base + '/linear'!r}\n"
    )
    env = {
        "PATH": str(tmp_path) + os.pathsep + os.defpath,
        "PYTHONPATH": os.pathsep.join(map(str, (tmp_path, SUMMARY.parent))),
        "TEST_API": base,
        "LINEAR_API_KEY": "fixture-only",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    route = {
        "mode": mode,
        "assignee": "all-hands-bot",
        "resolver_author": "resolver-writer",
        "reviewer": "all-hands-bot",
        "required_checks": ["test"],
    }
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "state_dir": str(tmp_path / "state"),
                "factory_handoff": {
                    "enabled": True,
                    "dispatcher_login": gh.actor,
                    "trusted_enrollers": ["maintainer"],
                    "repositories": {REPO: route},
                },
            }
        )
    )
    steps = []

    def tick(*args, failure=False):
        process = subprocess.run(
            [
                sys.executable,
                str(SUMMARY),
                "--config",
                str(config),
                "--issue",
                URL,
                *args,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert not errors, errors
        assert process.returncode == int(failure), process.stderr
        status = json.loads(process.stdout)["issues"][ISSUE.casefold()]
        steps.append(
            {
                "status": status,
                "comment": gh.comments[0]["body"] if gh.comments else "",
                "labels": sorted(label["name"] for label in gh.issue["labels"]),
            }
        )
        return status

    try:
        assert tick("--dry-run") == "eligible"
        gh.fail_once = ISSUE + ("/assignees" if mode == "assignment" else "/comments")
        assert tick(failure=True) == "readback_or_write_failed"
        assert tick() == "implementing"
        assert tick() == "implementing"
        branch = (
            "openhands/issue-42"
            if mode == "assignment"
            else "openhands/factory-issue-42"
        )
        gh.make_pr(branch=branch, body="Closes #42" if mode == "assignment" else URL)
        gh.check_state = "failure"
        assert tick() == "revision-needed"
        gh.pr["head"]["sha"] = "b" * 40
        gh.check_state = "success"
        gh.fail_once = PULL + "/requested_reviewers"
        assert tick(failure=True) == "readback_or_write_failed"
        assert tick() == "reviewing"
        gh.review("APPROVED", sha="a" * 40)
        assert tick() == "reviewing"
        gh.review("APPROVED")
        assert tick() == "accepted"
        assert "(draft)" in steps[-1]["comment"]
        gh.pr["draft"] = False
        assert tick() == "accepted"
        assert (
            "(ready for review)" in steps[-1]["comment"]
            and "b" * 40 in steps[-1]["comment"]
        )
        assert "inspect the behavior evidence" in steps[-1]["comment"]
        assert gh.issue["body"] == "Human issue body. Never rewrite this."
        assert len(gh.comments) == 1
        assert (
            sum(endpoint.endswith("/requested_reviewers") for endpoint, *_ in gh.writes)
            == 1
        )
        assert sum(
            endpoint.endswith("/assignees") for endpoint, *_ in gh.writes
        ) == int(mode == "assignment")
        assert len(linear.writes) == int(mode == "linear")
        assert all(
            "linear.app" not in step["comment"] and "OHE-123" not in step["comment"]
            for step in steps
        )
        (tmp_path / "observed.json").write_text(
            json.dumps({"mode": mode, "steps": steps}, indent=2)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
