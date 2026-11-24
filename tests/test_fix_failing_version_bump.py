import importlib.util
import json
import os
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
                        "minimal_fix_max_files": 7,
                        "minimal_fix_max_changed_lines": 42,
                    }
                )
                module = load_module(self.tmp / path.name)
                self.assertEqual(module.AUTO_MERGE_DEPENDABOT, True)
                self.assertEqual(
                    [entry["repo"] for entry in module.REPOS], ["OpenHands/foo"]
                )
                self.assertEqual(module.MAINTAINERS, ["neubig", "enyst"])
                self.assertEqual(module.MINIMAL_FIX_MAX_FILES, 7)
                self.assertEqual(module.MINIMAL_FIX_MAX_CHANGED_LINES, 42)

    def test_ignores_removed_max_merges_key(self):
        # The merge cap was removed, but a configuration written while it
        # existed (like the deployed automation's) must still load.
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.write_config({"auto_merge_dependabot": True, "max_merges_per_run": 3})
                module = load_module(self.tmp / path.name)
                self.assertTrue(module.AUTO_MERGE_DEPENDABOT)
                self.assertFalse(hasattr(module, "MAX_MERGES_PER_RUN"))

    def test_defaults_stay_conservative(self):
        # Loaded from a directory holding no config.json, so the defaults are
        # what is being asserted. The deployed tarball ships an enabling
        # config.json beside its copy of the script, which would otherwise
        # shadow the defaults this test is about.
        clean = Path(__import__("tempfile").mkdtemp())
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                (clean / path.name).write_bytes(path.read_bytes())
                module = load_module(clean / path.name)
                self.assertFalse(module.AUTO_MERGE_DEPENDABOT)
                self.assertTrue(module.HANDLE_DEPENDABOT)
                self.assertEqual(module.PIN_BUMP_BRANCH, "")
                self.assertFalse(module.PIN_BUMP_DRY_RUN)
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

    def test_config_path_env_var_wins(self):
        # The plugin path lets the host render the config and point at it with
        # AUTOMATION_CONFIG_PATH; a config.json beside the script must not
        # shadow it.
        other = Path(__import__("tempfile").mkdtemp()) / "rendered.json"
        other.write_text(json.dumps({"auto_merge_dependabot": True}))
        previous = os.environ.get("AUTOMATION_CONFIG_PATH")
        os.environ["AUTOMATION_CONFIG_PATH"] = str(other)
        try:
            for path in MODULES:
                with self.subTest(module=str(path.relative_to(ROOT))):
                    module = load_module(path)
                    self.assertTrue(module.AUTO_MERGE_DEPENDABOT)
        finally:
            if previous is None:
                os.environ.pop("AUTOMATION_CONFIG_PATH", None)
            else:
                os.environ["AUTOMATION_CONFIG_PATH"] = previous

    def test_rejects_wrong_types(self):
        for payload in [
            {"minimal_fix_max_files": True},
            {"auto_merge_dependabot": 1},
            {"maintainers": "neubig"},
            {"minimal_fix_max_files": 0},
            {"repos": []},
            {"handle_dependabot": "no"},
            {"pin_bump_branch": ["bump-pins"]},
            {"pin_bump_dry_run": 1},
            {"pin_bump_files": ["pyproject.toml"]},
            {"pin_bump_files": {"OpenHands/automation": "pyproject.toml"}},
            {"pin_bump_checks": {"OpenHands/OpenHands": [1]}},
        ]:
            with self.subTest(payload=payload):
                self.write_config(payload)
                with self.assertRaises(SystemExit):
                    load_module(self.tmp / MODULES[0].name)

    def test_config_turns_pin_bumps_on(self):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                self.write_config(
                    {
                        "handle_dependabot": False,
                        "pin_bump_branch": "bump-pins",
                        "pin_bump_author": "some-bot[bot]",
                        "pin_bump_files": {"OpenHands/foo": ["pyproject.toml"]},
                        "pin_bump_checks": {"OpenHands/foo": ["extra"]},
                        "pin_bump_dry_run": True,
                        "hold_label": "do not merge",
                    }
                )
                module = load_module(self.tmp / path.name)
                self.assertFalse(module.HANDLE_DEPENDABOT)
                self.assertEqual(module.PIN_BUMP_BRANCH, "bump-pins")
                self.assertEqual(module.PIN_BUMP_AUTHOR, "some-bot[bot]")
                self.assertEqual(module.PIN_BUMP_FILES, {"OpenHands/foo": ["pyproject.toml"]})
                self.assertEqual(module.PIN_BUMP_CHECKS, {"OpenHands/foo": ["extra"]})
                self.assertTrue(module.PIN_BUMP_DRY_RUN)
                self.assertEqual(module.HOLD_LABEL, "do not merge")


