"""
GitHub PR Reviewer - OpenHands Automation Script

Event-driven: one run per `pull_request` webhook delivery, no cron. The run
re-sweeps the whole repo rather than acting only on the PR named in the
payload, so a delivery that is lost (nothing here deduplicates or retries
them) costs latency, not coverage.

Two things still need a *later* look, and without a cron nothing else would
provide it, so `_drive_repo` keeps re-sweeping inside the run's budget:

  * CI still running on a head commit. This is the common case, not an edge
    case -- CI is always in flight immediately after the push or open that
    triggered us, and the gate below deliberately defers rather than spend a
    review on a commit that may be superseded. The clean fix is a
    `check_suite.completed` subscription on the org webhook, which the org
    does not currently deliver; until then this waits it out in-process.
  * A review conversation that has not reached a terminal state.

Both drain through the same pass, and both simply stop at
PR_INLINE_WAIT_BUDGET, leaving the work to the next event for the repo.

Polls a GitHub repository for open pull requests and decides what to do
with each one. release-please release PRs (carrying an `autorelease:` label)
are skipped entirely: they are bot-generated chores that don't need
OpenHands review. Draft PRs are skipped entirely unless `@all-hands-bot` is
currently requested as a reviewer on them, in which case they are treated
like any other PR (see rule 1 below).

  1. If `@all-hands-bot` is currently requested as a reviewer, run a full AI
     review unconditionally - this overrides every check below, including a
     failing/incomplete PR description, red CI, contributor trust level, and
     draft status. Checked fresh on every poll, so a request made after an
     earlier verdict (e.g. a Slack ping) is picked up without needing a new
     commit.
  2. Otherwise, if the PR description is missing a filled-in HUMAN section,
     leave a plain, non-AI comment explaining what to fix. This dedupes on
     the HUMAN section's own content, not the commit: pushing unrelated code
     does not repeat the comment, only editing the description (and still
     failing) does, and the same comment is edited in place rather than
     reposted.
  3. Otherwise, if CI is failing on the head commit, leave a plain comment
     saying so (once per head_sha - a new push is new information here).
  4. Otherwise, if the author is a trusted contributor (an org
     member/collaborator, or already has enough merged PRs in this repo),
     run a full AI review.
  5. Otherwise, notify a Slack channel that the PR is awaiting human review.
     This fires at most once per PR (not per commit), guarded by a live
     check of the Slack channel's own history before sending, so lost local
     state cannot cause a duplicate ping.

Steps 2-4 fire at most once per (PR, head SHA): the unit of work is a
commit, so pushing new commits is what re-triggers them. Step 1 is checked
independently every poll regardless of any earlier verdict, and step 5 is
deliberately decoupled from head_sha entirely.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from maintainer_handoff import request_maintainer_review

# Capture the original urlopen before any call sites are rewritten to the
# retrying wrapper, so the wrapper itself can reach the real implementation
# without recursing.
_orig_urlopen = urllib.request.urlopen


def _retry_urlopen(req, timeout=None, attempts=4):
    """Retry transient network failures with exponential backoff.

    The automation sandbox has intermittent egress to api.github.com and
    slack.com: a connection sometimes times out even though the host is
    reachable seconds later. A single dropped connection used to abort the
    whole run, which - across the many sequential calls a poll makes - made
    nearly every run fail. This retries only genuine connection-level
    failures (URLError that is not an HTTPError, plus socket timeouts).

    HTTPError is deliberately NOT retried: a 401/404/403 is a real response
    from the server, not a transient drop, and retrying a POST (e.g. posting
    a comment) could duplicate it.
    """
    if timeout is None:
        timeout = HTTP_TIMEOUT
    last_exc = None
    for i in range(attempts):
        try:
            return _orig_urlopen(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_exc = exc
            if i < attempts - 1:
                time.sleep(min(2 ** i, 8))
    raise last_exc

# ── Multi-repo configuration ──────────────────────────────────────────────────
# Each repo is processed independently with its own per-repo KV state key
# (state-v6:<repo-slug>), so PR numbers / state buckets never collide across
# repositories - exactly like running one automation per repo, but from a
# single cron. Any field below can be overridden per-entry; omitted fields
# fall back to the module-level defaults.
# This list and the automation's trigger filter must name the same repos.
# They are two halves of one decision and drifted apart: the trigger was
# widened to seven repos while this stayed at five, so events for the three it
# gained reached `_entry_for`, matched nothing, and exited having done nothing.
# Adding a repo means adding it in BOTH places, with a Slack channel for the
# "needs a human" path below.
#
# Deliberately NOT covered, so the next reader does not re-add them as a
# tidy-up:
#   OpenHands/saas-deploy, OpenHands/evaluation -- both private. Reviewing them
#     from this VM points an OSS-scoped token at private code, which is a
#     decision to take explicitly rather than by widening a filter.
#   OpenHands/enterprise -- public, so the token is not the obstacle, but it
#     has no Slack channel for the untrusted-contributor path, which would
#     silently drop those PRs instead of escalating them.
# See OpenHands/automation#418 section A4.
#
# OpenHands/typescript-client was archived into software-agent-sdk on
# 2026-08-30 and is gone from both halves; its leftover state is cleaned up by
# RETIRED_STATE_SLUGS below.
REPOS = [
    {"repo": "OpenHands/extensions", "slack_channel_id": "C0A6U0HTD7X"},
    {"repo": "OpenHands/OpenHands", "slack_channel_id": "C06QT0AGY4W"},
    {"repo": "OpenHands/software-agent-sdk", "slack_channel_id": "C06R25BT5B2"},
    {"repo": "OpenHands/automation", "slack_channel_id": "C0ALCEFB7AQ"},
]
MAINTAINERS = ["neubig", "VascoSch92", "simonrosenberg", "juanmichelini", "palrohitg"]

# Module-level defaults shared by every repo (override per-entry in REPOS).
# REPO is a fallback only — every entry in REPOS supplies its own "repo", so
# this is never actually used; it exists so `_cfg("repo", REPO)` resolves.
REPO = ""
TRIGGER_LABEL = "review-this"          # PR label that triggers an unconditional AI review
TRIGGER_REVIEWER = "all-hands-bot"     # GitHub user assigned as reviewer that triggers an unconditional AI review
REVIEW_TONE = "concise"
REVIEW_STYLE_INSTRUCTIONS = ""
# Pin the public review policy that this live automation is validating. The
# SDK marketplace loader fetches it directly from OpenHands/extensions for
# each review conversation; no review-policy copy lives in this automation.
REVIEW_EXTENSIONS_REF = "d11a77a574ac7c7e349e54799fb6fd8472e618bd"
# Path within each checked-out repository to a repo-specific review guide
# (e.g. the repo's own code-review skill). When the file exists at this path
# relative to the repo root, its contents are injected verbatim into the
# review prompt so the guide is always applied deterministically, rather than
# relying on the spawned agent's skill activation. Set to "" per-repo to
# disable, or to a different path to override.
REPO_REVIEW_GUIDE_PATH = ".agents/skills/custom-codereview-guide.md"
DEFAULT_OPENHANDS_URL = "https://oss-agent-canvas.ngrok.dev"
# How many AI reviews may be in flight at once, PER REPO. Each holds one
# conversation and one repository checkout, so raising this raises disk and
# LLM spend.
MAX_CONCURRENT_REVIEWS = 3
# Handled review keys, newest last. Bounded so the state stays well under the
# KV value limit, and so a key can eventually age out - the Bloom filter this
# replaced could never forget, which made a failed review permanently
# un-retryable.
MAX_SEEN_REVIEWS = 500

# A later GitHub event will retry deferred work. Keep this fallback short so a
# burst of webhook deliveries cannot pin several scanner runs for 20 minutes.
PR_INLINE_WAIT_BUDGET = 60
PR_INLINE_POLL_INTERVAL = 60
# Set by _process_repo when it defers a PR because CI had not finished. It is
# the signal that another sweep is worth doing; without a cron, nothing else
# would come back to that commit.
_CI_PENDING = False
# Consecutive conversation-creation failures tolerated per review before it is
# given up on. Without this a review that always fails would be retried every
# minute forever.
MAX_CREATION_ATTEMPTS = 3
# Every outbound call is bounded: an unbounded read hangs until the
# automation timeout, which used to strand a claim and stall all reviews.
HTTP_TIMEOUT = 30

# Deterministic pre-check, evaluated before any AI review or Slack ping.
HUMAN_SECTION_MIN_CHARS = 20
# Below this many merged PRs in the repo, a non-member/collaborator author
# routes to Slack instead of an automatic AI review.
TRUSTED_CONTRIBUTOR_MIN_MERGED_PRS = 3
SLACK_BOT_TOKEN_SECRET = "SLACK_BOT_TOKEN_OPENHANDS_AI"
# Safety-net lookback for "has this PR already been pinged to Slack" - the
# primary dedup is the slack_notified KV bucket; this only guards against
# re-sending after that state was lost. A review-request channel doesn't
# scroll fast, so a few hundred recent messages is plenty.
MAX_SLACK_HISTORY_PAGES = 3
# Same bounding rationale as MAX_SEEN_REVIEWS, applied to the per-PR
# CI-failure / description-check / Slack-notice buckets below.
MAX_EVALUATED_ENTRIES = 500

# Entry counts alone do NOT bound the stored state. The service enforces its
# KV limit on the automation's whole document, and this automation keeps one
# namespaced entry per repo in REPOS inside that single document - so the
# ceiling is shared by every repo, while each sweep can only see and write
# its own namespace. Five repos times three 500-entry buckets overran the
# 64 KB cap on 2026-08-24; every write after that was rejected with 413, and
# because claiming a review slot IS a state write, no review could be started
# in any repo. Give each namespace a fair share of the document and trim to
# fit before writing, so one busy repo cannot wedge the others.
KV_DOCUMENT_LIMIT = 64 * 1024
KV_BUDGET_SAFETY = 0.85
PER_REPO_STATE_BUDGET = int(KV_DOCUMENT_LIMIT * KV_BUDGET_SAFETY / max(len(REPOS), 1))
# Buckets sacrificed, oldest first, when a namespace is over budget - in the
# order they may be given up. Every one of these is re-derivable from GitHub,
# so dropping an entry costs at most one redundant check: the CI and
# description notices are looked up on the PR and edited in place, a dropped
# slack_notified is backstopped by the Slack history scan, and a dropped
# seen_reviews key is caught by _matching_review_exists before any second
# review is spent. `active` is never trimmed - it is in-flight work, and
# losing it strands a running review.
TRIMMABLE_BUCKETS = ("evaluated", "description_checks", "slack_notified", "seen_reviews")

DONE_DEBOUNCE = 15
MAX_ACTIVE_AGE = 2 * 60 * 60
CAS_RETRIES = 8
TERMINAL_STATUSES = {"idle", "finished", "error", "stuck"}
_TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Per-repo context, set at the start of each repo's processing in main().
# All repo-specific behaviour reads from here so the gate logic, CAS state,
# Slack, and GitHub calls act on the correct repository. It is only mutated
# once per repo iteration in main() and read synchronously within that
# iteration, so there is no cross-repo interleaving.
_CURRENT: dict = {}


def _cfg(key: str, default):
    """Read a config value for the current repo, falling back to the
    module-level default when the per-repo entry does not override it."""
    val = _CURRENT.get(key)
    return val if val is not None else default


def _load_event() -> dict | None:
    """The webhook delivery, or None when the run was dispatched by hand.

    AUTOMATION_EVENT_PAYLOAD wraps it as {"event": {"payload": ...}} for a
    custom webhook source.
    """
    raw = os.environ.get("AUTOMATION_EVENT_PAYLOAD")
    if not raw:
        return None
    try:
        outer = json.loads(raw)
    except Exception as exc:
        print(f"Could not parse AUTOMATION_EVENT_PAYLOAD: {exc}")
        return None
    payload = (outer.get("event") or {}).get("payload")
    return payload if isinstance(payload, dict) else None


def _entry_for(repo_full_name: str) -> dict | None:
    """Map a payload's repository onto its REPOS entry. The org webhook
    delivers every repo in the org, so most events are not ours."""
    for entry in REPOS:
        if entry.get("repo", "").lower() == (repo_full_name or "").lower():
            return entry
    return None


def _state_key() -> str:
    return f"state-v6:{_CURRENT['repo'].replace('/', '__')}"


def _get_env_key() -> str:
    return os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0") or ""


def get_secret(name: str) -> str:
    url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    key = _get_env_key()
    req = urllib.request.Request(
        f"{url}/api/settings/secrets/{name}",
        headers={"X-Session-API-Key": key},
    )
    with _retry_urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode().strip()


def fire_callback(
    status: str = "COMPLETED",
    error: str | None = None,
    conversation_id: str | None = None,
) -> None:
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body: dict = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    if conversation_id:
        body["conversation_id"] = conversation_id
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
        },
    )
    try:
        _retry_urlopen(req, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        print(f"Callback error (non-fatal): {exc}")


# ── Bounded, concurrency-safe state ────────────────────────────────────────────

_KV_TOKEN = os.environ.get("AUTOMATION_KV_TOKEN", "")
_KV_BASE = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")
# v6 re-keys everything from "review-request event id" to "(pr_number,
# head_sha)" via review_key = f"{pr_number}:{head_sha}", so a PR is
# (re-)acted on whenever its code changes rather than only when someone
# explicitly requests @all-hands-bot as a reviewer. It also adds `evaluated`
# for the two outcomes that never spin up an AI conversation. v5 is left
# intact for rollback. The KV key carries the repo slug so each repository
# has its own independent state, exactly like running one automation per
# repo; only the "version" field inside the stored value advances.
def _require_kv() -> None:
    if not (_KV_TOKEN and _KV_BASE):
        raise RuntimeError("Automation KV store is required for crash-safe review deduplication")


def _kv_request(method: str, suffix: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{_KV_BASE}/v1/kv/{_state_key()}{suffix}", data=data,
        headers={"Authorization": f"Bearer {_KV_TOKEN}", "Content-Type": "application/json"}, method=method,
    )
    with _retry_urlopen(req, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


# A repo dropped from REPOS leaves its `state-v6:` key behind in this
# automation's KV document, where nothing will ever trim it again: the
# per-repo budget only applies to a repo still being swept. That dead weight
# then sits on top of the live budget, and an oversized document is how every
# review in every repo got wedged behind 413s on 2026-08-24. Delete retired
# keys instead of leaving them to accumulate.
RETIRED_STATE_SLUGS = (
    "OpenHands__typescript-client",  # archived into software-agent-sdk, 2026-08-30
)


def _delete_retired_state() -> None:
    """Best-effort removal of the state keys of repos no longer in REPOS.

    A 404 is the normal steady state once a key has been removed, so it is
    not worth reporting. Nothing here is fatal: failing to reclaim the space
    is worse than the run not starting only if it is left unnoticed, and the
    next run tries again.
    """
    for slug in RETIRED_STATE_SLUGS:
        key = f"state-v6:{slug}"
        request = urllib.request.Request(
            f"{_KV_BASE}/v1/kv/{key}",
            headers={"Authorization": f"Bearer {_KV_TOKEN}"},
            method="DELETE",
        )
        try:
            with _retry_urlopen(request, timeout=HTTP_TIMEOUT):
                print(f"Reclaimed retired state key {key}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                print(f"Warning: could not remove retired state key {key}: {exc}")
        except Exception as exc:
            print(f"Warning: could not remove retired state key {key}: {exc}")


def _review_key(pr_number: int, head_sha: str) -> str:
    return f"{pr_number}:{head_sha}"


def _compact_active(active: dict | None) -> dict | None:
    if not active:
        return None
    fields = (
        "review_key",
        "pr_number",
        "head_sha",
        "html_url",
        "status",
        "owner_run_id",
        "claimed_at",
        "conversation_id",
        "workspace_dir",
        "last_activity",
        "updated_at",
        "error",
        "terminal_status",
        "finalizer_run_id",
    )
    compact = {field: active[field] for field in fields if field in active}
    for field in ("html_url", "workspace_dir", "error"):
        if field in compact:
            compact[field] = str(compact[field])[:1000]
    return compact


def _compact_evaluated(evaluated: dict | None) -> dict:
    """Bound the per-PR CI-failure record, keyed by PR number, so the
    'fix CI' comment is skipped until the head SHA actually changes.

    This used to also carry `deterministic_failed` and `awaiting_human`
    outcomes, but those now dedup on different keys than head_sha (see
    `description_checks` / `slack_notified` below), so this bucket is
    ci_failed-only.
    """
    evaluated = evaluated or {}
    trimmed = {}
    for key, record in evaluated.items():
        if not isinstance(record, dict):
            continue
        head_sha = record.get("head_sha")
        if not head_sha or record.get("outcome") != "ci_failed":
            continue
        entry = {"head_sha": head_sha, "outcome": "ci_failed", "at": record.get("at")}
        if record.get("comment_id"):
            entry["comment_id"] = record["comment_id"]
        trimmed[str(key)] = entry
    if len(trimmed) > MAX_EVALUATED_ENTRIES:
        trimmed = dict(list(trimmed.items())[-MAX_EVALUATED_ENTRIES:])
    return trimmed


def _compact_description_checks(raw: dict | None) -> dict:
    """Bound the per-PR HUMAN-section check record: the fingerprint of the
    last-flagged text plus the id of the comment raised about it, so a
    repeat failure edits that comment instead of piling on a new one."""
    raw = raw or {}
    trimmed = {}
    for key, record in raw.items():
        if not isinstance(record, dict):
            continue
        fingerprint = record.get("fingerprint")
        if not fingerprint:
            continue
        trimmed[str(key)] = {
            "fingerprint": fingerprint,
            "comment_id": record.get("comment_id"),
            "at": record.get("at"),
        }
    if len(trimmed) > MAX_EVALUATED_ENTRIES:
        trimmed = dict(list(trimmed.items())[-MAX_EVALUATED_ENTRIES:])
    return trimmed


def _compact_slack_notified(raw: dict | None) -> dict:
    """Bound the per-PR 'already pinged Slack' marker. Keyed by PR number
    only (no head_sha) - this is a fact about the PR/process, not the code,
    so it must not reset on every commit."""
    raw = raw or {}
    trimmed = {str(key): True for key, value in raw.items() if value}
    if len(trimmed) > MAX_EVALUATED_ENTRIES:
        trimmed = dict(list(trimmed.items())[-MAX_EVALUATED_ENTRIES:])
    return trimmed


def _state_size(state: dict) -> int:
    """Serialized size of one namespace, measured the way the service
    measures the document it belongs to."""
    return len(json.dumps(state).encode("utf-8"))


def _fit_to_budget(state: dict, budget: int = PER_REPO_STATE_BUDGET) -> dict:
    """Drop the oldest entries until this namespace fits `budget`.

    Buckets are given up in TRIMMABLE_BUCKETS order and only as far as
    necessary, so a repo that fits keeps its full history. Entries are
    removed in chunks rather than one at a time: re-serializing per element
    over a 500-entry bucket is quadratic, and this runs on every save.
    """
    if _state_size(state) <= budget:
        return state
    for bucket in TRIMMABLE_BUCKETS:
        while _state_size(state) > budget:
            value = state.get(bucket)
            if not value:
                break  # exhausted; fall through to the next bucket
            drop = max(1, len(value) // 10)
            if isinstance(value, list):
                del value[:drop]
            elif isinstance(value, dict):
                for key in list(value)[:drop]:
                    value.pop(key, None)
            else:
                break
    return state


def _compact_state(value: dict | None) -> dict:
    """Normalise stored state to version 7.

    Version 5 keyed everything by a GitHub review-request event id. That unit
    of work no longer exists, but each v5 active record already carried its
    own ``pr_number``/``head_sha``, so in-flight reviews are migrated forward
    by re-deriving ``review_key`` from those fields. ``seen_events`` and
    ``attempts`` were keyed by event id with no head_sha to translate, so
    those two buckets simply start empty - at worst a PR already reviewed
    under v5 gets one redundant (idempotent) matching-review check on the
    next pass.

    Version 6 tracked `deterministic_failed`/`awaiting_human` as per-head_sha
    `evaluated` entries. Version 7 splits those into their own buckets with
    different dedup keys: a v6 PR already marked `awaiting_human` carries
    forward as already-Slack-notified (so it isn't re-pinged the moment this
    deploys); `deterministic_failed` entries don't carry forward - the
    fingerprint they'd need doesn't exist under v6, so at worst that PR's
    HUMAN check re-fires its comment once more before settling into the new
    edit-in-place behaviour.
    """
    value = value or {}
    version = value.get("version")
    if version == 7:
        seen_reviews = [str(item) for item in (value.get("seen_reviews") or [])]
        raw_active = value.get("active") or {}
        raw_attempts = value.get("attempts") or {}
        raw_evaluated = value.get("evaluated") or {}
        raw_description_checks = value.get("description_checks") or {}
        raw_slack_notified = value.get("slack_notified") or {}
    elif version == 6:
        seen_reviews = [str(item) for item in (value.get("seen_reviews") or [])]
        raw_active = value.get("active") or {}
        raw_attempts = value.get("attempts") or {}
        raw_evaluated = {}
        raw_description_checks = {}
        raw_slack_notified = {
            str(pr_number): True
            for pr_number, record in (value.get("evaluated") or {}).items()
            if isinstance(record, dict) and record.get("outcome") == "awaiting_human"
        }
    elif version == 5:
        seen_reviews = []
        raw_attempts = {}
        raw_evaluated = {}
        raw_description_checks = {}
        raw_slack_notified = {}
        v5_active = value.get("active") or {}
        raw_active = {}
        for record in (v5_active.values() if isinstance(v5_active, dict) else []):
            if not isinstance(record, dict):
                continue
            pr_number = record.get("pr_number")
            head_sha = record.get("head_sha")
            if pr_number is None or not head_sha:
                continue
            new_key = _review_key(pr_number, head_sha)
            migrated = dict(record)
            migrated["review_key"] = new_key
            raw_active[new_key] = migrated
    else:
        seen_reviews = []
        raw_active = {}
        raw_attempts = {}
        raw_evaluated = {}
        raw_description_checks = {}
        raw_slack_notified = {}

    seen_reviews = list(dict.fromkeys(seen_reviews))[-MAX_SEEN_REVIEWS:]

    if not isinstance(raw_active, dict):
        raw_active = {}
    active: dict = {}
    for key, record in raw_active.items():
        compact = _compact_active(record)
        if not compact:
            continue
        compact.setdefault("review_key", key)
        active[key] = compact

    attempts = {}
    if isinstance(raw_attempts, dict):
        for key, count in raw_attempts.items():
            try:
                attempts[str(key)] = int(count)
            except (TypeError, ValueError):
                continue
    attempts = dict(list(attempts.items())[-MAX_SEEN_REVIEWS:])

    state = {
        "version": 7,
        "repo": _cfg("repo", REPO),
        "seen_reviews": seen_reviews,
        "attempts": attempts,
        "active": active,
        "evaluated": _compact_evaluated(raw_evaluated),
        "description_checks": _compact_description_checks(raw_description_checks),
        "slack_notified": _compact_slack_notified(raw_slack_notified),
    }
    last_completed = value.get("last_completed") if version == 7 else None
    if isinstance(last_completed, dict):
        state["last_completed"] = {
            k: last_completed[k]
            for k in ("review_key", "status", "completed_at", "previous_status")
            if k in last_completed
        }
    return _fit_to_budget(state)


def _load_state() -> tuple[dict, int]:
    _require_kv()
    try:
        data = _kv_request("GET", "?meta=true")
        return _compact_state(data.get("value")), int(data.get("version", 0))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _compact_state(None), 0
        raise


def _save_state(state: dict, version: int) -> bool:
    # version 0 means the key does not exist yet; if_version=0 is rejected on a
    # missing key, so the first write has to be a create (nx=true).
    suffix = "?nx=true" if version == 0 else f"?if_version={version}"
    try:
        _kv_request("PUT", suffix, _compact_state(state))
        return True
    except urllib.error.HTTPError as exc:
        if exc.code in (409, 412):
            return False
        if exc.code == 413:
            # This namespace is within its share but the document is still
            # over the cap, so another repo is oversized (or the limit was
            # lowered). Surrender our own headroom and retry once: an
            # unwritable state means no review can ever be claimed, and the
            # per-PR handler in _process_repo would otherwise swallow this
            # and report the run COMPLETED with nothing done.
            print(
                f"  KV document over its {KV_DOCUMENT_LIMIT}-byte limit; "
                f"halving {_cfg('repo', REPO)} state and retrying once"
            )
            shrunk = _fit_to_budget(_compact_state(state), PER_REPO_STATE_BUDGET // 2)
            _kv_request("PUT", suffix, shrunk)
            return True
        raise


def _claim_review(pr: dict) -> tuple[str, dict] | None:
    """Reserve one of the concurrent slots for reviewing this PR at its
    current head SHA.

    The head_sha is deliberately NOT recorded as seen here. Marking it before
    the conversation exists made any failure permanent - the review could
    never be retried. ``_mark_seen`` runs once the conversation actually
    exists.
    """
    head_sha = _head_sha(pr)
    review_key = _review_key(pr["number"], head_sha)
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        active = state["active"]
        if review_key in state["seen_reviews"]:
            return None
        if review_key in active:
            return None
        if len(active) >= MAX_CONCURRENT_REVIEWS:
            return None
        claim = {
            "review_key": review_key, "pr_number": pr["number"], "head_sha": head_sha,
            "html_url": pr.get("html_url", ""), "status": "claimed",
            "owner_run_id": os.environ.get("AUTOMATION_RUN_ID", ""), "claimed_at": time.time(),
        }
        active[review_key] = claim
        state["active"] = active
        if _save_state(state, version):
            return review_key, claim
    raise RuntimeError("Could not claim review after concurrent state updates")


def _record_failed_attempt(review_key: str) -> int:
    """Count a creation failure; give up once MAX_CREATION_ATTEMPTS is reached."""
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        count = int(state["attempts"].get(review_key, 0)) + 1
        if count >= MAX_CREATION_ATTEMPTS:
            state["attempts"].pop(review_key, None)
            if review_key not in state["seen_reviews"]:
                state["seen_reviews"] = (state["seen_reviews"] + [review_key])[-MAX_SEEN_REVIEWS:]
        else:
            state["attempts"][review_key] = count
        if _save_state(state, version):
            return count
    raise RuntimeError("Could not record creation failure")


def _mark_seen(review_key: str) -> None:
    """Record a review as handled, so it is never queued twice."""
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        if review_key in state["seen_reviews"]:
            return
        state["seen_reviews"] = (state["seen_reviews"] + [review_key])[-MAX_SEEN_REVIEWS:]
        if _save_state(state, version):
            return
    raise RuntimeError("Could not record review as seen")


def _record_ci_failed(pr_number: int, head_sha: str, comment_id=None) -> None:
    """Record a CI-failure comment as posted (and, when known, its comment
    id so a later re-check can edit it in place), so it fires once per
    head_sha instead of on every poll."""
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        record = {"head_sha": head_sha, "outcome": "ci_failed", "at": time.time()}
        if comment_id is not None:
            record["comment_id"] = comment_id
        state["evaluated"][str(pr_number)] = record
        state["evaluated"] = _compact_evaluated(state["evaluated"])
        if _save_state(state, version):
            return
    raise RuntimeError(f"Could not record CI-failed outcome for PR #{pr_number}")


def _ci_already_failed(state: dict, pr_number: int, head_sha: str) -> bool:
    record = state["evaluated"].get(str(pr_number))
    return bool(record and record.get("head_sha") == head_sha)


def _description_check_record(state: dict, pr_number: int) -> dict | None:
    return state["description_checks"].get(str(pr_number))


def _save_description_check(pr_number: int, fingerprint: str, comment_id) -> None:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        state["description_checks"][str(pr_number)] = {
            "fingerprint": fingerprint, "comment_id": comment_id, "at": time.time(),
        }
        state["description_checks"] = _compact_description_checks(state["description_checks"])
        if _save_state(state, version):
            return
    raise RuntimeError(f"Could not record description-check state for PR #{pr_number}")


def _is_slack_notified(state: dict, pr_number: int) -> bool:
    return bool(state["slack_notified"].get(str(pr_number)))


def _mark_slack_notified(pr_number: int) -> None:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        state["slack_notified"][str(pr_number)] = True
        state["slack_notified"] = _compact_slack_notified(state["slack_notified"])
        if _save_state(state, version):
            return
    raise RuntimeError(f"Could not record Slack notification for PR #{pr_number}")


def _claim_slack_notification(pr_number: int) -> bool:
    """Atomically reserve the right to post this PR's Slack notification."""
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        if _is_slack_notified(state, pr_number):
            return False
        state["slack_notified"][str(pr_number)] = True
        state["slack_notified"] = _compact_slack_notified(state["slack_notified"])
        if _save_state(state, version):
            return True
    raise RuntimeError(f"Could not reserve Slack notification for PR #{pr_number}")


