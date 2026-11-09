"""Regression checks for reviewer-request events from the docs repository."""

import argparse
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/github-pr-reviewer-events/scripts"


def load_definition(path, name, namespace):
    tree = ast.parse(path.read_text())
    definition = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name
    )
    module = ast.fix_missing_locations(ast.Module(body=[definition], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


class DocsEventRoutingTests(unittest.TestCase):
    def test_run_scan_adds_docs_only_for_docs_events(self):
        calls = []

        def record_scan(*args, **kwargs):
            calls.append(kwargs)

        namespace = {"os": os, "json": json, "run_repositories": record_scan}
        reviewer = type(
            "Reviewer",
            (),
            {"_event_payload": staticmethod(load_definition(
                SCRIPTS / "worker.py", "_event_payload", {"os": os, "json": json}
            ))},
        )
        namespace["PullRequestReviewer"] = reviewer
        namespace["ReviewIntake"] = type("ReviewIntake", (), {"drain": lambda self: None})
        run_scan = load_definition(SCRIPTS / "worker.py", "run_scan", namespace)
        original = os.environ.get("AUTOMATION_EVENT_PAYLOAD")
        try:
            for repo in ("OpenHands/docs", "OpenHands/automation", None):
                if repo is None:
                    os.environ.pop("AUTOMATION_EVENT_PAYLOAD", None)
                else:
                    os.environ["AUTOMATION_EVENT_PAYLOAD"] = json.dumps(
                        {"event": {"payload": {"repository": {"full_name": repo}}}}
                    )
                run_scan(dispatcher=None)
        finally:
            if original is None:
                os.environ.pop("AUTOMATION_EVENT_PAYLOAD", None)
            else:
                os.environ["AUTOMATION_EVENT_PAYLOAD"] = original

        self.assertEqual(calls[0]["extra_repositories"], ("OpenHands/docs",))
        self.assertEqual(calls[1]["extra_repositories"], ())
        self.assertEqual(calls[2]["extra_repositories"], ())

    def test_extra_repository_runs_once_with_existing_config(self):
        calls = []

        class Recorder:
            def __init__(self, *, repository, **kwargs):
                self.repository = repository

            def run(self):
                calls.append(self.repository)

        namespace = {"argparse": argparse, "json": json, "os": os, "Path": Path}
        run_repositories = load_definition(
            SCRIPTS / "github_client.py", "run_repositories", namespace
        )
        original_argv = sys.argv
        original_config = os.environ.get("AUTOMATION_CONFIG_PATH")
        sys.argv = ["worker.py"]
        os.environ["AUTOMATION_CONFIG_PATH"] = str(SCRIPTS / "config.json")
        try:
            run_repositories(Recorder, extra_repositories=("OpenHands/docs",))
            self.assertEqual(calls.count("OpenHands/docs"), 1)
            calls.clear()
            with tempfile.TemporaryDirectory() as directory:
                config = json.loads((SCRIPTS / "config.json").read_text())
                config["repos"].append("OpenHands/docs")
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(config))
                os.environ["AUTOMATION_CONFIG_PATH"] = str(path)
                run_repositories(Recorder, extra_repositories=("OpenHands/docs",))
            self.assertEqual(calls.count("OpenHands/docs"), 1)
        finally:
            sys.argv = original_argv
            if original_config is None:
                os.environ.pop("AUTOMATION_CONFIG_PATH", None)
            else:
                os.environ["AUTOMATION_CONFIG_PATH"] = original_config


if __name__ == "__main__":
    unittest.main()
