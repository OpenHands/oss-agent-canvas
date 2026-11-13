"""Pin bumps in the fix-failing-version-bump automation.

The release bot's rolling `bump-pins` pull request is fixed and merged under
stricter rules than dependabot's. Everything here runs against an in-memory
GitHub: no test may approve or merge a real pull request.
"""

import io
import unittest
import urllib.error

from test_fix_failing_version_bump import MODULES, ROOT, load_module

BOT = "openhands-release-bot[bot]"
SELF = "all-hands-bot"
REPO = "OpenHands/automation"
CANVAS = "OpenHands/OpenHands"
CANVAS_PINS = [
    "config/defaults.json",
    "package.json",
    "package-lock.json",
    ".agents/skills/local-stack-runtime/references/guide.md",
    "scripts/dev-safe.mjs",
    "scripts/check-sdk-version-sync.mjs",
    "__tests__/scripts/dev-safe.test.ts",
]


def pin_pr(repo=REPO, **overrides):
    pr = {
        "number": 9,
        "state": "open",
        "draft": False,
        "title": "fix: bump SDK to 1.63.0",
        "user": {"login": BOT},
        "labels": [],
        "requested_reviewers": [],
        "head": {"ref": "bump-pins", "sha": "head1", "repo": {"full_name": repo}},
        "base": {"ref": "main", "repo": {"full_name": repo, "default_branch": "main"}},
    }
    pr.update(overrides)
    return pr


def run(name, conclusion="success", status="completed", run_id=1):
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion}


def push(actor, after="head1", kind="push"):
    return {"activity_type": kind, "actor": {"login": actor}, "after": after}


def changed(path, additions=1, deletions=1):
    return {"filename": path, "additions": additions, "deletions": deletions}


class FakeGitHub:
    """One repository with one pin-bump PR. Reads are answered; writes are recorded."""

    def __init__(self, pr=None, required=("unit-tests",), check_runs=None, statuses=(),
                 activity=None, files=None, other_prs=(), refuse_merge=False, broken=()):
        self.pr = pr or pin_pr()
        self.required = list(required)
        self.check_runs = [run("unit-tests")] if check_runs is None else list(check_runs)
        self.statuses = list(statuses)
        self.activity = [push(BOT, kind="branch_creation")] if activity is None else list(activity)
        self.files = [changed("pyproject.toml"), changed("uv.lock", 12, 12)] if files is None else list(files)
        self.other_prs = list(other_prs)
        self.refuse_merge = refuse_merge
        self.broken = broken  # path suffixes whose reads fail
        self.reads = []
        self.writes = []
        self.prompts = []

    def paginate(self, token, path, params=None):
        self.reads.append((path, dict(params or {})))
        if path.endswith("/pulls"):
            if (params or {}).get("head"):
                return [self.pr]
            return [self.pr, *self.other_prs]
        if "/rules/branches/" in path:
            return [
                {"type": "pull_request", "parameters": {}},
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": c} for c in self.required]},
                },
            ]
        if path.endswith("/files"):
            return self.files
        return []

    def request(self, token, method, path, params=None, body=None, accept=None):
        if method != "GET":
            self.writes.append((method, path.split("/", 4)[-1], body))
            if path.endswith("/merge") and self.refuse_merge:
                raise urllib.error.HTTPError(path, 405, "Not allowed", {}, io.BytesIO(b"required check"))
            return {"merged": True}, {}
        self.reads.append((path, dict(params or {})))
        if any(path.endswith(suffix) for suffix in self.broken):
            raise urllib.error.URLError("boom")
        if path.endswith("/check-runs"):
            return {"check_runs": self.check_runs}, {}
        if path.endswith("/status"):
            return {"statuses": self.statuses, "total_count": len(self.statuses)}, {}
        if path.endswith("/activity"):
            return self.activity, {}
        if path.endswith("/reviews"):
            return [], {}
        if "/pulls/" in path:
            number = int(path.rsplit("/", 1)[-1])
            return next(pr for pr in [self.pr, *self.other_prs] if pr["number"] == number), {}
        return {}, {}

    def kinds(self):
        """Writes as short labels, e.g. ["approve", "merge"]."""
        labels = []
        for method, path, body in self.writes:
            if path.endswith("/reviews"):
                labels.append("approve" if body.get("event") == "APPROVE" else "review")
            elif path.endswith("/merge"):
                labels.append("merge")
            elif path.endswith("/requested_reviewers"):
                labels.append("request-review")
            elif path.endswith("/comments"):
                labels.append("comment")
            else:
                labels.append(f"{method} {path}")
        return labels

    def comments(self):
        return [body["body"] for _, path, body in self.writes if path.endswith("/comments")]