def _unmark_slack_notified(pr_number: int) -> None:
    """Release a claim when its message never reached Slack, allowing retry."""
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        if str(pr_number) not in state["slack_notified"]:
            return
        del state["slack_notified"][str(pr_number)]
        if _save_state(state, version):
            return
    raise RuntimeError(f"Could not release Slack notification for PR #{pr_number}")


def _update_active(review_key: str, **changes) -> dict | None:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        active = state["active"]
        record = active.get(review_key)
        if not record:
            return None
        record.update(changes)
        record["updated_at"] = time.time()
        active[review_key] = record
        state["active"] = active
        if _save_state(state, version):
            return record
    raise RuntimeError("Could not update active review after concurrent state updates")


def _acquire_finalizer(review_key: str, terminal_status: str) -> dict | None:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        active = state["active"]
        record = active.get(review_key)
        if not record or record.get("status") != "active":
            return None
        record.update({"status": "finalizing", "terminal_status": terminal_status,
                       "finalizer_run_id": os.environ.get("AUTOMATION_RUN_ID", ""), "updated_at": time.time()})
        active[review_key] = record
        state["active"] = active
        if _save_state(state, version):
            return record
    raise RuntimeError("Could not acquire terminal result ownership")


def _clear_active(review_key: str, terminal_status: str, **details) -> bool:
    for _ in range(CAS_RETRIES):
        state, version = _load_state()
        active = state["active"]
        if review_key not in active:
            return False
        active.pop(review_key, None)
        state["active"] = active
        state["last_completed"] = {"review_key": review_key, "status": terminal_status,
                                   "completed_at": time.time(), **details}
        if _save_state(state, version):
            return True
    raise RuntimeError("Could not clear completed review")


