# Weekly Automation Improvement

A standalone prototype for OSS-14275. Flow-1 analyzes eligible automation traces
in self-hosted Laminar. Each week, the worker aggregates findings **per automation**,
asks Luna to identify focused repair topics, then gives a full OpenHands agent
powered by Sol 6.1 one focused repair task
per topic. **A new draft PR requires executed baseline/candidate evidence and an
independent review of whether that evidence supports the repair.** Unsupported,
regressed or inconclusive candidates remain pending. Merges and deployment require
human review.

## Weekly path

1. Read persisted Flow-1 findings from self-hosted Laminar and retain unresolved
   findings in the private backlog. Group by automation, then cluster/signature.
2. Luna selects focused repair topics from compact cards. Counts are facts;
   detector diagnoses and cluster membership remain leads to investigate.
3. Sol inspects the original evidence and approved source, using raw-read tools
   when needed. It proposes one focused repair or declines. Repeating an existing
   instruction without an enforceable improvement is not sufficient.
4. Sol validates the real code with regressions, controls and original inputs
   where feasible. The publisher reruns identical checks on baseline/candidate
   code; an independent review inspects the evidence. Configured historical
   replay additionally checks task outcomes and actual deliverables.
5. Publish a labeled repair draft only when paired evidence supports it. Reports
   distinguish tested mechanisms, task improvements, regressions and coverage gaps.
   Unproven findings remain pending; merge and deployment require human review.

`validation_sample_size` is a user-configurable positive integer, default **5**:
up to N topic-linked negative conversations plus N random completed no-finding
conversations. Inputs are frozen before proposing the patch, with no replacement
after failure. There is no separate gold-label annotation/adjudication pipeline.
The historical execution adapter currently supports issue triage. Explicit reviewer
fault tests provide narrower publication/factual-claim evidence; they cannot stand
in for historical-input coverage. See the [validation setup](../../evals/automation-repair/README.md).

Processing continues across topic batches until no actionable topics remain or a
resource threshold is reached. There is no total topic/PR-count quota. Each topic
has its own source snapshot, evidence, patch and publication reservation. Existing
PRs block duplicates; failed publication releases its reservation. Private receipts
and candidate bundles support recovery without silently rerolling failed cases.

## Issue as the shared brief

Each selected repair topic gets one issue in the same repository as its repair PR.
The issue uses fixed sections: **Goal**, **Problem and impact**, **Scope and source**,
**Evidence**, **Validation**, and **Attempts and decisions**. Existing topic issues
are reused. Human additions are preserved; a closed issue is not silently reopened.

The public issue contains source/trace links and immutable evidence catalogs. Original
tasks, approved source, proposals, failed patches, replay prompts, tool events, final
state and comparison results stay private in
`state_dir/issue-evidence/<owner>/<repository>/<topic>/<content-hash>.json`.
Files are secret-redacted, content-checked and owner-only. Catalogs expose references,
never raw private content. They survive cleanup of temporary repair worktrees.

Sol receives the issue brief and can use `read_issue(section, offset)`,
`read_issue_comment(comment_id, offset)` and `read_issue_artifact(reference, offset)`.
These page through the named issue and that topic's private files. The OpenHands
agent also has its normal terminal and filesystem access.
A revision reads the same issue, including the previous attempt's replay artifacts.
The PR links back to its issue. Dry runs create only local previews.

The worker uses its existing GitHub credential, Laminar key and local file access.
Set optional `evidence_location` to an authenticated worker link/SSH instructions so
standalone agents know where files live. An agent outside the worker needs authorized
VM access; public GitHub access alone cannot retrieve private evidence. These files
are stored on the current worker, not a separate artifact service; retain its state
directory when replacing the VM.

