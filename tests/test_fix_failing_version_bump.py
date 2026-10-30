import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The deployed automation tarball and the plugin scripts carry the same file;
# every test runs against both so the mirror cannot silently drift.
MODULES = sorted(
    ROOT.glob("automations/fix-failing-version-bump-*/tarball/main.py")
) + sorted(ROOT.glob("plugins/fix-failing-version-bump/scripts/main.py"))


def load_module(path):
    spec = importlib.util.spec_from_file_location("fixer", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Boundary:
    """In-memory GitHub boundary; tests must never merge a real pull request."""

    def __init__(self, files=None, reviews=None):
        self.files = files or []
        self.reviews = reviews or []
        self.requests = []
        self.merged = []

    def paginate(self, token, path, params=None):
        if path.endswith("/files"):
            return self.files
        return []

    def request(self, token, method, path, params=None, body=None, accept=None):
        self.requests.append((method, path, body))
        if path.endswith("/reviews"):
            return self.reviews, {}
        if path.endswith("/merge"):
            self.merged.append(path)
            return {"merged": True}, {}
        return {}, {}


class ClassifyTests(unittest.TestCase):
    def check(self, files, expected):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                module = load_module(path)
                module._github_paginate = Boundary(files).paginate
                kind, reason = module._classify_bump("token", "owner/repo", {"number": 1})
                self.assertEqual(kind, expected)
                self.assertTrue(reason)

    def test_version_only_bump(self):
        self.check(
            [
                {"filename": "frontend/package.json", "additions": 1, "deletions": 1},
                {"filename": "frontend/package-lock.json", "additions": 40, "deletions": 30},
            ],
            "version-only",
        )

    def test_version_only_requirements_and_locks(self):
        self.check(
            [
                {"filename": "uv.lock", "additions": 3, "deletions": 3},
                {"filename": "pyproject.toml", "additions": 1, "deletions": 1},
                {"filename": "requirements/dev.txt", "additions": 1, "deletions": 1},
            ],
            "version-only",
        )

    def test_minimal_fix(self):
        self.check(
            [
                {"filename": "frontend/package.json", "additions": 1, "deletions": 1},
                {"filename": "src/hooks/useX.ts", "additions": 10, "deletions": 4},
            ],
            "minimal-fix",
        )

    def test_large_by_lines(self):
        self.check(
            [
                {"filename": "src/a.ts", "additions": 60, "deletions": 30},
                {"filename": "src/b.ts", "additions": 20, "deletions": 10},
            ],
            "large",
        )

    def test_large_by_files(self):
        self.check(
            [
                {"filename": f"src/f{i}.ts", "additions": 5, "deletions": 1}
                for i in range(11)
            ],
            "large",
        )

    def test_no_files_is_large(self):
        self.check([], "large")

    def test_source_change_is_not_version_only(self):
        self.check(
            [{"filename": "src/main.py", "additions": 2, "deletions": 1}],
            "minimal-fix",
        )


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp())
        for path in MODULES:
            __import__("shutil").copyfile(path, self.tmp / path.name)

    def write_config(self, payload):
        (self.tmp / "config.json").write_text(json.dumps(payload))

    def test_config_enables_auto_merge(self):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.write_config(
                    {
                        "repos": ["OpenHands/foo"],
                        "auto_merge_dependabot": True,
                        "maintainers": ["neubig", "enyst"],
                        "max_merges_per_run": 2,
                    }
                )
                module = load_module(self.tmp / path.name)
                self.assertEqual(module.AUTO_MERGE_DEPENDABOT, True)
                self.assertEqual(
                    [entry["repo"] for entry in module.REPOS], ["OpenHands/foo"]
                )
                self.assertEqual(module.MAINTAINERS, ["neubig", "enyst"])
                self.assertEqual(module.MAX_MERGES_PER_RUN, 2)

    def test_defaults_stay_conservative(self):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                module = load_module(path)
                self.assertFalse(module.AUTO_MERGE_DEPENDABOT)
                self.assertEqual(module.MAINTAINERS, [])
                self.assertEqual(
                    [entry["repo"] for entry in module.REPOS],
                    [
                        "OpenHands/extensions",
                        "OpenHands/OpenHands",
                        "OpenHands/software-agent-sdk",
                        "OpenHands/automation",
                        "OpenHands/enterprise",
                    ],
                )

    def test_rejects_wrong_types(self):
        for payload in [
            {"max_merges_per_run": True},
            {"auto_merge_dependabot": 1},
            {"maintainers": "neubig"},
            {"minimal_fix_max_files": 0},
            {"repos": []},
        ]:
            with self.subTest(payload=payload):
                self.write_config(payload)
                with self.assertRaises(SystemExit):
                    load_module(self.tmp / MODULES[0].name)


class DeployedAutomationConfigTests(unittest.TestCase):
    def test_deployed_automation_ships_enabled_config(self):
        configs = list(
            ROOT.glob("automations/fix-failing-version-bump-*/tarball/config.json")
        )
        self.assertEqual(len(configs), 1)
        payload = json.loads(configs[0].read_text())
        self.assertTrue(payload["auto_merge_dependabot"])
        self.assertTrue(payload["maintainers"])

    def test_deployed_tarball_and_plugin_scripts_match(self):
        deployed = next(ROOT.glob("automations/fix-failing-version-bump-*/tarball/main.py"))
        plugin = next(ROOT.glob("plugins/fix-failing-version-bump/scripts/main.py"))
        self.assertEqual(deployed.read_text(), plugin.read_text())


class MaintainerHandoffTests(unittest.TestCase):
    def check_handoff(self, roster, reviews, requested, author, expected_login):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                module = load_module(path)
                module.MAINTAINERS = roster
                boundary = Boundary(reviews=reviews)
                module._github_request = boundary.request
                pr = {
                    "number": 5,
                    "head": {"sha": "headsha"},
                    "user": {"login": author},
                    "requested_reviewers": requested,
                }
                login = module._request_maintainer_review("token", "owner/repo", pr, "reason")
                self.assertEqual(login, expected_login)

    def test_requests_first_available_maintainer(self):
        self.check_handoff(["neubig", "enyst"], [], [], "dependabot[bot]", "neubig")

    def test_skips_already_requested_and_prior_reviewers(self):
        reviews = [{"commit_id": "headsha", "user": {"login": "neubig"}}]
        self.check_handoff(["neubig", "enyst"], reviews, [], "dependabot[bot]", "enyst")

    def test_returns_none_when_roster_empty(self):
        self.check_handoff([], [], [], "dependabot[bot]", None)

    def test_returns_none_when_everyone_already_reviewed(self):
        reviews = [
            {"commit_id": "headsha", "user": {"login": "neubig"}},
            {"commit_id": "headsha", "user": {"login": "enyst"}},
        ]
        self.check_handoff(["neubig", "enyst"], reviews, [], "dependabot[bot]", None)

    def test_reviews_on_other_heads_do_not_block(self):
        reviews = [{"commit_id": "oldsha", "user": {"login": "neubig"}}]
        self.check_handoff(["neubig", "enyst"], reviews, [], "dependabot[bot]", "neubig")


class ParseMaintainersTests(unittest.TestCase):
    def test_dedupes_and_strips(self):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                module = load_module(path)
                self.assertEqual(
                    module._parse_maintainers(["@neubig", "neubig", " enyst ", ""]),
                    ["neubig", "enyst"],
                )
                self.assertEqual(
                    module._parse_maintainers(["dependabot[bot]", "neubig"]),
                    ["neubig"],
                )


if __name__ == "__main__":
    unittest.main()