def _release_resources(record: dict, agent_url: str, api_key: str) -> None:
    """Drop the repository checkout a finished review no longer needs.

    The conversation is deliberately KEPT: it is the record of the review and
    is wanted for inspection afterwards. Only the checkout is removed, since
    that is the bulk of the disk use (roughly 1 GB per review) and holds
    nothing that is not already on GitHub.
    """
    conversation_id = record.get("conversation_id")
    workspace_dir = record.get("workspace_dir")
    if not workspace_dir:
        return
    # The checkout is the conversation's working directory. Paths such as
    # "stale" and "expired" can fire while the agent is still running, so
    # confirm it has stopped before removing the ground from under it.
    if conversation_id:
        try:
            status = conversation_status(agent_url, api_key, conversation_id)
        except urllib.error.HTTPError as exc:
            status = "finished" if exc.code == 404 else None
        except Exception:
            status = None
        if status is None:
            print(f"  Could not confirm conversation {conversation_id} has stopped; "
                  f"leaving checkout {workspace_dir} in place")
            return
        if status not in TERMINAL_STATUSES:
            print(f"  Conversation {conversation_id} is still '{status}'; "
                  f"leaving its checkout in place")
            return
        print(f"  Keeping conversation {conversation_id} for inspection")
    try:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        print(f"  Removed checkout {workspace_dir}")
    except Exception as exc:
        print(f"  Warning: could not remove checkout {workspace_dir}: {exc}")


def _remove_review_request(token: str, pr_number: int) -> None:
    """Best-effort: drop @<trigger_reviewer> from requested reviewers once a
    request has been acted on, so the PR doesn't show a stale request in the
    GitHub UI. Failures here are non-fatal - this is cosmetic cleanup, not
    part of the trigger logic.
    """
    repo = _cfg("repo", REPO)
    reviewer = _cfg("trigger_reviewer", TRIGGER_REVIEWER)
    try:
        _github_request(token, "DELETE", f"/repos/{repo}/pulls/{pr_number}/requested_reviewers",
                        body={"reviewers": [reviewer]})
    except Exception:
        pass


def _finish_review(record: dict, terminal_status: str, github_token: str,
                   agent_url: str, api_key: str, **details) -> None:
    """Release everything a completed review holds and free its slot."""
    _release_resources(record, agent_url, api_key)
    review_key = record.get("review_key")
    if review_key:
        try:
            _mark_seen(review_key)
        except Exception as exc:
            print(f"  Warning: could not record {review_key} as seen: {exc}")
    _clear_active(record["review_key"], terminal_status, **details)


def _github_request(
    token: str,
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
    accept: str = "application/vnd.github+json",
) -> tuple:
    url = f"https://api.github.com{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _retry_urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read()
        return (json.loads(raw) if raw.strip() else {}), dict(r.headers)


def _github_paginate(token: str, path: str, params: dict | None = None) -> list:
    results = []
    page = 1
    base_params = dict(params or {})
    base_params.setdefault("per_page", 100)
    while True:
        base_params["page"] = page
        data, _ = _github_request(token, "GET", path, params=base_params)
        if not isinstance(data, list):
            break
        results.extend(data)
        if len(data) < base_params["per_page"]:
            break
        page += 1
    return results


class _MaintainerRepository:
    """Adapt the live reviewer's retrying GitHub transport to the shared selector."""

    def __init__(self, token: str, repository: str):
        self.token = token
        self.repository = repository

    def gh(self, method, path, body=None):
        return _github_request(
            self.token, method, f"/repos/{self.repository}{path}", body=body
        )[0]

    def gh_pages(self, path):
        return _github_paginate(self.token, f"/repos/{self.repository}{path}")

    def api(self, method, path, params=None, body=None):
        return _github_request(
            self.token, method, path, params=params, body=body
        )[0]


# Secrets the spawned review conversation is allowed to look up. The dedicated
# GITHUB_TOKEN_REVIEW secret is the token the review agent uses to post its PR
# review; GITHUB_TOKEN is included as a fallback so the spawned conversation
# can still proceed if the dedicated secret is not registered. Restricting to
# this allowlist (instead of forwarding every configured secret) keeps the
# untrusted-PR conversation on least privilege: it only gets a GitHub token,
# never Slack/Datadog/other secrets. See references/security.md.
_REVIEW_SECRETS_ALLOWLIST = ("GITHUB_TOKEN_REVIEW", "GITHUB_TOKEN")