def configure(module, github, repo=REPO, **settings):
    """Point one copy of the script at the fake GitHub, with pin bumps on."""
    module.HANDLE_DEPENDABOT = False
    module.PIN_BUMP_BRANCH = "bump-pins"
    module.PIN_BUMP_AUTHOR = BOT
    module.PIN_BUMP_FILES = {REPO: ["pyproject.toml", "uv.lock"], CANVAS: CANVAS_PINS}
    module.PIN_BUMP_CHECKS = {}
    module.PIN_BUMP_DRY_RUN = False
    module.HOLD_LABEL = "release: hold"
    module.MAINTAINERS = ["neubig"]
    module.MAX_MERGES_PER_RUN = 3
    module.MINIMAL_FIX_MAX_FILES = 10
    module.MINIMAL_FIX_MAX_CHANGED_LINES = 100
    for name, value in settings.items():
        setattr(module, name, value)
    module._github_request = github.request
    module._github_paginate = github.paginate

    def create_conversation(agent_url, api_key, prompt):
        github.prompts.append(prompt)
        return "conv-1"

    module.create_conversation = create_conversation
    return module


class PinBumpCase(unittest.TestCase):
    def modules(self, github_factory=FakeGitHub, **settings):
        """Yield (module, github) for every copy of the script, each with a fresh fake."""
        for path in MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                github = github_factory()
                yield configure(load_module(path), github, **settings), github

    def poll(self, module, github, fixes=None, merges=None, active=0, merged=0, repo=REPO):
        fixes = {} if fixes is None else fixes
        merges = {} if merges is None else merges
        result = module._process_pin_bump(
            repo, github.pr, "token", SELF, "http://agent", "key", "http://canvas",
            fixes, merges, active, merged,
        )
        return result, fixes, merges


class SelectionTests(PinBumpCase):
    def test_only_the_bots_pull_request_from_the_pin_branch_into_the_default_branch(self):
        fork = {"ref": "bump-pins", "sha": "head1", "repo": {"full_name": "someone/automation"}}
        release = {"ref": "release-please--branches--main", "sha": "head1", "repo": {"full_name": REPO}}
        old_bump = {"ref": "bump-sdk-1.60.2", "sha": "head1", "repo": {"full_name": REPO}}
        maintenance = {"ref": "release/1.2", "repo": {"full_name": REPO, "default_branch": "main"}}
        for module, _ in self.modules():
            self.assertTrue(module._is_pin_bump(pin_pr(), REPO))
            self.assertFalse(module._is_pin_bump(pin_pr(user={"login": "dependabot[bot]"}), REPO))
            self.assertFalse(module._is_pin_bump(pin_pr(user={"login": "hieptl"}), REPO))
            self.assertFalse(module._is_pin_bump(pin_pr(head=release), REPO))
            self.assertFalse(module._is_pin_bump(pin_pr(head=old_bump), REPO))
            self.assertFalse(module._is_pin_bump(pin_pr(head=fork), REPO))
            self.assertFalse(module._is_pin_bump(pin_pr(base=maintenance), REPO))
            module.PIN_BUMP_BRANCH = ""
            self.assertFalse(module._is_pin_bump(pin_pr(), REPO))


