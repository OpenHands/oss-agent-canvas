import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/github-pr-reviewer-events/scripts'
REGULAR_REPOS = ['OpenHands/automation', 'OpenHands/oss-agent-canvas']
DOCS = 'OpenHands/docs'
REVIEWER = 'test-reviewer'


def load_worker():
    spec = importlib.util.spec_from_file_location('reviewer_events_worker', SCRIPTS / 'worker.py')
    worker = importlib.util.module_from_spec(spec)
    # Routing stops before dispatch, so CI does not need the SDK for this test.
    dispatcher = types.SimpleNamespace(AgentConversationDispatcher=object)
    with patch.dict(sys.modules, {'agent_conversation': dispatcher}), patch.object(
        sys, 'path', [str(SCRIPTS), *sys.path]
    ):
        spec.loader.exec_module(worker)
    return worker


class ReviewerEventRoutingTests(unittest.TestCase):
    def route(self, repo=None, *, repos=REGULAR_REPOS):
        seen, matched = [], []
        worker = load_worker()

        def record_selection(reviewer):
            seen.append(reviewer.repository)
            payload = reviewer._event_payload()
            if payload is not None and reviewer._event_candidate(payload) is not None:
                matched.append(reviewer.repository)

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'repos': repos, 'trigger_reviewer': REVIEWER}))
            env = {
                'AUTOMATION_CONFIG_PATH': str(config),
                'WORKSPACE_BASE': directory,
                'GITHUB_PERSONAL_ACCESS_TOKEN': 'test-placeholder',
            }
            if repo is not None:
                env['AUTOMATION_EVENT_PAYLOAD'] = json.dumps({'event': {'payload': {
                    'action': 'review_requested',
                    'repository': {'full_name': repo},
                    'pull_request': {'number': 42},
                    'requested_reviewer': {'login': REVIEWER},
                }}})
            # Substitute only the network/dispatch entry point.
            with patch.dict(os.environ, env, clear=True), patch.object(
                worker.PullRequestReviewer, 'run', record_selection
            ), patch.object(sys, 'argv', ['worker.py']):
                worker.run_scan(dispatcher=None)
        return seen, matched

    def test_docs_request_routes_when_docs_is_not_configured(self):
        self.assertEqual(self.route(DOCS), ([*REGULAR_REPOS, DOCS], [DOCS]))

    def test_docs_request_does_not_duplicate_a_configured_docs_repo(self):
        repos = [*REGULAR_REPOS, DOCS.lower()]
        self.assertEqual(self.route(DOCS, repos=repos), (repos, [DOCS.lower()]))

    def test_configured_request_routes_as_before(self):
        self.assertEqual(self.route(REGULAR_REPOS[0]), (REGULAR_REPOS, REGULAR_REPOS[:1]))

    def test_scheduled_scan_does_not_add_docs(self):
        self.assertEqual(self.route(), (REGULAR_REPOS, []))

    def test_other_unconfigured_repo_is_not_added(self):
        self.assertEqual(self.route('OpenHands/other'), (REGULAR_REPOS, []))