def _resolve_github_token() -> str:
    """Uses the dedicated GITHUB_TOKEN_REVIEW secret, falling back to the
    generic GITHUB_TOKEN only if the dedicated secret is not registered."""
    for name in _REVIEW_SECRETS_ALLOWLIST:
        try:
            token = get_secret(name)
            if token:
                return token
        except Exception:
            pass
    raise RuntimeError(
        "GITHUB_TOKEN_REVIEW (or GITHUB_TOKEN) secret is not set. "
        "Add a GitHub token with Contents: Read, Pull requests: Read, and Issues: Read/Write permissions."
    )


def _verify_token_and_repo(token: str, repo: str) -> None:
    try:
        user_data, _ = _github_request(token, "GET", "/user")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("The resolved GitHub token (GITHUB_TOKEN_REVIEW or GITHUB_TOKEN) is invalid or expired.") from exc
        raise RuntimeError(f"GitHub /user check failed: {exc.code}") from exc

    print(f"Authenticated as GitHub user: {user_data.get('login', '?')}")

    try:
        _github_request(token, "GET", f"/repos/{repo}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"Repository '{repo}' is not accessible with the current token.") from exc
        raise RuntimeError(f"GitHub /repos/{repo} check failed: {exc.code}") from exc


def _list_open_prs(token: str, repo: str) -> list[dict]:
    return _github_paginate(
        token,
        f"/repos/{repo}/pulls",
        {"state": "open", "sort": "updated", "direction": "desc"},
    )


def _get_pr(token: str, repo: str, pr_number: int) -> dict:
    pr, _ = _github_request(token, "GET", f"/repos/{repo}/pulls/{pr_number}")
    return pr


def _post_github_comment(token: str, repo: str, pr_number: int, body: str) -> None:
    try:
        _github_request(
            token,
            "POST",
            f"/repos/{repo}/issues/{pr_number}/comments",
            body={"body": body},
        )
    except Exception as exc:
        print(f"  Warning: failed to post comment on PR #{pr_number}: {exc}")


def _post_or_update_comment(token: str, pr_number: int, comment_id, body: str) -> int | None:
    """Edit `comment_id` in place if given and still present; otherwise post
    a fresh comment. Returns the id of the comment actually used, or None if
    both the edit and the fallback create failed (caller should not persist
    state in that case, so the next poll retries)."""
    repo = _cfg("repo", REPO)
    if comment_id:
        try:
            _github_request(token, "PATCH", f"/repos/{repo}/issues/comments/{comment_id}", body={"body": body})
            return comment_id
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                print(f"  Warning: failed to update comment {comment_id} on PR #{pr_number}: {exc}")
                return None
            # Comment was deleted/inaccessible; fall through to post fresh.
        except Exception as exc:
            print(f"  Warning: failed to update comment {comment_id} on PR #{pr_number}: {exc}")
            return None
    try:
        result, _ = _github_request(token, "POST", f"/repos/{repo}/issues/{pr_number}/comments", body={"body": body})
        return result.get("id")
    except Exception as exc:
        print(f"  Warning: failed to post comment on PR #{pr_number}: {exc}")
        return None


def _bot_reviews(token: str, pr_number: int) -> list[dict]:
    """Every review the trigger reviewer has left on a PR, oldest first."""
    repo = _cfg("repo", REPO)
    reviewer = _cfg("trigger_reviewer", TRIGGER_REVIEWER).lower()
    return [
        review
        for review in _github_paginate(token, f"/repos/{repo}/pulls/{pr_number}/reviews")
        if (review.get("user") or {}).get("login", "").lower() == reviewer
    ]


def _matching_review_exists(token: str, pr_number: int, head_sha: str) -> bool:
    if not head_sha:
        return False
    return any(r.get("commit_id") == head_sha for r in _bot_reviews(token, pr_number))


def _head_is_approved(token: str, pr_number: int, head_sha: str) -> bool:
    """Whether the bot's last decisive review on this exact head approves it."""
    approved = False
    for review in _bot_reviews(token, pr_number):
        if review.get("commit_id") != head_sha:
            continue
        state = (review.get("state") or "").upper()
        if state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            approved = state == "APPROVED"
    return approved


def _handoff_approved_review(token: str, pr_number: int, head_sha: str) -> None:
    """Request one configured human after a positive current-head bot review."""
    if not _head_is_approved(token, pr_number, head_sha):
        return
    repo = _cfg("repo", REPO)
    pr = _get_pr(token, repo, pr_number)
    if _head_sha(pr) != head_sha:
        return
    selected = request_maintainer_review(
        _MaintainerRepository(token, repo), pr, MAINTAINERS
    )
    print(f"  PR #{pr_number}: positive bot review handed to @{selected}")


# COMMENTED is absent: a later comment does not retract an earlier approval.
_DECISIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


def _standing_approval(token: str, pr_number: int) -> dict | None:
    """The bot's approval that currently gates this PR, or None.

    Mirrors GitHub: the last decisive review wins, and an approval keeps
    counting after the head moves, so there is no head-SHA filter here.
    """
    standing = None
    for review in _bot_reviews(token, pr_number):
        state = (review.get("state") or "").upper()
        if state in _DECISIVE_REVIEW_STATES:
            standing = review if state == "APPROVED" else None
    return standing


DISMISS_ATTEMPTS = 3
_DISMISS_MESSAGE = (
    "Automated approval no longer reflects the pull request's current head commit."
)


def _dismiss_review(token: str, pr_number: int, review: dict) -> bool:
    """Dismiss one of the bot's reviews. Returns whether it is gone."""
    repo = _cfg("repo", REPO)
    review_id = review.get("id")
    if not review_id:
        return False
    for attempt in range(DISMISS_ATTEMPTS):
        try:
            _github_request(
                token, "PUT",
                f"/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/dismissals",
                body={"message": _DISMISS_MESSAGE},
            )
            print(f"  Dismissed review {review_id} on PR #{pr_number}")
            return True
        except Exception as exc:
            print(f"  Warning: could not dismiss review {review_id} on PR #{pr_number} "
                  f"(attempt {attempt + 1}/{DISMISS_ATTEMPTS}): {exc}")
            if attempt < DISMISS_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
    # A lost response looks like a failed dismissal, and the retry then fails
    # on an already dismissed review; ask GitHub for the actual state.
    try:
        still_approving = any(
            r.get("id") == review_id and (r.get("state") or "").upper() == "APPROVED"
            for r in _bot_reviews(token, pr_number)
        )
    except Exception:
        return False
    if not still_approving:
        print(f"  Review {review_id} on PR #{pr_number} is no longer approving; "
              f"treating the dismissal as done")
        return True
    return False


def _dismiss_superseded_approval(token: str, pr_number: int) -> None:
    """Clear a bot approval stranded on a commit the branch has moved past."""
    try:
        approval = _standing_approval(token, pr_number)
    except Exception as exc:
        print(f"  Warning: could not check PR #{pr_number} for a standing approval: {exc}")
        return
    if approval is None:
        return
    print(f"  PR #{pr_number}: bot approval {approval.get('id')} still stands on a "
          f"superseded commit; dismissing")
    if not _dismiss_review(token, pr_number, approval):
        _post_github_comment(token, _cfg("repo", REPO), pr_number, _with_ai_disclosure(
            "⚠️ An earlier automated **approval** on this pull request could not "
            "be dismissed automatically. It does not reflect a review of the "
            "current commit and should be dismissed manually before merging."))


# Budgets for the diff injected into the review prompt. Keeping the diff in
# the prompt (rather than leaving the agent to discover it via ad-hoc API
# calls against a history-less tarball) is what binds the review to the
# actual PR. A missing or wrong diff is the root cause of reviews that
# describe an unrelated change.
MAX_TOTAL_DIFF = 100_000
MAX_PER_FILE_PATCH = 20_000


def _get_pr_files(token: str, pr_number: int) -> list[dict]:
    """Fetch every file in the PR via /pulls/{n}/files, paginating until a
    short page is returned. Each entry carries filename, status, +/- counts,
    and the `patch` text (absent for binary/too-large files)."""
    repo = _cfg("repo", REPO)
    files: list[dict] = []
    page = 1
    while True:
        path = f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
        page_files, _ = _github_request(token, "GET", path)
        if not isinstance(page_files, list) or not page_files:
            break
        files.extend(page_files)
        if len(page_files) < 100:
            break
        page += 1
    return files


def _format_files_manifest(files: list[dict]) -> str:
    """Build the 'Files Changed' manifest shown before the patch block. Every
    file appears exactly once regardless of patch budget; the patch block may
    abbreviate individual patches, the manifest never does."""
    if not files:
        return "## Files Changed\n\n_(no files reported by GitHub)_\n"
    total_add = sum((f.get("additions") or 0) for f in files)
    total_del = sum((f.get("deletions") or 0) for f in files)
    lines = [
        f"## Files Changed ({len(files)} files, +{total_add} / -{total_del})\n",
        "All files in the PR are listed here. If a file's patch is missing or "
        "abbreviated in the Patches section below, read the file from the "
        "workspace (it is checked out) rather than treating it as absent.\n",
    ]
    for f in files:
        path = f.get("filename", "?")
        status = (f.get("status") or "").lower()
        tag = f"[{status}]" if status else ""
        adds = f.get("additions", 0) or 0
        dels = f.get("deletions", 0) or 0
        stats = f"+{adds}/-{dels}" if (adds or dels) else ""
        suffix = ""
        if not f.get("patch") and (adds or dels):
            suffix = "(binary or unavailable, no patch)"
        bits = [f"- `{path}`", tag, stats, suffix]
        lines.append(" ".join(b for b in bits if b))
    return "\n".join(lines) + "\n"


def _format_patches(files: list[dict]) -> str:
    """Assemble per-file patches into a single diff block within budget. Every
    file gets at least its diff header; patches are abbreviated per-file and
    then by total budget, with inline markers so the agent can tell a short
    patch from a missing one."""
    sections: list[str] = []
    total = 0
    for f in files:
        path = f.get("filename", "?")
        previous = f.get("previous_filename")
        status = (f.get("status") or "").lower()
        header_lines = [f"diff --git a/{previous or path} b/{path}"]
        if status == "renamed":
            header_lines.append(f"rename from {previous}")
            header_lines.append(f"rename to {path}")
        if status == "added":
            header_lines.append("new file")
        elif status == "removed":
            header_lines.append("deleted file")
        header = "\n".join(header_lines) + "\n"

        remaining = MAX_TOTAL_DIFF - total
        if remaining <= len(header):
            sections.append(
                header
                + f"[patch omitted: total budget of {MAX_TOTAL_DIFF:,} chars reached; "
                f"read `{path}` from the workspace to inspect]\n"
            )
            total += len(sections[-1])
            continue

        patch = f.get("patch") or ""
        if not patch:
            note = ""
            if status not in {"added", "removed"} or (f.get("additions") or f.get("deletions")):
                note = "[no patch available — likely binary or unrepresentable as text]\n"
            sections.append(header + note)
            total += len(sections[-1])
            continue

        cap = min(MAX_PER_FILE_PATCH, remaining - len(header))
        cut = patch
        truncated = False
        if len(patch) > cap:
            cut_at = patch.rfind("\n", 0, cap)
            cut = patch[: cut_at if cut_at > 0 else cap]
            truncated = True
        section = header + cut
        if not section.endswith("\n"):
            section += "\n"
        if truncated:
            section += (
                f"[patch abbreviated: {len(patch):,} chars total, showing first "
                f"{len(cut):,}; read `{path}` from the workspace to inspect the rest]\n"
            )
        sections.append(section)
        total += len(section)
    return "\n".join(sections)


def _get_pr_diff_payload(token: str, pr_number: int) -> tuple[str, str]:
    """Fetch PR files and produce (manifest, patches) for the prompt."""
    files = _get_pr_files(token, pr_number)
    return _format_files_manifest(files), _format_patches(files)


def _ci_status(token: str, repo: str, head_sha: str) -> str:
    """Returns "running", "failed", "clear", or "unknown" for the given commit.

    A commit with no checks configured at all is "clear" - there is nothing
    to wait for and nothing that failed. On API errors (this VM has
    documented intermittent GitHub egress issues) this returns "unknown"
    rather than failing open to "clear": a transient network error must
    never be indistinguishable from CI actually passing, since this same
    gate decides whether an untrusted contributor's PR goes to Slack or a
    full AI review runs. The caller treats "unknown" exactly like
    "running" - skip the gate for this poll only, re-check next time.
    """
    try:
        page = 1
        any_failed = False
        while True:
            data, _ = _github_request(
                token, "GET", f"/repos/{repo}/commits/{head_sha}/check-runs",
                params={"per_page": 100, "page": page},
            )
            runs = data.get("check_runs", [])
            if any(run.get("status") != "completed" for run in runs):
                return "running"
            if any(run.get("conclusion") in ("failure", "timed_out", "action_required") for run in runs):
                any_failed = True
            if len(runs) < 100:
                break
            page += 1
        status_data, _ = _github_request(token, "GET", f"/repos/{repo}/commits/{head_sha}/status")
        if int(status_data.get("total_count", 0)) > 0:
            legacy_state = status_data.get("state")
            if legacy_state == "pending":
                return "running"
            if legacy_state == "failure":
                any_failed = True
        return "failed" if any_failed else "clear"
    except Exception as exc:
        print(f"  Warning: could not determine CI status for {head_sha[:12]}: {exc}")
        return "unknown"


def _oh_request(agent_url: str, api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    url = f"{agent_url}{path}"
    headers = {"X-Session-API-Key": api_key, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _retry_urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode()
        raise RuntimeError(f"Agent API {method} {path} → {exc.code}: {body_text}") from exc


def _fetch_settings(agent_url: str, api_key: str) -> dict:
    req = urllib.request.Request(
        f"{agent_url}/api/settings",
        headers={"X-Session-API-Key": api_key, "X-Expose-Secrets": "plaintext"},
    )
    with _retry_urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def _get_agent_dict(agent_url: str, api_key: str) -> dict:
    data = _fetch_settings(agent_url, api_key)
    return {
        "kind": "Agent",
        "llm": data.get("agent_settings", {}).get("llm", {}),
        "tools": [{"name": "terminal"}, {"name": "file_editor"}],
        "agent_context": {
            "load_public_skills": False,
            "load_user_skills": True,
            "load_project_skills": True,
            "registered_marketplaces": [
                {
                    "name": "review-policy",
                    "source": "github:OpenHands/extensions",
                    "ref": REVIEW_EXTENSIONS_REF,
                    "auto_load": ["code-review"],
                }
            ],
        },
    }


def _get_mcp_config(agent_url: str, api_key: str) -> dict | None:
    try:
        data = _fetch_settings(agent_url, api_key)
        mcp_config = data.get("agent_settings", {}).get("mcp_config")
        if isinstance(mcp_config, dict) and mcp_config.get("mcpServers"):
            return mcp_config
    except Exception as exc:
        print(f"Warning: could not fetch MCP config: {exc}")
    return None


def _list_secret_names(agent_url: str, api_key: str) -> list[dict]:
    try:
        result = _oh_request(agent_url, api_key, "GET", "/api/settings/secrets")
        return result.get("secrets", [])
    except Exception as exc:
        print(f"Warning: could not list secrets: {exc}")
        return []


def _build_secrets_payload(agent_url: str, api_key: str) -> dict:
    """Build the secrets forwarded to the spawned review conversation.

    Only the GitHub token secrets in _REVIEW_SECRETS_ALLOWLIST are forwarded,
    on least-privilege grounds: the review conversation only needs a GitHub
    token to post its review, so it must not receive Slack/Datadog/other
    secrets. A dedicated LookupSecret is emitted for each allowlisted name that
    is actually registered, so a missing dedicated secret does not inject a
    broken lookup. GITHUB_TOKEN_REVIEW is exposed to the agent under that name;
    GITHUB_TOKEN, when registered, is exposed under its own name as a fallback.
    """
    registered = {s.get("name", "") for s in _list_secret_names(agent_url, api_key)}
    secrets = {}
    for name in _REVIEW_SECRETS_ALLOWLIST:
        if not name or name not in registered:
            continue
        lookup: dict = {
            "kind": "LookupSecret",
            "url": f"/api/settings/secrets/{name}",
        }
        if api_key:
            lookup["headers"] = {"X-Session-API-Key": api_key}
        secrets[name] = lookup
    return secrets


def _checkout_path(pr_number: int, head_sha: str) -> Path:
    workspace = Path(os.environ.get("WORKSPACE_BASE", "/workspace")).resolve()
    return workspace / "repositories" / f"pr-{pr_number}-{head_sha[:12]}"


def _prepare_repository(github_token: str, pr_number: int, head_sha: str) -> Path:
    """Prepare a real git checkout at the PR's head SHA.

    A git checkout (not a tarball snapshot) is used so the agent can run
    `git diff`, `git log`, and `git show` to self-verify what the PR changed
    and ground its review in the actual diff. The tarball approach left the
    agent with no git history and no way to locally verify the workspace
    matched the stated SHA, which contributed to reviews for unrelated PRs.
    """
    repo = _cfg("repo", REPO)
    checkout = _checkout_path(pr_number, head_sha)
    if checkout.exists():
        shutil.rmtree(checkout)
    checkout.mkdir(parents=True)

    # Use the token-bearing URL for the fetch only, then remove the remote so
    # the credential is never left in the repo config for the agent to see.
    authed_url = f"https://x-access-token:{github_token}@github.com/{repo}.git"
    try:
        subprocess.run(
            ["git", "init", str(checkout)],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "add", "origin", authed_url],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "fetch", "--depth=1", "origin", head_sha],
            check=True, capture_output=True, timeout=HTTP_TIMEOUT,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "checkout", head_sha],
            check=True, capture_output=True, timeout=30,
        )
        # Detached HEAD at the exact SHA; no branch needed. Remove the remote
        # so the token-bearing URL is never left in the repo config.
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "remove", "origin"],
            capture_output=True, timeout=10,
        )
    except Exception:
        shutil.rmtree(checkout, ignore_errors=True)
        raise

    if not (checkout / "AGENTS.md").is_file():
        print(f"  Warning: {repo} has no root AGENTS.md at {head_sha[:12]}")
    return checkout