class DeployedAutomationConfigTests(unittest.TestCase):
    def test_deployed_automation_ships_enabled_config(self):
        configs = list(
            ROOT.glob("automations/fix-failing-version-bump-dependabot-*/tarball/config.json")
        )
        self.assertEqual(len(configs), 1)
        payload = json.loads(configs[0].read_text())
        self.assertTrue(payload["auto_merge_dependabot"])
        self.assertTrue(payload["maintainers"])
        # No per-run merge cap: every mergeable PR is merged in one poll.
        self.assertNotIn("max_merges_per_run", payload)
        # The dependabot deployment keeps doing dependabot only.
        module = load_module(configs[0].with_name("main.py"))
        self.assertTrue(module.HANDLE_DEPENDABOT)
        self.assertEqual(module.PIN_BUMP_BRANCH, "")

    def test_pin_bump_deployment_does_pin_bumps_only(self):
        configs = list(
            ROOT.glob("automations/fix-failing-version-bump-bump-pins-*/tarball/config.json")
        )
        self.assertEqual(len(configs), 1)
        payload = json.loads(configs[0].read_text())
        self.assertNotIn("max_merges_per_run", payload)
        module = load_module(configs[0].with_name("main.py"))
        self.assertFalse(module.HANDLE_DEPENDABOT)
        self.assertEqual(module.PIN_BUMP_BRANCH, "bump-pins")
        self.assertTrue(module.MAINTAINERS)
        # Every repository it polls says which files a bump may change.
        for entry in module.REPOS:
            self.assertTrue(module.PIN_BUMP_FILES.get(entry["repo"]), entry["repo"])

    def test_deployed_tarball_and_plugin_scripts_match(self):
        deployed = sorted(ROOT.glob("automations/fix-failing-version-bump-*/tarball/main.py"))
        plugin = next(ROOT.glob("plugins/fix-failing-version-bump/scripts/main.py"))
        self.assertEqual(len(deployed), 2)
        for path in deployed:
            self.assertEqual(path.read_text(), plugin.read_text(), path)

    def test_plugin_schema_declares_the_pin_bump_keys(self):
        # The schema is closed (additionalProperties: false): a key the script
        # reads but the schema omits could not be set through the plugin host.
        manifest = json.loads(
            (ROOT / "plugins/fix-failing-version-bump/.plugin/plugin.json").read_text()
        )
        declared = manifest["automation"]["config_schema"]["properties"]
        for key in (
            "handle_dependabot", "pin_bump_branch", "pin_bump_author", "pin_bump_files",
            "pin_bump_checks", "pin_bump_dry_run", "hold_label",
        ):
            self.assertIn(key, declared)