[Issue #62](https://github.com/OpenHands/oss-agent-canvas/issues/62) indexes the
native-agent reporting repair in [PR #66](https://github.com/OpenHands/oss-agent-canvas/pull/66):
14 regressions fail before and pass after, with 19 controls passing on both.
Recorded-event projections cover two negatives and five randomly sampled
non-negative inputs. A real SDK/server check verified persisted report delivery.
This demonstrates a reporting mechanism, not general model-quality improvement.

## Configure and run

Copy [weekly.example.json](weekly.example.json) outside git. Configure the project,
Signal, endpoints, existing canonical checkout, approved repository/base branch,
private state directory and GitHub secret name. Create the repository label `automation-improvement`
(already installed in `OpenHands/oss-agent-canvas`). `triage_model` and `repair_model`
are configurable. `triage_batch_size` controls topics returned per Luna call
(default three), not a total PR limit. The obsolete `max_topics_per_automation`
setting no longer limits output. Configure the global `comparative_validation`
runner and use `require_improvement: true` (the default). `validate_existing_repairs`
refreshes descriptions for existing repair PRs. `validation_sample_size: 5` is the
default number of distinct inputs per cohort, not repetitions. At most 2N inputs
produce 4N agent executions per repetition. Increase execution budgets when raising
N; timeouts remain inconclusive. Keep the credentials file owner-only:

```json
{"LMNR_PROJECT_API_KEY":"...","LITELLM_API_KEY":"..."}
```

The worker honors `AUTOMATION_CONFIG_PATH` and otherwise defaults to
`~/.config/automation-improvement/config.json`. On the OSS
VM it reads the configured GitHub secret through the native Agent Server session;
elsewhere set `GH_TOKEN` privately. The Flow-1 Cloud key stays in Laminar's LLM
profile. The worker only needs the SQL-capable self-hosted project key, proxy key
and existing GitHub access. Keep keys outside git and global agent profiles.

```bash
AUTOMATION_API_URL=http://localhost:8000/api/automation bash automations/weekly-automation-improvement/tarball/setup.sh

automations/weekly-automation-improvement/tarball/.venv/bin/python automations/weekly-automation-improvement/tarball/weekly.py \
  --config /private/weekly.json --dry-run

automations/weekly-automation-improvement/tarball/.venv/bin/python automations/weekly-automation-improvement/tarball/weekly.py \
  --config /private/weekly.json
```

`--limit 1..3` bounds representative conversations **per topic**, not the
number of findings collected. Dry runs spend inference tokens but neither publish
nor change the backlog, handled-event ledger or weekly reservations. The YAML
runs `.venv/bin/python weekly.py` on Mondays at 08:00 UTC after installing the
service's SDK version, following the other SDK automations. Owner configuration must exist.
Native completion callbacks follow the existing automation convention.

Current plugin automations resolve to their declared repository, branch and
`plugins/<name>` directory. Legacy bundles need a unique byte match for all
Python/text files under `automations/<name>/tarball`. Foreign/unmapped/ambiguous
sources cannot publish. This MVP supports one approved target repository and
source scope is restricted to one automation per repair. It does not prove
historical executed source equals current source; context and PRs disclose this.

## State and publication

A process lock prevents overlap. `backlog` stores pending event records, `events`
stores assessed event IDs with their topic IDs, and `topic_weeks` stores completed
or reserved topic investigations. Repeated batches can produce any number of
independently useful PRs; the ledger deduplicates topics rather than imposing a
weekly count quota.

Before publication, reserve only that topic's slot atomically. An uncertain result
blocks that topic rather than its siblings. Branch names derive from automation
ID, topic ID and ISO week. Topic identity hashes the automation UUID and a stable,
short semantic key; it does not depend on event IDs, cluster labels or the week.
PR bodies carry automation/topic/key/week/event markers. An open PR with the same
topic blocks another PR across weeks; a closed/merged PR blocks only its creation
week, allowing later regressions to be investigated. PR discovery/reuse remains
scoped to the configured base branch, and remote branches are never overwritten.

GitHub markers recover individual weekly topic slots if local state is lost.
Legacy handled events remain handled; the existing-repair validator can add evidence
without reopening their original findings. Old completed automation-wide slots do not
block unrelated new topics; uncertain old publication reservations remain
conservative. Existing PR descriptions and completed topic keys are supplied to
Luna and Sol. A model can still misclassify or rename an issue: this is not a
complete semantic deduplicator, and cluster membership does not prove root cause.

No finding is automatically labeled a confirmed defect. Recurrence counts describe
the pending backlog, not validated failure rates. Human review controls merge and
deployment. SDK/provider/infrastructure leads can be reported, but this publisher
only permits workflow repairs in the configured automation repository.

## Laminar sampling

[signal.example.json](signal.example.json) is a paused installation template with
the detector payload, execution-finish trigger and automation filter. `sampleRate:
null` means all matching traces. The public numeric range is 1–95; zero and 100
are not substitutes for disabling sampling. Apply either choice explicitly:

```bash
python3 automations/weekly-automation-improvement/tarball/configure_signal.py \
  --config /private/weekly.json --no-sampling

python3 automations/weekly-automation-improvement/tarball/configure_signal.py \
  --config /private/weekly.json --sample-rate 50
```

The setup command changes only sampling and verifies the detector's other settings
by reading it back. Reapplying the same setting creates no version. It does not
backfill history. Production Signal `fadaa9d7-a70a-42df-ab63-04c88f3b77ac` belongs
to project `e41ee96e-851b-45ad-bfe8-b986861c331d`. Rotate the Cloud credential before
November 15. At previously measured volume/usage, full coverage is estimated at
$18–36/month in Flow-1 inference, excluding subscriptions, Luna and Sol.

Reports estimate Flow-1 gross cost from token usage and the dated
[pricing snapshot](tarball/flow1-pricing.json); that is not an invoice. Unknown
usage remains unknown. Luna and Sol retain proxy-reported cost and request latency.
The workload limits below are not a dollar cap.

## Frozen local iteration and bounds

The batch directory contains `collection/input.json` with the complete lightweight
inventory. Each automation has `triage/pass-NNN/triage-input.json` plus saved Luna
requests, reference maps, responses and measurements. Its `topics/TOPIC_ID/` directories each contain a
frozen `snapshot-with-source.json`, source config, Sol request/response, analysis,
mechanical-validation artifacts and `topic-report.json`. `automation-report.json`
collects the topic outcomes.
`weekly-report.json` reports all automation outcomes and explicit collection-error
status when ownership or collection is incomplete. Artifacts/state are owner-only;
redaction is best effort. Never share them without inspection.

Replay a frozen Sol input without Laminar collection or publication:

```bash
python3 automations/weekly-automation-improvement/tarball/main.py \
  --input /private/run/AUTOMATION_UUID/topics/TOPIC_ID/snapshot-with-source.json \
  --source-config /private/run/AUTOMATION_UUID/topics/TOPIC_ID/source-config.json \
  --check-draft --output /private/replay
```

The compactor omits repeated LLM histories, reasoning/provider metadata and empty
wrappers. Original task instructions, cited/adjacent spans, compact whole-session
chronology and final visible steps remain, with explicit clipping. The earlier
Luna trajectory-analysis mode and optional configured behavioral-check path remain
available through `main.py`; the weekly path reuses persisted Flow-1 findings.

## Resource thresholds and request bounds

`max_inference_cost_usd` (default $15) and `max_run_seconds` (default 10800) apply to
the whole invocation. They are checked before new model calls and topic work.
A completed request and its publication can finish, so one request can exceed the
spending threshold. This is not a strict provider billing cap. Proxy-reported
costs are recorded; an unknown/invalid cost stops further inference. Timeouts and
failed requests are not silently retried. Deferred topics stay in the backlog,
and reports distinguish budget exhaustion from "no actionable findings".

Existing-PR comparisons share that budget with new repairs. Comparison runners
read the remaining allowance from `comparison-budget.json` beside the request
and write `comparison/budget.json` with reported spend, unknown-cost count and
`complete: true` after accounting finishes. Missing or interrupted accounting
stops further inference. Paired check timeouts use the remaining deadline and
terminate their process groups when timed out.

Publication reservations begin immediately before pushing a branch. Preparation
failures remain retryable. An uncertain write is reconciled against existing PRs
and its remote branch; unresolved reservations appear in `pending_publications`
and keep the weekly report incomplete.

The repair worker is a normal OpenHands SDK `Agent`/`Conversation` powered by
`openai/gpt-6.1-sol`, with the default terminal, editor, browser, task tracker,
context condenser and subagent tools. It works in an isolated Git worktree,
can create/delete files, install dependencies and run tests. Code changes come
from its actual Git diff. Repository scope and publication stay in the harness.
The coder and its checks run under the worker identity; a Git worktree provides
source separation, not an operating-system sandbox. Use a dedicated development
worker with access appropriate for the source and evidence being inspected.

Configure `repair_agent` for effort, iteration and conversation time limits.
The SDK receives the remaining run budget. The example allows $15 and three
hours for the whole invocation; these are configurable resource limits.
The coding brief includes its deadline and shared budget. Source discovery
preserves the deployed automation's timeout and trigger settings for both the
coder and evidence reviewer, including checks on blocking calls and scan latency.
Projected samples retain their 1,000-span/2 MB bounds, and the agent can page
through the original data. There are no file-count or patch-size restrictions.

The publisher runs the agent's regression and control commands on actual
baseline/candidate worktrees with identical new test assets. A separate Sol
review checks that failures reproduce the reported behavior rather than a
missing import/dependency, and checks public-data safety. Configured historical
comparisons also run. Mechanism tests are labelled with their narrower scope;
PRs remain drafts for review. Credentials are checked before pushing.

```bash
python3 -m pytest automations/weekly-automation-improvement/tests -q
python3 -m ruff check automations/weekly-automation-improvement
```

See [TOPIC_VALIDATION.md](TOPIC_VALIDATION.md), earlier [VALIDATION.md](VALIDATION.md), and rollout tracker
[#50](https://github.com/OpenHands/oss-agent-canvas/issues/50). Detector accuracy,
behavioral benefit, executed revision uptake and post-merge benefit remain unproven.

### On-demand evidence access

The issue is the repair brief, with sections for the goal, scope, evidence,
validation and previous attempts. Public issues contain authenticated references
and metadata; original inputs, prompts, SDK events/completions, tests and logs
stay in owner-only worker artifacts. Their catalogs are linked from the issue.

The agent can use its terminal to read the issue, comments and private artifacts,
or call `search_runs`, `read_signal`, `list_spans` and `read_trace` through the
evidence CLI described in its task. Reads are paged and verify automation
ownership. There is no eight-read limit on the OpenHands agent. It also has
normal GitHub access and repository tools. Up to configurable N=5 negatives and
N random completed non-negative inputs are collected for validation; missing
data and unsupported historical adapters are reported explicitly.

This uses the repository's supported git-synced `automation.yaml` + `tarball/`
layout. Plugin-backed automations are a second supported packaging mechanism;
copying the controller into another package would add a second source to maintain.
The private file ledger and lock deliberately target the current single-VM cron
runtime. A multi-host deployment would need the platform KV dispatcher pattern.