def create_conversation(
    agent_url: str,
    api_key: str,
    initial_message: str,
    workspace_dir: Path,
) -> str:
    payload: dict = {
        "workspace": {"working_dir": str(workspace_dir)},
        "agent": _get_agent_dict(agent_url, api_key),
        "initial_message": {"content": [{"text": initial_message}]},
    }
    secrets = _build_secrets_payload(agent_url, api_key)
    if secrets:
        payload["secrets"] = secrets
    mcp_config = _get_mcp_config(agent_url, api_key)
    if mcp_config:
        payload["mcp_config"] = mcp_config
    result = _oh_request(agent_url, api_key, "POST", "/api/conversations", payload)
    return result["id"]


def conversation_status(agent_url: str, api_key: str, conv_id: str) -> str:
    result = _oh_request(agent_url, api_key, "GET", f"/api/conversations/{conv_id}")
    return result.get("execution_status", "unknown")


def conversation_final_response(agent_url: str, api_key: str, conv_id: str) -> str:
    result = _oh_request(agent_url, api_key, "GET", f"/api/conversations/{conv_id}/agent_final_response")
    return result.get("response", "")


_TONE_INSTRUCTIONS = {
    "thorough": (
        "Provide a comprehensive review. Cover correctness, security vulnerabilities, "
        "missing or inadequate tests, code style, maintainability, and potential edge cases. "
        "Reference specific files and line numbers where relevant."
    ),
    "concise": (
        "Provide a brief, high-signal review. Focus only on important bugs, security problems, "
        "or significant design flaws. Omit minor style feedback."
    ),
    "friendly": (
        "Provide a constructive, encouraging review. Acknowledge what is done well before "
        "raising concerns while still noting real issues."
    ),
}


def _labels(pr: dict) -> list[str]:
    return [label.get("name", "") for label in pr.get("labels", [])]


def _head_sha(pr: dict) -> str:
    return ((pr.get("head") or {}).get("sha") or "").strip()


def _bot_requested(pr: dict) -> bool:
    reviewer_login = _cfg("trigger_reviewer", TRIGGER_REVIEWER).lower()
    return any(
        (reviewer.get("login") or "").lower() == reviewer_login
        for reviewer in (pr.get("requested_reviewers") or [])
    )


def _has_trigger_label(pr: dict) -> bool:
    """A `review-this` label (or the per-repo override) is an explicit,
    unconditional review request, exactly like assigning @<trigger_reviewer>
    as a reviewer - it overrides the HUMAN-section / CI / trust gates."""
    label = _cfg("trigger_label", TRIGGER_LABEL).lower()
    return any(name.lower() == label for name in _labels(pr))


def _is_release_pr(pr: dict) -> bool:
    """release-please release PRs are bot-generated chores assembled from
    merged commit messages, so the HUMAN-section/CI/trust gates are just
    noise on them. Recognized the same way the release-actions `release-ready`
    workflow does: by the `autorelease:` label prefix release-please adds."""
    return any(name.startswith("autorelease:") for name in _labels(pr))


def _with_ai_disclosure(body: str) -> str:
    disclosure = "_This comment was posted by an AI agent (OpenHands)._"
    body = (body or "").strip()
    if disclosure.lower() in body.lower():
        return body
    return f"{body}\n\n{disclosure}" if body else disclosure


def _with_deterministic_disclosure(body: str) -> str:
    disclosure = "_This is an automated check - no AI was used to generate this comment._"
    body = (body or "").strip()
    if disclosure.lower() in body.lower():
        return body
    return f"{body}\n\n{disclosure}" if body else disclosure


# ── Deterministic checks / trust gating / Slack fallback ──────────────────────

def _extract_section(body: str, start: str, ends: list[str]) -> str | None:
    start_idx = body.find(start)
    if start_idx == -1:
        return None
    start_idx += len(start)
    end_idx = len(body)
    for end in ends:
        idx = body.find(end, start_idx)
        if idx != -1:
            end_idx = min(end_idx, idx)
    return body[start_idx:end_idx]


def _visible_section_text(pr: dict, start: str, ends: list[str]) -> str:
    section = _extract_section(pr.get("body") or "", start, ends)
    if section is None:
        return ""
    return _HTML_COMMENT_RE.sub("", section).strip()


def _human_section_text(pr: dict) -> str:
    return _visible_section_text(pr, "HUMAN:", ["---", "AGENT:"])


def _human_section_issue(pr: dict) -> str | None:
    if len(_human_section_text(pr)) < HUMAN_SECTION_MIN_CHARS:
        return (
            f"the PR description's `HUMAN:` section needs at least {HUMAN_SECTION_MIN_CHARS} "
            "characters describing what you tested, not just the template placeholder"
        )
    return None


def _human_fingerprint(pr: dict) -> str:
    return hashlib.sha256(_human_section_text(pr).encode()).hexdigest()[:16]