class MergeCapTests(unittest.TestCase):
    """A single poll must merge every green dependabot PR, not just a few."""

    def test_merges_all_green_prs_in_one_repo(self):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                module = load_module(path)
                module.AUTO_MERGE_DEPENDABOT = True
                # The pin-bump deployment's copy ships with dependabot off.
                module.HANDLE_DEPENDABOT = True
                module.PIN_BUMP_BRANCH = ""
                prs = [
                    {
                        "number": n,
                        "head": {"sha": f"sha{n}", "ref": f"dependabot/{n}"},
                        "user": {"login": "dependabot[bot]"},
                    }
                    for n in range(1, 8)
                ]
                merged = []
                module._verify_repo = lambda *a, **k: None
                module._list_open_prs = lambda *a, **k: prs
                module._get_pr = lambda token, repo, number: prs[number - 1]
                module._ci_status = lambda *a, **k: "clear"
                module._maybe_merge_green_dependabot = (
                    lambda token, repo, pr, head_sha, fixes, merges: merged.append(
                        pr["number"]
                    )
                )
                state = {"version": 1, "repos": {}}
                module._process_repo(
                    "owner/repo", "token", "http://agent", "key", "http://oh", state
                )
                self.assertEqual(merged, [1, 2, 3, 4, 5, 6, 7])


# What the automation service accepts for one KV value (`kv_max_value_size`).
KV_VALUE_LIMIT = 64 * 1024


class StateSizeTests(unittest.TestCase):
    """The state must stay small enough to be saved, poll after poll."""

    def poll(self, module, state, open_prs):
        module.HANDLE_DEPENDABOT = True
        module.PIN_BUMP_BRANCH = ""
        module._verify_repo = lambda *a, **k: None
        module._list_open_prs = lambda *a, **k: open_prs
        module._get_pr = lambda token, repo, number: next(
            pr for pr in open_prs if pr["number"] == number
        )
        module._ci_status = lambda *a, **k: "running"
        module._check_fix_completion = lambda *a, **k: None
        module._process_repo("owner/repo", "token", "http://agent", "key", "http://oh", state)

    def fix(self, number, status):
        key = f"owner/repo#{number}@{number:040d}"
        return key, {
            "repo": "owner/repo", "pr_number": number, "head_sha": f"{number:040d}",
            "status": status, "last_activity": 1.0 + number,
        }

    def merge(self, number):
        key = f"owner/repo#{number}@{number:040d}"
        return key, {
            "repo": "owner/repo", "pr_number": number, "head_sha": f"{number:040d}",
            "classification": "version-only", "merged_at": 1.0 + number,
        }

    def test_poll_forgets_pull_requests_that_are_no_longer_open(self):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                module = load_module(path)
                open_key, open_rec = self.fix(1, "handoff")
                gone_key, gone_rec = self.fix(2, "handoff")
                active_key, active_rec = self.fix(3, "active")
                merged_key, merged_rec = self.merge(4)
                state = {"version": 1, "repos": {"owner/repo": {
                    "fixes": {open_key: open_rec, gone_key: gone_rec, active_key: active_rec},
                    "merges": {merged_key: merged_rec},
                }}}
                open_pr = {
                    "number": 1,
                    "head": {"sha": f"{1:040d}", "ref": "dependabot/1"},
                    "user": {"login": "dependabot[bot]"},
                }
                self.poll(module, state, [open_pr])
                repo_state = state["repos"]["owner/repo"]
                # The open PR stays handled, and a running fix keeps its record
                # until the completion check closes it.
                self.assertEqual(set(repo_state["fixes"]), {open_key, active_key})
                self.assertEqual(repo_state["merges"], {})

    def test_state_fits_the_kv_store_after_a_poll(self):
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                module = load_module(path)
                # Two hundred hand-offs and two hundred merges, all long closed:
                # within the per-repo record caps, yet too big to save.
                fixes = dict(self.fix(n, "handoff") for n in range(1, 201))
                merges = dict(self.merge(n) for n in range(201, 401))
                state = {"version": 1, "repos": {"owner/repo": {"fixes": fixes, "merges": merges}}}
                self.assertGreater(len(json.dumps(state).encode()), KV_VALUE_LIMIT)
                self.poll(module, state, [])
                self.assertLessEqual(len(json.dumps(state).encode()), KV_VALUE_LIMIT)


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