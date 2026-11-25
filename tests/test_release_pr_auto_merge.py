"""Release pull requests in the fix-failing-version-bump automation.

A repository's release-please pull request is merged once it would ship a
merged pin bump. Everything here runs against an in-memory GitHub: no test may
approve or merge a real pull request.
"""

import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path

from test_fix_failing_version_bump import MODULES, ROOT, load_module
from test_pin_bump_auto_merge import BOT, REPO, SELF, FakeGitHub, changed, configure, pin_pr, push, run

RELEASE_BRANCH = "release-please--branches--main"
RELEASE_FILES = [
    ".release-please-manifest.json",
    "openhands/automation/__init__.py",
    "openhands/automation/app.py",
    "pyproject.toml",
    "uv.lock",
]
KEY = f"{REPO}#12@head1"


def release_pr(**overrides):
    pr = {
        "number": 12,
        "state": "open",
        "draft": False,
        "title": "chore(main): release 1.35.2",
        "user": {"login": BOT},
        "labels": [{"name": "autorelease: pending"}, {"name": "type: chore"}],
        "requested_reviewers": [],
        "head": {"ref": RELEASE_BRANCH, "sha": "head1", "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO, "default_branch": "main"}},
    }
    pr.update(overrides)
    return pr


def merged_pr(number, branch, merged_at, author=BOT, repo=REPO):
    return {
        "number": number,
        "state": "closed",
        "merged_at": merged_at,
        "user": {"login": author},
        "head": {"ref": branch, "repo": {"full_name": repo}},
        "base": {"ref": "main", "repo": {"full_name": REPO, "default_branch": "main"}},
    }


# Release 1.35.1 went out in the morning; the pin bump was merged in the afternoon.
LAST_RELEASE = merged_pr(10, RELEASE_BRANCH, "2026-11-24T11:36:10Z")
BUMP = merged_pr(11, "bump-pins", "2026-11-24T17:06:41Z")


class FakeRelease(FakeGitHub):
    """One repository with an open release PR and its history of merged bot PRs."""

    def __init__(self, pr=None, history=(LAST_RELEASE, BUMP), open_bumps=(), behind=0, files=None, **github):
        files = [changed(path) for path in RELEASE_FILES] if files is None else files
        super().__init__(pr=pr or release_pr(), files=files, **github)
        self.history = list(history)
        self.open_bumps = list(open_bumps)
        self.behind = behind  # None: the comparison cannot be read

    def paginate(self, token, path, params=None):
        if path.endswith("/pulls"):
            self.reads.append((path, dict(params or {})))
            head = (params or {}).get("head", "")
            if head.endswith(":bump-pins"):
                return self.open_bumps
            return [self.pr]
        return super().paginate(token, path, params)

    def request(self, token, method, path, params=None, body=None, accept=None):
        if method == "GET" and path.endswith("/pulls"):
            self.reads.append((path, dict(params or {})))
            branch = params["head"].split(":", 1)[1]
            return [pr for pr in self.history if pr["head"]["ref"] == branch], {}
        if method == "GET" and "/compare/" in path:
            self.reads.append((path, dict(params or {})))
            if self.behind is None:
                raise urllib.error.URLError("boom")
            return {"behind_by": self.behind}, {}
        return super().request(token, method, path, params, body, accept)

    def read(self, fragment):
        return [path for path, _ in self.reads if fragment in path]


class ReleaseCase(unittest.TestCase):
    def modules(self, github_factory=FakeRelease, **settings):
        """Yield (module, github) for every copy of the script, each with a fresh fake."""
        settings.setdefault("RELEASE_BRANCHES", {REPO: RELEASE_BRANCH})
        settings.setdefault("RELEASE_FILES", {REPO: RELEASE_FILES})
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                github = github_factory()
                yield configure(load_module(path), github, **settings), github

    def poll(self, module, github, fixes=None, merges=None):
        fixes = {} if fixes is None else fixes
        merges = {} if merges is None else merges
        merged = module._process_release(REPO, github.pr, "token", SELF, fixes, merges, github.open_bumps)
        return merged, fixes, merges

    def left_alone(self, **github):
        """Nothing is written and nothing is recorded: the next poll looks again."""
        for module, fake in self.modules(lambda: FakeRelease(**github)):
            merged, fixes, merges = self.poll(module, fake)
            self.assertEqual((merged, fake.writes, fixes, merges), (False, [], {}, {}))
            yield fake


class SelectionTests(ReleaseCase):
    def test_only_the_bots_pending_release_from_the_release_branch(self):
        fork = {"ref": RELEASE_BRANCH, "sha": "head1", "repo": {"full_name": "someone/automation"}}
        component = {"ref": f"{RELEASE_BRANCH}--components--other", "sha": "head1", "repo": {"full_name": REPO}}
        maintenance = {"ref": "release/1.2", "repo": {"full_name": REPO, "default_branch": "main"}}
        for module, _ in self.modules():
            self.assertTrue(module._is_release_pr(release_pr(), REPO))
            self.assertFalse(module._is_release_pr(release_pr(user={"login": "hieptl"}), REPO))
            self.assertFalse(module._is_release_pr(release_pr(head=fork), REPO))
            self.assertFalse(module._is_release_pr(release_pr(head=component), REPO))
            self.assertFalse(module._is_release_pr(release_pr(base=maintenance), REPO))
            self.assertFalse(module._is_release_pr(pin_pr(), REPO))
            # Merging a pull request release-please no longer tracks releases nothing.
            self.assertFalse(module._is_release_pr(release_pr(labels=[{"name": "autorelease: tagged"}]), REPO))
            self.assertFalse(module._is_release_pr(release_pr(labels=[]), REPO))
            # A repository without a release branch has no release pull request.
            self.assertFalse(module._is_release_pr(release_pr(), "OpenHands/extensions"))
            module.RELEASE_BRANCHES = {}
            self.assertFalse(module._is_release_pr(release_pr(), REPO))

    def test_a_pin_bump_is_still_told_apart_from_a_release(self):
        for module, _ in self.modules():
            self.assertTrue(module._is_pin_bump(pin_pr(), REPO))
            self.assertFalse(module._is_pin_bump(release_pr(), REPO))


class UnreleasedPinBumpTests(ReleaseCase):
    def unreleased(self, history, expected, **settings):
        for module, _ in self.modules(lambda: FakeRelease(history=history), **settings):
            bump = module._unreleased_pin_bump("token", REPO)
            self.assertEqual(bump and bump["number"], expected)

    def test_a_bump_merged_after_the_last_release_is_unreleased(self):
        self.unreleased([LAST_RELEASE, BUMP], 11)

    def test_a_bump_the_last_release_already_shipped_is_not(self):
        shipped = merged_pr(13, RELEASE_BRANCH, "2026-11-24T17:18:16Z")
        self.unreleased([LAST_RELEASE, BUMP, shipped], None)

    def test_the_newest_merge_of_each_kind_counts(self):
        older_bump = merged_pr(8, "bump-pins", "2026-11-20T08:56:14Z")
        self.unreleased([BUMP, LAST_RELEASE, older_bump], 11)
        self.unreleased([older_bump, LAST_RELEASE], None)

    def test_without_any_release_yet_a_merged_bump_is_unreleased(self):
        self.unreleased([BUMP], 11)

    def test_no_merged_bump_means_nothing_to_ship(self):
        self.unreleased([LAST_RELEASE], None)
        self.unreleased([], None)

    def test_closed_or_foreign_pull_requests_from_the_branches_do_not_count(self):
        closed_bump = merged_pr(14, "bump-pins", None)
        someones_bump = merged_pr(15, "bump-pins", "2026-11-24T18:00:00Z", author="hieptl")
        forked_bump = merged_pr(16, "bump-pins", "2026-11-24T18:00:00Z", repo="someone/automation")
        self.unreleased([LAST_RELEASE, closed_bump, someones_bump, forked_bump], None)
        # A release merged by someone else's pull request does not hide the bump.
        someones_release = merged_pr(17, RELEASE_BRANCH, "2026-11-24T18:00:00Z", author="hieptl")
        self.unreleased([LAST_RELEASE, BUMP, someones_release], 11)

    def test_without_a_pin_branch_nothing_is_a_pin_bump(self):
        self.unreleased([LAST_RELEASE, BUMP], None, PIN_BUMP_BRANCH="")


class ReleaseFilesTests(ReleaseCase):
    def extra(self, files, expected, **settings):
        for module, _ in self.modules(lambda: FakeRelease(files=files), **settings):
            self.assertEqual(module._release_extra_files("token", REPO, {"number": 12}), expected)

    def test_the_repositorys_release_files_are_expected(self):
        self.extra([changed(path, 300, 300) for path in RELEASE_FILES], [])

    def test_anything_else_is_reported(self):
        self.extra([changed("pyproject.toml"), changed("openhands/automation/scheduler.py")],
                   ["openhands/automation/scheduler.py"])

    def test_the_evidence_directory_may_only_be_deleted(self):
        removed = {"filename": ".pr/evidence/after.png", "status": "removed"}
        added = {"filename": ".pr/evidence/new.png", "status": "added"}
        elsewhere = {"filename": "docs/.pr/after.png", "status": "removed"}
        self.extra([changed("pyproject.toml"), removed], [])
        self.extra([changed("pyproject.toml"), removed, added, elsewhere],
                   [".pr/evidence/new.png", "docs/.pr/after.png"])

    def test_without_a_list_every_file_is_unexpected(self):
        self.extra([changed("pyproject.toml")], ["pyproject.toml"], RELEASE_FILES={})

    def test_none_when_no_file_can_be_listed(self):
        self.extra([], None)


class FlowTests(ReleaseCase):
    def test_a_green_release_that_ships_a_pin_bump_is_approved_and_squash_merged(self):
        for module, github in self.modules():
            merged, fixes, merges = self.poll(module, github)
            self.assertTrue(merged)
            self.assertEqual(github.kinds(), ["approve", "merge"])
            _, _, approval = github.writes[0]
            self.assertIn("it ships pin bump #11", approval["body"])
            _, _, merge = github.writes[1]
            self.assertEqual(merge["merge_method"], "squash")
            self.assertEqual(merge["sha"], "head1")
            self.assertEqual(merge["commit_title"], "chore(main): release 1.35.2 (#12)")
            self.assertEqual(merges[KEY]["classification"], "release")
            self.assertEqual(fixes, {})

    def test_a_release_without_a_new_pin_bump_is_left_to_a_maintainer(self):
        shipped = merged_pr(13, RELEASE_BRANCH, "2026-11-24T17:18:16Z")
        for github in self.left_alone(history=[LAST_RELEASE, BUMP, shipped]):
            # Green is not the reason to release, so the checks are not even read.
            self.assertEqual(github.read("/check-runs"), [])

    def test_the_release_waits_for_an_open_pin_bump(self):
        for github in self.left_alone(open_bumps=[pin_pr()]):
            self.assertEqual(github.read("/check-runs"), [])

    def test_the_release_waits_until_it_is_built_on_the_tip_of_its_base(self):
        for behind in (1, 7, None):
            for github in self.left_alone(behind=behind):
                self.assertEqual(github.read("/compare/"), [f"/repos/{REPO}/compare/main...head1"])
                self.assertEqual(github.read("/check-runs"), [])

    def test_nothing_happens_while_the_gate_is_not_green(self):
        for check_runs in ([], [run("unit-tests", None, "in_progress")], [run("unit-tests", "failure")]):
            for github in self.left_alone(check_runs=check_runs):
                # A red release is fixed on the base branch: no fix conversation.
                self.assertEqual(github.prompts, [])

    def test_release_checks_join_the_gate_and_pin_bump_checks_do_not(self):
        runs = [run("unit-tests"), run("e2e", "failure", run_id=2), run("Check SDK version consistency", "failure", run_id=3)]
        for module, github in self.modules(
            lambda: FakeRelease(check_runs=runs), PIN_BUMP_CHECKS={REPO: ["Check SDK version consistency"]},
        ):
            merged, _, _ = self.poll(module, github)
            self.assertTrue(merged)
        for module, github in self.modules(lambda: FakeRelease(check_runs=runs), RELEASE_CHECKS={REPO: ["e2e"]}):
            merged, fixes, merges = self.poll(module, github)
            self.assertEqual((merged, github.writes, fixes, merges), (False, [], {}, {}))

    def test_hold_label_and_draft_are_left_alone(self):
        held = release_pr(labels=[{"name": "autorelease: pending"}, {"name": "Release: Hold"}])
        for pr in (held, release_pr(draft=True)):
            for github in self.left_alone(pr=pr):
                self.assertEqual(github.read("/check-runs"), [])

    def test_a_pull_request_that_stopped_being_the_release_is_left_alone(self):
        tagged = release_pr(labels=[{"name": "autorelease: tagged"}])
        for pr in (release_pr(state="closed"), release_pr(user={"login": "hieptl"}), tagged):
            list(self.left_alone(pr=pr))

    def test_a_branch_someone_else_pushed_to_goes_to_a_maintainer(self):
        activity = [push("hieptl"), push(BOT, "head0", "force_push")]
        for module, github in self.modules(lambda: FakeRelease(activity=activity)):
            merged, fixes, merges = self.poll(module, github)
            self.assertFalse(merged)
            self.assertEqual(github.kinds(), ["request-review", "comment"])
            self.assertIn("@neubig", github.comments()[0])
            self.assertIn("this release PR's CI is green", github.comments()[0])
            self.assertIn("pushed to by hieptl", github.comments()[0])
            self.assertEqual(fixes[KEY]["status"], "handoff")
            self.assertEqual(merges, {})

            # Asked once per head.
            self.poll(module, github, fixes, merges)
            self.assertEqual(github.kinds(), ["request-review", "comment"])

    def test_this_automations_own_push_is_not_trusted_on_a_release_branch(self):
        # On a pin bump its fix commits are expected; a release carries none.
        activity = [push(SELF), push(BOT, "head0", "force_push")]
        for module, github in self.modules(lambda: FakeRelease(activity=activity)):
            merged, fixes, _ = self.poll(module, github)
            self.assertFalse(merged)
            self.assertIn(f"pushed to by {SELF}", github.comments()[0])

    def test_the_activity_of_the_release_branch_is_what_is_read(self):
        for module, github in self.modules():
            self.poll(module, github)
            refs = [params["ref"] for path, params in github.reads if path.endswith("/activity")]
            self.assertEqual(refs, [f"refs/heads/{RELEASE_BRANCH}"])

    def test_the_merge_waits_until_the_activity_log_covers_the_head(self):
        list(self.left_alone(activity=[push(BOT, "older", "force_push")]))

    def test_a_file_a_release_does_not_change_goes_to_a_maintainer(self):
        files = [changed("pyproject.toml"), changed("openhands/automation/scheduler.py", 4, 2)]
        for module, github in self.modules(lambda: FakeRelease(files=files)):
            merged, fixes, merges = self.poll(module, github)
            self.assertFalse(merged)
            self.assertEqual(github.kinds(), ["request-review", "comment"])
            self.assertIn("files a release does not change (openhands/automation/scheduler.py)", github.comments()[0])
            self.assertEqual(fixes[KEY]["status"], "handoff")
            self.assertEqual(merges, {})

    def test_a_release_whose_files_cannot_be_listed_goes_to_a_maintainer(self):
        for module, github in self.modules(lambda: FakeRelease(files=[])):
            merged, fixes, _ = self.poll(module, github)
            self.assertFalse(merged)
            self.assertEqual(github.kinds(), ["request-review", "comment"])
            self.assertEqual(fixes[KEY]["status"], "handoff")

    def test_deleted_evidence_does_not_stop_the_merge(self):
        files = [changed(path) for path in RELEASE_FILES]
        files.append({"filename": ".pr/evidence/after.png", "status": "removed", "additions": 0, "deletions": 0})
        for module, github in self.modules(lambda: FakeRelease(files=files)):
            merged, _, _ = self.poll(module, github)
            self.assertTrue(merged)

    def test_dry_run_comments_once_and_writes_nothing_else(self):
        for module, github in self.modules(RELEASE_DRY_RUN=True):
            merged, fixes, merges = self.poll(module, github)
            self.assertFalse(merged)
            self.assertEqual(github.kinds(), ["comment"])
            self.assertIn("Dry run", github.comments()[0])
            self.assertIn("approve and squash-merge this release pull request", github.comments()[0])
            self.assertIn("it ships pin bump #11", github.comments()[0])
            self.assertEqual(merges, {})

            # The next poll of the same head says nothing more.
            self.poll(module, github, fixes, merges)
            self.assertEqual(github.kinds(), ["comment"])

            # Switching dry run off acts on the head that was only noted.
            module.RELEASE_DRY_RUN = False
            merged, fixes, merges = self.poll(module, github, fixes, merges)
            self.assertTrue(merged)
            self.assertEqual(github.kinds(), ["comment", "approve", "merge"])

    def test_dry_run_of_a_refusal_only_comments(self):
        files = [changed("openhands/automation/scheduler.py")]
        for module, github in self.modules(lambda: FakeRelease(files=files), RELEASE_DRY_RUN=True):
            merged, fixes, _ = self.poll(module, github)
            self.assertFalse(merged)
            self.assertEqual(github.kinds(), ["comment"])
            self.assertIn("ask a maintainer to review this release pull request", github.comments()[0])
            self.assertEqual(fixes[KEY]["status"], "dry-run")

    def test_the_two_dry_runs_are_separate_switches(self):
        # Pin bumps live, releases still in dry run: the release is only noted.
        for module, github in self.modules(PIN_BUMP_DRY_RUN=False, RELEASE_DRY_RUN=True):
            merged, _, _ = self.poll(module, github)
            self.assertEqual((merged, github.kinds()), (False, ["comment"]))
        for module, github in self.modules(PIN_BUMP_DRY_RUN=True, RELEASE_DRY_RUN=False):
            merged, _, _ = self.poll(module, github)
            self.assertEqual((merged, github.kinds()), (True, ["approve", "merge"]))

    def test_a_refused_merge_is_left_to_a_maintainer(self):
        for module, github in self.modules(lambda: FakeRelease(refuse_merge=True)):
            merged, fixes, merges = self.poll(module, github)
            self.assertFalse(merged)
            self.assertEqual(github.kinds(), ["approve", "merge", "comment"])
            self.assertIn("GitHub refused the merge", github.comments()[0])
            self.assertEqual(fixes[KEY]["status"], "handoff")
            self.assertEqual(merges, {})


class ProcessRepoTests(ReleaseCase):
    def process(self, module, state=None):
        state = {} if state is None else state
        module._process_repo(REPO, "token", "http://agent", "key", "http://canvas", state, SELF)
        return state

    def test_the_release_is_merged_in_the_poll_that_finds_it_ready(self):
        for module, github in self.modules():
            state = self.process(module)
            self.assertEqual(github.kinds(), ["approve", "merge"])
            self.assertTrue(github.writes[1][1].startswith("pulls/12/"))
            self.assertEqual(list(state["repos"][REPO]["merges"]), [KEY])

    def test_an_open_pin_bump_is_handled_first_and_holds_the_release_back(self):
        bump = pin_pr()
        for module, github in self.modules(lambda: FakeRelease(open_bumps=[bump], other_prs=[bump])):
            self.process(module)
            # The pin bump (#9) is merged; the release (#12) waits for release-please
            # to rebuild it on the new tip.
            self.assertEqual(github.kinds(), ["approve", "merge"])
            self.assertTrue(github.writes[1][1].startswith("pulls/9/"))

    def test_a_repository_without_a_release_branch_is_not_asked_for_one(self):
        for module, github in self.modules(RELEASE_BRANCHES={"OpenHands/OpenHands": "some-branch"}):
            self.process(module)
            listings = [params for path, params in github.reads if path.endswith("/pulls")]
            self.assertEqual(listings, [{"state": "open", "head": "OpenHands:bump-pins"}])
            self.assertEqual(github.writes, [])

    def test_a_dry_run_note_survives_the_poll_while_the_release_is_open(self):
        for module, github in self.modules(RELEASE_DRY_RUN=True):
            state = self.process(module)
            self.process(module, state)
            self.assertEqual(github.kinds(), ["comment"])
            self.assertEqual(state["repos"][REPO]["fixes"][KEY]["status"], "dry-run")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for path in MODULES:
            shutil.copyfile(path, self.tmp / path.name)

    def load(self, payload):
        (self.tmp / "config.json").write_text(json.dumps(payload))
        return load_module(self.tmp / MODULES[0].name)

    def test_config_turns_releases_on(self):
        module = self.load({
            "release_branches": {REPO: RELEASE_BRANCH},
            "release_files": {REPO: ["pyproject.toml"]},
            "release_checks": {REPO: ["e2e"]},
            "release_dry_run": True,
        })
        self.assertEqual(module.RELEASE_BRANCHES, {REPO: RELEASE_BRANCH})
        self.assertEqual(module.RELEASE_FILES, {REPO: ["pyproject.toml"]})
        self.assertEqual(module.RELEASE_CHECKS, {REPO: ["e2e"]})
        self.assertTrue(module.RELEASE_DRY_RUN)

    def test_releases_are_off_by_default(self):
        module = self.load({})
        self.assertEqual(module.RELEASE_BRANCHES, {})
        self.assertEqual(module.RELEASE_FILES, {})
        self.assertEqual(module.RELEASE_CHECKS, {})
        self.assertFalse(module.RELEASE_DRY_RUN)

    def test_rejects_wrong_types(self):
        for payload in [
            {"release_branches": [RELEASE_BRANCH]},
            {"release_branches": {REPO: ["main"]}},
            {"release_branches": {REPO: ""}},
            {"release_files": ["pyproject.toml"]},
            {"release_files": {REPO: "pyproject.toml"}},
            {"release_checks": {REPO: [1]}},
            {"release_dry_run": 1},
        ]:
            with self.subTest(payload=payload):
                with self.assertRaises(SystemExit):
                    self.load(payload)

    def test_plugin_schema_declares_the_release_keys(self):
        # The schema is closed (additionalProperties: false): a key the script
        # reads but the schema omits could not be set through the plugin host.
        manifest = json.loads((ROOT / "plugins/fix-failing-version-bump/.plugin/plugin.json").read_text())
        declared = manifest["automation"]["config_schema"]["properties"]
        for key in ("release_branches", "release_files", "release_checks", "release_dry_run"):
            self.assertIn(key, declared)

    def test_the_pin_bump_deployment_lists_the_files_of_every_release_it_merges(self):
        (config,) = ROOT.glob("automations/fix-failing-version-bump-bump-pins-*/tarball/config.json")
        module = load_module(config.with_name("main.py"))
        self.assertTrue(module.RELEASE_BRANCHES)
        self.assertTrue(module.RELEASE_DRY_RUN)
        polled = [entry["repo"] for entry in module.REPOS]
        for repo in module.RELEASE_BRANCHES:
            self.assertIn(repo, polled)
            self.assertTrue(module.RELEASE_FILES.get(repo), repo)

    def test_the_dependabot_deployment_merges_no_release(self):
        (config,) = ROOT.glob("automations/fix-failing-version-bump-dependabot-*/tarball/config.json")
        module = load_module(config.with_name("main.py"))
        self.assertEqual(module.RELEASE_BRANCHES, {})


if __name__ == "__main__":
    unittest.main()