def _is_trusted_contributor(token: str, repo: str, pr: dict) -> bool:
    association = (pr.get("author_association") or "").upper()
    if association in _TRUSTED_ASSOCIATIONS:
        return True
    login = (pr.get("user") or {}).get("login", "")
    if not login:
        return False
    query = f"repo:{repo} type:pr is:merged author:{login}"
    result, _ = _github_request(token, "GET", "/search/issues", params={"q": query, "per_page": 1})
    return int(result.get("total_count", 0)) >= TRUSTED_CONTRIBUTOR_MIN_MERGED_PRS


def _existing_description_comment(token: str, pr_number: int) -> dict | None:
    """Safety net for first run after a state migration: scan the PR's own
    comments for an existing bot HUMAN-section notice. Returns its id (plus
    the current fingerprint) so the caller can record it as already-handled
    instead of posting a duplicate. The comment body does not embed the
    fingerprint, so this matches on the deterministic disclosure + notice
    prefix; the caller still keys the recorded state on the live fingerprint."""
    repo = _cfg("repo", REPO)
    reviewer = _cfg("trigger_reviewer", TRIGGER_REVIEWER).lower()
    comments = _github_paginate(token, f"/repos/{repo}/issues/{pr_number}/comments")
    for comment in comments:
        login = (comment.get("user") or {}).get("login", "")
        if login.lower() != reviewer:
            continue
        body = comment.get("body") or ""
        if "This PR needs a couple of things fixed before OpenHands can review it" in body:
            return {"comment_id": comment.get("id")}
    return None



def _handle_description_issue(token: str, state: dict, pr: dict, issue: str) -> None:
    """Leave (or update in place) a plain, non-AI comment about the HUMAN
    section. Dedupes on the section's own content, not the commit or a
    generic per-PR flag: editing the description and still failing is worth
    a fresh (edited-in-place) comment; pushing unrelated code is not.

    A GitHub-comment scan acts as a safety net against a duplicate HUMAN
    notice after local state was lost (e.g. first run after migrating to a
    shared multi-repo automation): if the bot already posted a notice with
    the same fingerprint, record it rather than post again. This mirrors the
    CI-failing and Slack notice safety nets, so a fresh KV namespace
    self-seeds from GitHub truth on the first poll."""
    pr_number = pr["number"]
    fingerprint = _human_fingerprint(pr)
    record = _description_check_record(state, pr_number)
    if record and record.get("fingerprint") == fingerprint:
        return
    if record is None:
        # No local record at all: the bot may already have commented (e.g.
        # before this automation took over). Reuse that comment id so the
        # notice is edited in place next time instead of posting a dupe.
        existing = _existing_description_comment(token, pr_number)
        if existing:
            _save_description_check(pr_number, fingerprint, existing.get("comment_id"))
            return
    body = _with_deterministic_disclosure(
        "👋 **This PR needs a couple of things fixed before OpenHands can review it:**\n\n"
        f"- {issue}\n\n"
        "Push an update once this is addressed and this check re-runs automatically."
    )
    comment_id = _post_or_update_comment(token, pr_number, record.get("comment_id") if record else None, body)
    if comment_id is not None:
        _save_description_check(pr_number, fingerprint, comment_id)


def _ci_failing_marker(head_sha: str) -> str:
    return f"<!-- ci-failing-notice:{head_sha} -->"


def _leave_ci_failing_comment(token: str, pr: dict, head_sha: str, comment_id=None) -> int | None:
    """Post the CI-failing notice, editing an existing bot notice in place
    when a comment id is known, and returns the id of the comment used (or
    None if both the edit and the fallback create failed). The caller must
    only record the outcome as handled when an id is returned, so a
    transient failure is retried next poll rather than permanently
    suppressed until the next commit."""
    reviewer = _cfg("trigger_reviewer", TRIGGER_REVIEWER)
    body = (
        f"{_ci_failing_marker(head_sha)}\n"
        "🚦 **CI is currently failing on this PR's latest commit.**\n\n"
        "Please fix the failing checks before OpenHands reviews it - this is re-checked "
        "automatically once you push a new commit. (A maintainer can also request "
        f"`@{reviewer}` as a reviewer to have it reviewed regardless of CI status.)"
    )
    return _post_or_update_comment(token, pr["number"], comment_id, _with_deterministic_disclosure(body))


def _ci_failing_notice_posted(token: str, pr_number: int, head_sha: str) -> int | None:
    """Scan the PR's own comments for an existing bot CI-failing notice and
    return its id, or None. Matches on the head_sha-specific marker first,
    then the stable notice text (an older notice from a previous head is the
    same logical comment and must be edited rather than duplicated). Needed
    because local KV state alone can't find a notice posted by an older
    version of this script, or one posted by a concurrent poller run."""
    repo = _cfg("repo", REPO)
    reviewer = _cfg("trigger_reviewer", TRIGGER_REVIEWER).lower()
    marker = _ci_failing_marker(head_sha)
    comments = _github_paginate(token, f"/repos/{repo}/issues/{pr_number}/comments")
    for comment in comments:
        login = (comment.get("user") or {}).get("login", "")
        if login.lower() != reviewer:
            continue
        body = comment.get("body") or ""
        if marker in body or "CI is currently failing on this PR's latest commit" in body:
            return comment.get("id")
    return None


def _notify_ci_failing(token: str, state: dict, pr: dict, head_sha: str) -> None:
    pr_number = pr["number"]
    if _ci_already_failed(state, pr_number, head_sha):
        return
    record = state["evaluated"].get(str(pr_number))
    comment_id = record.get("comment_id") if record else None
    # No id to edit yet: look for an existing notice on the PR (from an older
    # version of this script, a prior state loss, or a concurrent poller) so
    # we edit it in place rather than piling on a duplicate.
    if comment_id is None:
        try:
            comment_id = _ci_failing_notice_posted(token, pr_number, head_sha)
        except Exception as exc:
            print(f"  Warning: could not check existing CI-failing comments for PR #{pr_number}: {exc}")
            # Fall through and still try to post - a possible duplicate is
            # better than silently never notifying because this check is broken.
    comment_id = _leave_ci_failing_comment(token, pr, head_sha, comment_id)
    if comment_id is not None:
        _record_ci_failed(pr_number, head_sha, comment_id)


def _slack_post_message(token: str, channel: str, text: str) -> None:
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": channel, "text": text}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with _retry_urlopen(req, timeout=HTTP_TIMEOUT) as r:
        result = json.loads(r.read())
    if not result.get("ok"):
        raise RuntimeError(f"Slack chat.postMessage failed: {result.get('error')}")