class GateStatusTests(PinBumpCase):
    def gate(self, expected, repo=REPO, checks=None, **github):
        for module, fake in self.modules(lambda: FakeGitHub(**github), PIN_BUMP_CHECKS=checks or {}):
            self.assertEqual(module._gate_status("token", repo, "main", "head1"), expected)

    def test_clear_once_the_required_check_reported_success(self):
        self.gate(("clear", []))

    def test_a_check_that_has_not_reported_is_awaited_not_green(self):
        self.gate(("running", ["unit-tests"]), check_runs=[])

    def test_a_running_check_is_awaited(self):
        self.gate(("running", ["unit-tests"]), check_runs=[run("unit-tests", None, "in_progress")])

    def test_a_failed_required_check_fails_the_gate(self):
        for conclusion in ("failure", "timed_out", "action_required", "startup_failure"):
            self.gate(("failed", ["unit-tests"]), check_runs=[run("unit-tests", conclusion)])

    def test_checks_outside_the_gate_are_ignored(self):
        self.gate(
            ("clear", []),
            check_runs=[
                run("unit-tests"),
                run("Build & Push (amd64)", "failure", run_id=2),
                run("windows-install-e2e", None, "in_progress", run_id=3),
            ],
        )

    def test_extra_checks_join_the_gate(self):
        extra = {CANVAS: ["Check SDK version consistency"]}
        required = ("test-and-build (ubuntu)",)
        built = run("test-and-build (ubuntu)")
        self.gate(("running", ["Check SDK version consistency"]), CANVAS, extra,
                  required=required, check_runs=[built])
        self.gate(("failed", ["Check SDK version consistency"]), CANVAS, extra, required=required,
                  check_runs=[built, run("Check SDK version consistency", "failure", run_id=2)])
        self.gate(("clear", []), CANVAS, extra, required=required,
                  check_runs=[built, run("Check SDK version consistency", run_id=2)])

    def test_the_newest_run_of_a_check_counts(self):
        self.gate(("clear", []), check_runs=[run("unit-tests", "failure", run_id=1), run("unit-tests", run_id=2)])
        self.gate(("failed", ["unit-tests"]),
                  check_runs=[run("unit-tests", run_id=1), run("unit-tests", "failure", run_id=2)])

    def test_a_check_without_a_verdict_is_awaited(self):
        for conclusion in ("cancelled", "skipped", "neutral", "stale"):
            self.gate(("running", ["unit-tests"]), check_runs=[run("unit-tests", conclusion)])

    def test_commit_statuses_count_like_check_runs(self):
        for state, expected in (
            ("success", ("clear", [])),
            ("pending", ("running", ["ci/legacy"])),
            ("failure", ("failed", ["ci/legacy"])),
            ("error", ("failed", ["ci/legacy"])),
        ):
            self.gate(expected, required=("ci/legacy",), check_runs=[],
                      statuses=[{"context": "ci/legacy", "state": state}])

    def test_a_failure_waits_for_the_other_gating_checks(self):
        self.gate(("running", ["lint"]), required=("unit-tests", "lint"),
                  check_runs=[run("unit-tests", "failure"), run("lint", None, "queued", run_id=2)])

    def test_unknown_without_any_gating_check(self):
        self.gate(("unknown", []), required=())

    def test_unknown_when_github_cannot_be_read(self):
        self.gate(("unknown", []), broken=("/check-runs",))


class ForeignPushersTests(PinBumpCase):
    def pushers(self, activity, expected, **github):
        for module, _ in self.modules(lambda: FakeGitHub(activity=activity, **github)):
            self.assertEqual(module._foreign_pushers("token", REPO, "head1", {BOT, SELF}), expected)

    def test_a_branch_the_bot_created_has_none(self):
        self.pushers([push(BOT, kind="branch_creation")], [])

    def test_this_automations_own_fix_commit_is_trusted(self):
        self.pushers([push(SELF), push(BOT, "head0", "force_push")], [])

    def test_a_push_by_anyone_else_is_reported(self):
        self.pushers([push("hieptl"), push(BOT, "head0", "force_push")], ["hieptl"])
        self.pushers(
            [push(SELF), push("hieptl", "h2"), push("enyst", "h1", "force_push"), push("hieptl", "h0"),
             push(BOT, "head0", "branch_creation")],
            ["hieptl", "enyst"],
        )

    def test_pushes_before_the_bots_last_rebuild_do_not_count(self):
        self.pushers(
            [push(BOT, kind="force_push"), push("hieptl", "h1"), push(BOT, "h0", "branch_creation")], [],
        )

    def test_none_when_the_log_cannot_vouch_for_the_head(self):
        self.pushers([], None)
        self.pushers([push(BOT, "another-head", "branch_creation")], None)
        # The bot never rebuilt the branch as far as the log goes back.
        self.pushers([push(SELF), push("hieptl", "h0", "branch_creation")], None)
        self.pushers([push(BOT, kind="branch_creation")], None, broken=("/activity",))


class ClassifyPinBumpTests(PinBumpCase):
    def classify(self, files, expected, repo=REPO, **settings):
        for module, _ in self.modules(lambda: FakeGitHub(files=files), **settings):
            kind, reason = module._classify_pin_bump("token", repo, {"number": 9})
            self.assertEqual(kind, expected, reason)
            self.assertTrue(reason)

    def test_the_repositorys_pin_files_are_version_only(self):
        self.classify([changed("pyproject.toml"), changed("uv.lock", 300, 300)], "version-only")
        self.classify([changed(path, 4, 4) for path in CANVAS_PINS], "version-only", CANVAS)

    def test_only_what_lies_outside_the_pin_files_counts_as_the_fix(self):
        big_lock = changed("uv.lock", 900, 900)
        self.classify([big_lock, changed("src/app.py", 30, 10)], "minimal-fix")
        self.classify([big_lock, changed("src/app.py", 80, 30)], "large")
        self.classify(
            [big_lock, *[changed(f"src/f{i}.py") for i in range(11)]], "large",
        )

    def test_a_pin_file_of_another_repository_is_not_a_pin_file_here(self):
        # config/defaults.json is a Canvas pin file; in automation it is source.
        self.classify([changed("pyproject.toml"), changed("config/defaults.json")], "minimal-fix")

    def test_without_a_list_every_file_counts(self):
        self.classify([changed("pyproject.toml"), changed("uv.lock", 300, 300)], "large", PIN_BUMP_FILES={})

    def test_no_files_is_large(self):
        self.classify([], "large")