def _slack_history_page(token: str, channel: str, cursor: str | None) -> dict:
    params = {"channel": channel, "limit": 200}
    if cursor:
        params["cursor"] = cursor
    req = urllib.request.Request(
        f"https://slack.com/api/conversations.history?{urlencode(params)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with _retry_urlopen(req, timeout=HTTP_TIMEOUT) as r:
        result = json.loads(r.read())
    if not result.get("ok"):
        raise RuntimeError(f"Slack conversations.history failed: {result.get('error')}")
    return result


def _slack_channel_has_notice(token: str, channel: str, html_url: str) -> bool:
    """Safety net against re-notifying after local state was lost: scan
    recent channel history for a message that already references this PR,
    bounded to MAX_SLACK_HISTORY_PAGES so a busy channel can't turn this
    into an unbounded scan."""
    cursor = None
    for _ in range(MAX_SLACK_HISTORY_PAGES):
        page = _slack_history_page(token, channel, cursor)
        for message in page.get("messages", []):
            if html_url in (message.get("text") or ""):
                return True
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return False


def _maintainer_already_engaged(token: str, pr_number: int) -> bool:
    """True if a human maintainer (org OWNER/MEMBER/COLLABORATOR, excluding
    the bot's own account) has already left an issue comment or a PR review.
    If so, a human is already looking at this PR and a Slack page would be
    redundant noise, even though the bot itself hasn't reviewed it."""
    repo = _cfg("repo", REPO)
    reviewer = _cfg("trigger_reviewer", TRIGGER_REVIEWER).lower()
    comments = _github_paginate(token, f"/repos/{repo}/issues/{pr_number}/comments")
    for comment in comments:
        login = (comment.get("user") or {}).get("login", "")
        if login.lower() == reviewer:
            continue
        if (comment.get("author_association") or "").upper() in _TRUSTED_ASSOCIATIONS:
            return True
    reviews = _github_paginate(token, f"/repos/{repo}/pulls/{pr_number}/reviews")
    for review in reviews:
        login = (review.get("user") or {}).get("login", "")
        if login.lower() == reviewer:
            continue
        if (review.get("author_association") or "").upper() in _TRUSTED_ASSOCIATIONS:
            return True
    return False


def _notify_slack_review_needed(token: str, state: dict, pr: dict) -> None:
    pr_number = pr.get("number")
    if _is_slack_notified(state, pr_number):
        return
    if _maintainer_already_engaged(token, pr_number):
        print(f"  PR #{pr_number}: a maintainer already commented/reviewed; skipping Slack notification")
        return
    slack_channel_id = _cfg("slack_channel_id", "")
    slack_secret = _cfg("slack_bot_token_secret", SLACK_BOT_TOKEN_SECRET)
    if not (slack_channel_id and slack_secret):
        print("  Slack channel/secret not configured; skipping review-needed notification")
        return
    try:
        slack_token = get_secret(slack_secret)
    except Exception as exc:
        print(f"  Warning: could not fetch {slack_secret}: {exc}")
        return
    title = pr.get("title", "(no title)")
    author = (pr.get("user") or {}).get("login", "?")
    html_url = pr.get("html_url", "")
    try:
        already_notified = _slack_channel_has_notice(slack_token, slack_channel_id, html_url)
    except Exception as exc:
        print(f"  Warning: could not check Slack history for PR #{pr_number}: {exc}")
        # Fall through and still try to post - a possible duplicate is better
        # than silently never notifying because history scanning is broken.
        # The reservation below bounds that to one message rather than one
        # message per poll.
        already_notified = False
    if already_notified:
        # The notice is already in the channel. Record that so later polls skip
        # it without another history scan; failing to record it is not a reason
        # to post a second copy.
        try:
            _mark_slack_notified(pr_number)
        except Exception as exc:
            print(f"  Warning: could not record existing Slack notice for PR #{pr_number}: {exc}")
        return
    repo = _cfg("repo", REPO)
    text = (
        f"👋 *{repo}#{pr_number}* needs a human review: <{html_url}|{title}> by @{author}\n"
        "This contributor's PR needs a human review, so OpenHands skipped the automatic AI review."
    )
    # The history check above is not enough when webhook deliveries overlap:
    # both runs can finish scanning before either message exists. Claim through
    # the KV store's compare-and-swap so only one contender may post.
    try:
        claimed = _claim_slack_notification(pr_number)
    except Exception as exc:
        print(f"  Warning: could not reserve Slack notification for PR #{pr_number}: {exc}")
        return
    if not claimed:
        return
    try:
        _slack_post_message(slack_token, slack_channel_id, text)
    except Exception as exc:
        print(f"  Warning: Slack notification failed for PR #{pr_number}: {exc}")
        try:
            _unmark_slack_notified(pr_number)
        except Exception as release_exc:
            print(f"  Warning: could not release the Slack reservation for PR "
                  f"#{pr_number}; it will not be re-notified: {release_exc}")


def _load_repo_review_guide(workspace_dir: Path) -> str | None:
    """Read the repo-specific review guide from the checked-out repository.

    The path is taken from the per-repo config (``repo_review_guide_path``,
    default ``REPO_REVIEW_GUIDE_PATH``). An empty path disables the feature.
    Returns the file contents, or None if the file is absent or unreadable —
    a missing guide is never fatal, the review simply proceeds without it.
    """
    guide_path = _cfg("repo_review_guide_path", REPO_REVIEW_GUIDE_PATH)
    if not guide_path:
        return None
    candidate = workspace_dir / guide_path
    try:
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
    except Exception as exc:
        print(f"  Warning: could not read repo review guide {candidate}: {exc}")
    return None


def _build_review_prompt(
    pr: dict, head_sha: str,
    repo_review_guide: str | None = None,
    files_manifest: str = "",
    patches: str = "",
) -> str:
    repo = _cfg("repo", REPO)
    review_tone = _cfg("review_tone", REVIEW_TONE)
    style_instructions = _cfg("review_style_instructions", REVIEW_STYLE_INSTRUCTIONS)
    number = pr.get("number", "?")
    title = pr.get("title", "(no title)")
    body = (pr.get("body") or "").strip() or "(no description)"
    html_url = pr.get("html_url", "")
    author = (pr.get("user") or {}).get("login", "?")
    base_branch = (pr.get("base") or {}).get("ref", "?")
    head_branch = (pr.get("head") or {}).get("ref", "?")
    label_str = ", ".join(_labels(pr)) or "(none)"
    changed_files = pr.get("changed_files", "?")
    additions = pr.get("additions", "?")
    deletions = pr.get("deletions", "?")
    clone_url = f"https://github.com/{repo}.git"
    tone = _TONE_INSTRUCTIONS.get(review_tone, _TONE_INSTRUCTIONS["thorough"])
    extra = f"\n\nAdditional style instructions:\n{style_instructions}" if style_instructions.strip() else ""
    guide_section = (
        f"\n\nRepo-specific review guide (from {_cfg('repo_review_guide_path', REPO_REVIEW_GUIDE_PATH)}):\n---\n{repo_review_guide}\n---\n"
        if repo_review_guide else ""
    )
    # The diff is injected directly so the review is bound to the actual PR
    # changes, not left to be reconstructed by the agent against a
    # history-less workspace. Individual patches may be abbreviated (look for
    # `[patch abbreviated: ...]`) or omitted (look for `[patch omitted: ...]`)
    # when they exceed the per-file or total budget. Files in the manifest
    # whose patch is missing are still in the PR — read them from the
    # workspace. Do not flag them as missing from the PR.
    diff_section = ""
    if files_manifest or patches:
        diff_section = f"\n{files_manifest}\n## Patches\n\n```diff\n{patches}\n```\n"

    return (
        "/codereview\n\n"
        "Review the GitHub pull request below and publish the review directly "
        "to GitHub. Do not modify files or push commits.\n\n"
        f"Repository : {repo}\n"
        f"Clone URL  : {clone_url}\n"
        f"PR #{number}: \"{title}\"\n"
        f"Author     : @{author}\n"
        f"Base → Head: {base_branch} ← {head_branch}\n"
        f"Head SHA   : {head_sha}\n"
        f"Labels     : {label_str}\n"
        f"Changes    : +{additions} -{deletions} across {changed_files} file(s)\n"
        f"URL        : {html_url}\n"
        f"\nPR Description:\n---\n{body}\n---\n\n"
        "NO UNADDRESSED all-hands-bot REVIEW: look at @all-hands-bot's submitted PR reviews and find the most recent one.\n"
        "   - If all-hands-bot's most recent review was made against the CURRENT head SHA (review.commit_id == head SHA), all-hands-bot has already reviewed the current code and it is waiting on the author — this is an UNADDRESSED review → SKIP.\n"
        "   - If all-hands-bot has NEVER reviewed, OR the head SHA is newer than all-hands-bot's most recent review's commit_id (the author pushed new commits since) → PROCEED to review.\n"
        "In other words, skip if no commits happened on the PR since the last review.\n\n"
        "Required workflow:\n"
        "1. The workspace is already the repository root at the exact Head SHA above, checked out as a real git repository (detached HEAD). Do not clone, fetch, push, or check out other branches. You may use `git log`, `git show`, and `git diff` to inspect history and changes.\n"
        "2. Follow the automatically loaded AGENTS.md guidance and applicable repository/public skills.\n"
        "3. Inspect the PR description, discussion, existing reviews, the diff provided below, and relevant surrounding code in the workspace.\n"
        "   Use `gh` or GitHub REST API calls with `GITHUB_TOKEN_REVIEW` (a GitHub token); never print secret values.\n"
        "4. Ground every finding in the workspace code. Before using an inline location, verify that the path and line are part of the PR diff.\n"
        "5. Publish one GitHub pull-request review using `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`.\n"
        "   Use `commit_id` equal to the Head SHA above. Follow the loaded code-review skill's decision standard: use `event: APPROVE` only when no material finding remains and the risk assessment permits automatic approval; use `event: COMMENT` when human validation is required; use `event: REQUEST_CHANGES` when material findings remain. Put the overall verdict and risk assessment in `body`.\n"
        "   Put each line-specific finding in the review's `comments` array with `path`, `line`, `side: RIGHT`, and `body`.\n"
        "   Only create inline comments for actionable findings on valid changed lines; do not create praise or nitpick threads.\n"
        "6. If a finding cannot be attached to a valid changed line, include it in the review body. If the review API rejects inline positions, retry by posting all findings in the review body.\n"
        "7. Introduce yourself with this disclosure in the first sentence of the review body: `_This review was created by an AI agent (OpenHands) on behalf of the repository maintainers._`\n"
        "8. If no material issues exist, still post a review body saying the change has no material findings, with the risk assessment and disclosure.\n"
        f"\nReview instructions:\n{tone}{extra}{guide_section}{diff_section}\n\n"
        "After GitHub accepts the review, output exactly `GITHUB_REVIEW_POSTED`. "
        "If posting fails after the required fallback, output the complete review text so the caller can post it as a PR comment."
    )


def _process_review_request(
    github_token: str, agent_url: str, api_key: str, openhands_url: str,
    pr: dict, review_key: str, claim: dict,
) -> str | None:
    number = pr["number"]
    head_sha = _head_sha(pr)
    print(f"  Queuing AI review for PR #{number} at {head_sha[:12]}")
    workspace_dir = None
    try:
        workspace_dir = _prepare_repository(github_token, number, head_sha)
        repo_review_guide = _load_repo_review_guide(workspace_dir)
        if repo_review_guide:
            print(f"  Injected repo review guide for PR #{number}")
        files_manifest, patches = "", ""
        try:
            files_manifest, patches = _get_pr_diff_payload(github_token, number)
            print(f"  Injected diff for PR #{number}: {len(files_manifest)+len(patches):,} chars")
        except Exception as exc:
            print(f"  Warning: could not fetch diff for PR #{number}: {exc}")
        conv_id = create_conversation(
            agent_url, api_key,
            _build_review_prompt(pr, head_sha, repo_review_guide, files_manifest, patches),
            workspace_dir,
        )
    except Exception as exc:
        # Free the slot instead of holding it for manual reconciliation. The
        # key is not in the seen list yet, so the next run retries it; holding
        # the slot used to stall every review for the repository.
        if workspace_dir:
            shutil.rmtree(workspace_dir, ignore_errors=True)
        _clear_active(review_key, "creation_failed", error=str(exc)[:500])
        attempts = _record_failed_attempt(review_key)
        if attempts >= MAX_CREATION_ATTEMPTS:
            print(f"  Review creation failed {attempts}x for PR #{number}; giving up: {exc}")
            _post_github_comment(github_token, _cfg("repo", REPO), number, _with_ai_disclosure(
                f"⚠️ **OpenHands could not start a review** for commit `{head_sha[:12]}` "
                f"after {attempts} attempts.\n\n```\n{str(exc)[:500]}\n```"))
            _remove_review_request(github_token, number)
        else:
            print(f"  Review creation failed (attempt {attempts}/{MAX_CREATION_ATTEMPTS}); "
                  f"slot released for retry: {exc}")
        return None
    _update_active(review_key, status="active", conversation_id=conv_id,
                   workspace_dir=str(workspace_dir), last_activity=time.time())
    # Only now is the review durably handled, so record it as seen.
    _mark_seen(review_key)
    try:
        _post_github_comment(github_token, _cfg("repo", REPO), number, _with_ai_disclosure(
            "🤖 **OpenHands is reviewing this PR.**\n\n"
            f"Head commit: `{head_sha}`\n"
            f"View the conversation: {openhands_url}/conversations/{conv_id}"))
    except Exception as exc:
        print(f"  Warning: acknowledgement comment failed for PR #{number}: {exc}")
    return conv_id


def _check_conversation_completion(
    rec: dict, latest_open_prs: dict[int, dict], github_token: str, agent_url: str, api_key: str,
) -> None:
    review_key = rec["review_key"]
    status = rec.get("status")
    age = time.time() - float(rec.get("last_activity") or rec.get("claimed_at") or 0.0)
    # Expiry is checked before any status branch. Previously only "active"
    # records could expire, so a record stuck in "claimed" (run killed mid
    # checkout) or "finalizing" held its slot indefinitely.
    if age > MAX_ACTIVE_AGE:
        # Close the loop on give-up too: say so on the PR and drop the request.
        # Otherwise the PR is marked handled but stays open, so it is polled
        # forever and never reviewed.
        _post_github_comment(github_token, _cfg("repo", REPO), rec["pr_number"], _with_ai_disclosure(
            f"⚠️ **OpenHands gave up on this review** for commit `{str(rec.get('head_sha',''))[:12]}` "
            f"after {MAX_ACTIVE_AGE // 3600}h (last state: `{status}`)."))
        _remove_review_request(github_token, rec["pr_number"])
        _finish_review(rec, "expired", github_token, agent_url, api_key, previous_status=status)
        print(f"  Review {review_key} exceeded {MAX_ACTIVE_AGE}s in state '{status}'; slot released")
        return
    if status == "finalizing":
        if _matching_review_exists(github_token, rec["pr_number"], rec.get("head_sha", "")):
            try:
                _handoff_approved_review(
                    github_token, rec["pr_number"], rec.get("head_sha", "")
                )
            except Exception as exc:
                print(
                    f"  Warning: human review handoff failed for PR "
                    f"#{rec['pr_number']}; will retry: {exc}"
                )
                return
            _remove_review_request(github_token, rec["pr_number"])
            _finish_review(rec, "closed", github_token, agent_url, api_key,
                           conversation_status=rec.get("terminal_status"))
            print(f"  PR #{rec['pr_number']} already has the requested review; finalizing slot cleared")
        return
    if status != "active" or age < DONE_DEBOUNCE:
        return
    conv_id = rec["conversation_id"]
    pr_number = rec["pr_number"]
    reviewed_sha = rec.get("head_sha", "")
    current_pr = latest_open_prs.get(pr_number)
    if not current_pr:
        _finish_review(rec, "closed", github_token, agent_url, api_key)
        return
    current_sha = _head_sha(current_pr)
    if current_sha and reviewed_sha and current_sha != reviewed_sha:
        # Dropping the review leaves any approval it made still gating merge.
        _dismiss_superseded_approval(github_token, pr_number)
        _finish_review(rec, "stale", github_token, agent_url, api_key,
                       stale_reason=f"head changed from {reviewed_sha} to {current_sha}")
        return
    try:
        status = conversation_status(agent_url, api_key, conv_id)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _finish_review(rec, "missing_conversation", github_token, agent_url, api_key)
            print(f"  Conversation {conv_id} no longer exists; slot released")
            return
        print(f"  Warning: could not get status for {conv_id}: {exc}")
        return
    except Exception as exc:
        print(f"  Warning: could not get status for {conv_id}: {exc}")
        return
    if status not in TERMINAL_STATUSES:
        return
    if not _acquire_finalizer(review_key, status):
        return
    try:
        final = conversation_final_response(agent_url, api_key, conv_id)
    except Exception:
        final = ""
    # Trust the GitHub API, not the agent's sentinel: reviews have been
    # reported as posted when no review existed on the PR.
    review_landed = _matching_review_exists(github_token, pr_number, reviewed_sha)
    if status in {"error", "stuck"}:
        _post_github_comment(github_token, _cfg("repo", REPO), pr_number, _with_ai_disclosure(
            f"⚠️ **OpenHands PR Reviewer encountered a problem** at commit `{reviewed_sha[:12]}` "
            f"(status: `{status}`).\n\n{final}".strip()))
    elif not review_landed:
        _post_github_comment(github_token, _cfg("repo", REPO), pr_number, _with_ai_disclosure(
            final or f"✅ **OpenHands completed the review for commit `{reviewed_sha[:12]}`.** No review text was produced."))
        print(f"  PR #{pr_number}: no review found on GitHub; posted the result as a comment")
    else:
        print(f"  PR #{pr_number}: review confirmed on GitHub")
    if review_landed:
        try:
            _handoff_approved_review(github_token, pr_number, reviewed_sha)
        except Exception as exc:
            print(
                f"  Warning: human review handoff failed for PR #{pr_number}; "
                f"will retry: {exc}"
            )
            return
    # Clearing the request is what takes the PR out of GitHub's "review
    # requested" list; it no longer affects whether we act on the PR.
    _remove_review_request(github_token, pr_number)
    _finish_review(rec, "closed", github_token, agent_url, api_key, conversation_status=status)


def _process_repo(
    repo_entry: dict, github_token: str, agent_url: str, api_key: str,
    target_pr_number: int | None = None,
) -> str | None:
    """Sweep one repository end-to-end: reconcile in-flight reviews, then
    evaluate every open PR through the deterministic gates. Each repo has
    its own KV state key, so this is isolated from every other repo."""
    global _CURRENT, _CI_PENDING
    _CURRENT = dict(repo_entry)
    repo = _cfg("repo", REPO)
    print(f"\n=== {repo} ===")
    try:
        openhands_url = get_secret("OPENHANDS_URL").rstrip("/") or _cfg("openhands_url", DEFAULT_OPENHANDS_URL)
    except Exception:
        openhands_url = _cfg("openhands_url", DEFAULT_OPENHANDS_URL)
    # A 401 here is a real misconfiguration (HTTPError, not retried by
    # _retry_urlopen) and must still surface as a hard failure. But a
    # transient connection failure to api.github.com - even one that
    # exhausts _retry_urlopen's backoff during a sustained egress outage -
    # should skip this poll and let the next minute's cron retry, not fail
    # the run. The repo-access check doubles as the connectivity probe.
    try:
        _verify_token_and_repo(github_token, repo)
    except urllib.error.HTTPError:
        raise  # genuine auth/permission problem - surface it
    except Exception as exc:
        print(f"  Skipping {repo}: could not reach GitHub to verify token ({exc}); will retry next poll")
        return None
    try:
        open_prs = _list_open_prs(github_token, repo)
    except Exception as exc:
        print(f"  Skipping {repo}: could not list open PRs ({exc}); will retry next poll")
        return None
    if target_pr_number is not None:
        open_prs = [pr for pr in open_prs if pr.get("number") == target_pr_number]
    latest_open_prs = {pr["number"]: pr for pr in open_prs}
    state, _ = _load_state()
    for record in list(state["active"].values()):
        if target_pr_number is not None and record.get("pr_number") != target_pr_number:
            continue
        try:
            _check_conversation_completion(record, latest_open_prs, github_token, agent_url, api_key)
        except Exception as exc:
            # One bad record must not stop the others from being reconciled.
            print(f"  Warning: completion check failed for {record.get('review_key')}: {exc}")

    state, _ = _load_state()
    in_flight = len(state["active"])
    capacity = MAX_CONCURRENT_REVIEWS - in_flight
    print(f"  {in_flight}/{MAX_CONCURRENT_REVIEWS} review(s) in flight; capacity for {max(capacity, 0)} more")

    last_conversation_id = None
    for pr in open_prs:
        # One PR's failure (e.g. a transient GitHub API timeout - this VM has
        # documented intermittent egress issues) must not stop every other
        # open PR from being evaluated in the same poll, and must not fail
        # the whole run: nothing was recorded for this PR, so it is simply
        # retried next minute.
        try:
            if _is_release_pr(pr):
                continue
            if pr.get("draft") and not (_bot_requested(pr) or _has_trigger_label(pr)):
                continue
            head_sha = _head_sha(pr)
            if not head_sha:
                continue
            review_key = _review_key(pr["number"], head_sha)
            state, _ = _load_state()
            if review_key in state["active"]:
                continue

            fresh_pr = _get_pr(github_token, repo, pr["number"])
            if _is_release_pr(fresh_pr):
                continue
            if fresh_pr.get("draft") and not (_bot_requested(fresh_pr) or _has_trigger_label(fresh_pr)):
                continue
            if _head_sha(fresh_pr) != head_sha:
                # Superseded by a new push between listing and now; the next
                # poll will evaluate whatever the current head_sha is.
                continue

            if _matching_review_exists(github_token, pr["number"], head_sha):
                # Already reviewed at this head (e.g. from before this
                # rollout). Record it and clear any stale reviewer request.
                try:
                    _handoff_approved_review(github_token, pr["number"], head_sha)
                except Exception as exc:
                    print(
                        f"  Warning: human review handoff failed for PR "
                        f"#{pr['number']}; will retry: {exc}"
                    )
                    continue
                _mark_seen(review_key)
                _remove_review_request(github_token, pr["number"])
                print(f"  PR #{pr['number']} already reviewed at {head_sha[:12]}; recorded")
                continue

            if review_key in state["seen_reviews"]:
                continue

            if _bot_requested(fresh_pr) or _has_trigger_label(fresh_pr):
                # Explicit request (reviewer assignment OR `review-this`
                # label) overrides everything else: no HUMAN-section check,
                # no CI gate, no trust gate.
                if capacity <= 0:
                    continue
                claimed = _claim_review(fresh_pr)
                if not claimed:
                    continue
                review_key, claim = claimed
                capacity -= 1
                conversation_id = _process_review_request(
                    github_token, agent_url, api_key, openhands_url, fresh_pr, review_key, claim)
                if conversation_id:
                    last_conversation_id = conversation_id
                continue

            issue = _human_section_issue(fresh_pr)
            if issue:
                _handle_description_issue(github_token, state, fresh_pr, issue)
                _remove_review_request(github_token, pr["number"])
                continue

            ci = _ci_status(github_token, repo, head_sha)
            if ci in ("running", "unknown"):
                # Don't record this: a commit still being built (or a CI
                # status we failed to determine, e.g. a transient GitHub API
                # error) is often superseded by another push within minutes,
                # so re-check on a later sweep rather than spend an AI review
                # or a Slack ping on it. Flagging it is what makes that later
                # sweep happen -- no cron will come back for this commit.
                _CI_PENDING = True
                print(f"  PR #{pr['number']}: CI {ci} at {head_sha[:12]}; will re-check this run")
                continue
            if ci == "failed":
                _notify_ci_failing(github_token, state, fresh_pr, head_sha)
                _remove_review_request(github_token, pr["number"])
                continue

            if _is_trusted_contributor(github_token, repo, fresh_pr):
                if capacity <= 0:
                    continue
                claimed = _claim_review(fresh_pr)
                if not claimed:
                    continue
                review_key, claim = claimed
                capacity -= 1
                conversation_id = _process_review_request(
                    github_token, agent_url, api_key, openhands_url, fresh_pr, review_key, claim)
                if conversation_id:
                    last_conversation_id = conversation_id
            else:
                _notify_slack_review_needed(github_token, state, fresh_pr)
                _remove_review_request(github_token, pr["number"])
        except Exception as exc:
            print(f"  Warning: evaluation failed for PR #{pr.get('number')}: {exc}")
            continue

    return last_conversation_id


def _drive_repo(
    repo_entry: dict, github_token: str, agent_url: str, api_key: str,
    target_pr_number: int | None = None,
) -> str | None:
    """Sweep the repo, then keep sweeping while something is worth waiting
    for, within this run's budget.

    Waiting is what replaces the cron. Two states justify another pass: CI
    that had not finished on some PR's head commit, and a review conversation
    that has not reached a terminal state. Neither has an event we receive,
    so if this run gives up on them the work waits for the repo's next
    `pull_request` delivery.

    A pass that finds nothing pending returns immediately, so the common case
    (an event on a repo with no CI in flight and no active review) costs one
    sweep, not the whole budget.
    """
    global _CI_PENDING
    deadline = time.time() + PR_INLINE_WAIT_BUDGET
    last_conversation_id = None

    while True:
        _CI_PENDING = False
        conv_id = _process_repo(
            repo_entry, github_token, agent_url, api_key, target_pr_number
        )
        if conv_id:
            last_conversation_id = conv_id

        state, _ = _load_state()
        waiting_on = []
        if _CI_PENDING:
            waiting_on.append("CI")
        active_count = sum(
            target_pr_number is None
            or record.get("pr_number") == target_pr_number
            for record in state["active"].values()
        )
        if active_count:
            waiting_on.append(f"{active_count} review(s)")
        if not waiting_on:
            return last_conversation_id
        if time.time() + PR_INLINE_POLL_INTERVAL >= deadline:
            print(f"  Still waiting on {', '.join(waiting_on)} at the "
                  f"{PR_INLINE_WAIT_BUDGET}s budget; the next event for this "
                  "repo picks it up")
            return last_conversation_id

        print(f"  Waiting on {', '.join(waiting_on)}; re-sweeping in "
              f"{PR_INLINE_POLL_INTERVAL}s")
        time.sleep(PR_INLINE_POLL_INTERVAL)


def main() -> str | None:
    _require_kv()
    _delete_retired_state()
    agent_url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    api_key = _get_env_key()
    github_token = _resolve_github_token()

    payload = _load_event()
    target_pr_number = None
    if payload is None:
        # Manual dispatch (or a malformed payload): sweep everything, which is
        # also how to drain the backlog after an outage.
        print("No event payload; sweeping every managed repo")
        entries = list(REPOS)
    else:
        repo_full = (payload.get("repository") or {}).get("full_name") or ""
        entry = _entry_for(repo_full)
        if entry is None:
            # The org webhook delivers every repo, so the trigger filter is
            # what is supposed to narrow this down. Reaching here means the
            # filter admitted a repo REPOS does not handle, i.e. the two have
            # drifted apart again - say so rather than exiting quietly.
            print(f"Ignoring event for unmanaged repo {repo_full!r}: it passed this "
                  f"automation's trigger filter but is not in REPOS, so the trigger "
                  f"and this script disagree and the run can do nothing")
            return None
        target_pr_number = (payload.get("pull_request") or {}).get("number")
        print(f"Event for {repo_full}: action={payload.get('action')!r} "
              f"pr=#{target_pr_number}")
        entries = [entry]

    last_conversation_id = None
    for repo_entry in entries:
        # A failure on one repo must never stop the others being processed.
        # KV/token errors that are genuinely fatal for all repos (e.g. no
        # GITHUB_TOKEN) surface from _resolve_github_token before the loop.
        try:
            conv_id = _drive_repo(
                repo_entry,
                github_token,
                agent_url,
                api_key,
                target_pr_number=target_pr_number,
            )
            if conv_id:
                last_conversation_id = conv_id
        except Exception as exc:
            repo = repo_entry.get("repo", "?")
            print(f"Error processing {repo}: {exc}")

    return last_conversation_id


if __name__ == "__main__":
    try:
        conversation_id = main()
        fire_callback("COMPLETED", conversation_id=conversation_id)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        fire_callback("FAILED", str(exc))
        sys.exit(1)