class FlowTests(PinBumpCase):
    def test_green_bot_only_bump_is_approved_and_squash_merged(self):
        for module, github in self.modules():
            (conversation, merged), fixes, merges = self.poll(module, github)
            self.assertEqual((conversation, merged), (None, True))
            self.assertEqual(github.kinds(), ["approve", "merge"])
            _, _, merge = github.writes[1]
            self.assertEqual(merge["merge_method"], "squash")
            self.assertEqual(merge["sha"], "head1")
            self.assertEqual(merge["commit_title"], "fix: bump SDK to 1.63.0 (#9)")
            self.assertEqual(list(merges), [f"{REPO}#9@head1"])
            self.assertEqual(fixes, {})

    def test_nothing_happens_while_the_gate_is_not_green(self):
        for module, github in self.modules(lambda: FakeGitHub(check_runs=[])):
            result, fixes, merges = self.poll(module, github)
            self.assertEqual(result, (None, False))
            self.assertEqual((github.writes, fixes, merges), ([], {}, {}))

    def test_hold_label_and_draft_are_left_alone(self):
        held = pin_pr(labels=[{"name": "Release: Hold"}])
        for pr in (held, pin_pr(draft=True)):
            for module, github in self.modules(lambda: FakeGitHub(pr=pr)):
                result, fixes, merges = self.poll(module, github)
                self.assertEqual(result, (None, False))
                self.assertEqual((github.writes, fixes, merges), ([], {}, {}))
                # Not even the checks are read.
                self.assertFalse([path for path, _ in github.reads if path.endswith("/check-runs")])

    def test_a_pull_request_that_stopped_being_a_pin_bump_is_left_alone(self):
        for pr in (pin_pr(state="closed"), pin_pr(user={"login": "hieptl"})):
            for module, github in self.modules(lambda: FakeGitHub(pr=pr)):
                result, _, _ = self.poll(module, github)
                self.assertEqual((result, github.writes), ((None, False), []))

    def test_dry_run_comments_once_and_writes_nothing_else(self):
        for module, github in self.modules(PIN_BUMP_DRY_RUN=True):
            result, fixes, merges = self.poll(module, github)
            self.assertEqual(result, (None, False))
            self.assertEqual(github.kinds(), ["comment"])
            self.assertIn("Dry run", github.comments()[0])
            self.assertIn("approve and squash-merge", github.comments()[0])
            self.assertEqual(merges, {})

            # The next poll of the same head says nothing more.
            self.poll(module, github, fixes, merges)
            self.assertEqual(github.kinds(), ["comment"])

            # Switching dry run off acts on the head that was only noted.
            module.PIN_BUMP_DRY_RUN = False
            result, fixes, merges = self.poll(module, github, fixes, merges)
            self.assertEqual(result, (None, True))
            self.assertEqual(github.kinds(), ["comment", "approve", "merge"])

    def test_a_branch_someone_else_pushed_to_goes_to_a_maintainer(self):
        activity = [push("hieptl"), push(BOT, "head0", "branch_creation")]
        for module, github in self.modules(lambda: FakeGitHub(activity=activity)):
            result, fixes, merges = self.poll(module, github)
            self.assertEqual(result, (None, False))
            self.assertEqual(github.kinds(), ["request-review", "comment"])
            self.assertIn("@neubig", github.comments()[0])
            self.assertIn("pushed to by hieptl", github.comments()[0])
            self.assertEqual(fixes[f"{REPO}#9@head1"]["status"], "handoff")
            self.assertEqual(merges, {})

            # Asked once per head.
            self.poll(module, github, fixes, merges)
            self.assertEqual(github.kinds(), ["request-review", "comment"])

    def test_a_large_companion_change_goes_to_a_maintainer(self):
        files = [changed("pyproject.toml"), changed("src/app.py", 90, 40)]
        for module, github in self.modules(lambda: FakeGitHub(files=files)):
            result, fixes, _ = self.poll(module, github)
            self.assertEqual(result, (None, False))
            self.assertEqual(github.kinds(), ["request-review", "comment"])
            self.assertIn("this pin-bump PR's CI is green", github.comments()[0])

    def test_a_small_companion_fix_by_this_automation_still_merges(self):
        activity = [push(SELF), push(BOT, "head0", "branch_creation")]
        files = [changed("pyproject.toml"), changed("uv.lock", 40, 40), changed("tests/test_sdk.py", 6, 2)]
        for module, github in self.modules(lambda: FakeGitHub(activity=activity, files=files)):
            result, _, _ = self.poll(module, github)
            self.assertEqual(result, (None, True))
            self.assertEqual(github.kinds(), ["approve", "merge"])

    def test_the_merge_waits_until_the_activity_log_covers_the_head(self):
        for module, github in self.modules(lambda: FakeGitHub(activity=[push(BOT, "older", "branch_creation")])):
            result, fixes, merges = self.poll(module, github)
            self.assertEqual(result, (None, False))
            # Nothing written and nothing recorded: the next poll looks again.
            self.assertEqual((github.writes, fixes, merges), ([], {}, {}))

    def test_a_failed_gate_starts_one_fix_conversation(self):
        runs = [run("unit-tests", "failure"), run("Build & Push (amd64)", "failure", run_id=2)]
        for module, github in self.modules(lambda: FakeGitHub(check_runs=runs)):
            (conversation, merged), fixes, _ = self.poll(module, github)
            self.assertEqual((conversation, merged), ("conv-1", False))
            self.assertEqual(github.kinds(), ["comment"])
            self.assertIn("fixing the failing CI on this pin bump", github.comments()[0])
            self.assertEqual(fixes[f"{REPO}#9@head1"]["status"], "active")

            (prompt,) = github.prompts
            self.assertIn("- unit-tests", prompt)
            self.assertNotIn("Build & Push", prompt)
            self.assertIn("Do NOT edit the pin files", prompt)
            self.assertIn("pyproject.toml, uv.lock", prompt)
            self.assertIn("push to `bump-pins`", prompt)
            self.assertNotIn("ependabot", prompt)

            # Once per head.
            result, _, _ = self.poll(module, github, fixes)
            self.assertEqual((result, len(github.prompts)), ((None, False), 1))

    def test_a_head_turned_green_by_rerunning_a_flaky_job_is_merged(self):
        for module, github in self.modules(lambda: FakeGitHub(check_runs=[run("unit-tests", "failure")])):
            _, fixes, merges = self.poll(module, github)
            # The fix conversation ended without a push: it re-ran the job.
            fixes[f"{REPO}#9@head1"]["status"] = "closed"

            # Still red on that head: no second conversation, nothing written.
            writes = len(github.writes)
            result, fixes, merges = self.poll(module, github, fixes, merges)
            self.assertEqual((result, len(github.prompts), len(github.writes)), ((None, False), 1, writes))

            # Green on the same head after the re-run: merged.
            github.check_runs = [run("unit-tests", "failure"), run("unit-tests", run_id=2)]
            result, fixes, merges = self.poll(module, github, fixes, merges)
            self.assertEqual(result, (None, True))
            self.assertEqual(github.kinds()[-2:], ["approve", "merge"])

    def test_a_failed_gate_in_dry_run_only_comments(self):
        for module, github in self.modules(
            lambda: FakeGitHub(check_runs=[run("unit-tests", "failure")]), PIN_BUMP_DRY_RUN=True,
        ):
            result, _, _ = self.poll(module, github)
            self.assertEqual((result, github.prompts), ((None, False), []))
            self.assertEqual(github.kinds(), ["comment"])
            self.assertIn("start a fix conversation for the failing gating checks (unit-tests)", github.comments()[0])

    def test_fix_and_merge_respect_the_per_run_limits(self):
        for module, github in self.modules(lambda: FakeGitHub(check_runs=[run("unit-tests", "failure")])):
            result, fixes, _ = self.poll(module, github, active=2)
            self.assertEqual((result, github.writes, fixes), ((None, False), [], {}))
        for module, github in self.modules():
            result, _, merges = self.poll(module, github, merged=3)
            self.assertEqual((result, github.writes, merges), ((None, False), [], {}))

    def test_a_refused_merge_is_left_to_a_maintainer(self):
        for module, github in self.modules(lambda: FakeGitHub(refuse_merge=True)):
            result, fixes, merges = self.poll(module, github)
            self.assertEqual(result, (None, False))
            self.assertEqual(github.kinds(), ["approve", "merge", "comment"])
            self.assertIn("GitHub refused the merge", github.comments()[0])
            self.assertEqual(fixes[f"{REPO}#9@head1"]["status"], "handoff")
            self.assertEqual(merges, {})


class ProcessRepoTests(PinBumpCase):
    def test_a_pin_bump_only_deployment_asks_for_the_pin_branch_alone(self):
        for module, github in self.modules():
            state = {}
            module._process_repo(REPO, "token", "http://agent", "key", "http://canvas", state, SELF)
            listings = [params for path, params in github.reads if path.endswith("/pulls")]
            self.assertEqual(listings, [{"state": "open", "head": "OpenHands:bump-pins"}])
            self.assertEqual(github.kinds(), ["approve", "merge"])
            self.assertEqual(list(state["repos"][REPO]["merges"]), [f"{REPO}#9@head1"])

    def test_without_a_pin_branch_the_bots_pull_request_is_ignored(self):
        for module, github in self.modules(HANDLE_DEPENDABOT=True, PIN_BUMP_BRANCH=""):
            module._process_repo(REPO, "token", "http://agent", "key", "http://canvas", {}, SELF)
            listings = [params for path, params in github.reads if path.endswith("/pulls")]
            self.assertEqual(len(listings), 1)
            self.assertNotIn("head", listings[0])
            self.assertEqual(github.writes, [])

    def test_dependabot_and_pin_bumps_can_share_one_deployment(self):
        dependabot = pin_pr(
            number=4, user={"login": "dependabot[bot]"},
            head={"ref": "dependabot/pip/x", "sha": "dep1", "repo": {"full_name": REPO}},
        )
        for module, github in self.modules(
            lambda: FakeGitHub(other_prs=[dependabot]), HANDLE_DEPENDABOT=True, AUTO_MERGE_DEPENDABOT=False,
        ):
            module._process_repo(REPO, "token", "http://agent", "key", "http://canvas", {}, SELF)
            # The pin bump merges; the green dependabot PR is untouched while
            # auto_merge_dependabot is off, exactly as before.
            self.assertEqual(github.kinds(), ["approve", "merge"])
            self.assertTrue(github.writes[1][1].startswith("pulls/9/"))


if __name__ == "__main__":
    unittest.main()
