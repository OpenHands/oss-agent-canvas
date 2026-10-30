# Weekly Automation Improvement

A standalone prototype for OSS-14275. Python reads persisted Flow-1 findings
from self-hosted Laminar and prepares the backlog. One OpenHands triage agent,
defaulting to Luna, starts from a shared weekly issue, investigates the evidence,
merges duplicates and produces a global priority queue. OpenHands repair agents,
defaulting to Sol 6.1, tackle the highest-priority topics in parallel. Before/after
evidence and independent review determine whether improvement is supported.
Coherent patches with failed/inconclusive validation remain drafts. Human review
controls merge and deployment.

## Weekly path

1. Python collects eligible findings, tracks the private backlog and groups
   observations by automation and Laminar cluster/signature. It prepares compact
   counts/examples plus complete selected Signal records and inventory references.
2. A weekly triage issue holds the situation summary, source scope, prior attempts,
   open PRs and immutable private evidence catalogs. The triage agent reads that
   issue first and can page through all preparation output or inspect raw traces
   with scoped read tools. Findings and clusters remain unverified leads.
3. The triage agent selects focused topics and assigns P0–P3 priority using impact,
   recurrence, confidence and likely repairability. Python validates references,
   automation scope and topic identity, then orders one finite queue globally by
   priority, preserving the agent's ordering within a priority level. There is no
   repeated triage loop or topic-count quota.
4. The highest-priority topics launch together in isolated worker processes.
   Each topic gets a linked repair issue in the source repository. Its repair agent investigates source and
   raw evidence, implements one coherent fix or returns an unsupported/unrepaired
   conclusion.
5. The publisher executes identical regression/control assets on baseline and
   candidate source, plus submitted candidate-only safety/integration checks.
   Independent review assesses whether the actual output supports the fix.
   Historical replay runs where supported; narrower mechanism evidence is explicit.
6. Candidates with accepted validation evidence become tagged PRs ready for
   review, with **Before / After Evidence**. Coherent patches with failed or
   inconclusive validation remain drafts with explicit limitations.
   Budget exhaustion defers lower-priority work without marking it attempted.
   Unselected and unsupported-to-repair findings remain pending as appropriate.

`validation_sample_size` is a user-configurable positive integer, default **5**:
up to N topic-linked negative conversations plus N random completed no-finding
conversations. Inputs are frozen before proposing the patch, with no replacement
after failure. There is no separate gold-label annotation/adjudication pipeline.
The historical execution adapter currently supports issue triage. Explicit reviewer
fault tests provide narrower publication/factual-claim evidence; they cannot stand
in for historical-input coverage. See the [validation setup](../../evals/automation-repair/README.md).

`max_prs_per_run` is a top-level positive integer, default **10**. After triage,
Python takes at most that many highest-priority topics and dispatches them in
parallel. Each topic can produce at most one PR. Inconclusive repairs with a usable
patch remain draft; failures without a usable patch remain on the issue. Failed
attempts do not trigger replacement topics. The count is an upper bound, not a
target to fill.

The planner finishes once, before repair execution. The queue continues until it
is exhausted or a resource threshold is reached. Each topic has its own snapshot,
evidence, patch and publication reservation. Existing PRs block duplicates;
pre-publication failures retain retry context. Private receipts and candidate
bundles support recovery without silently rerolling failed cases.

Each topic gets one coding attempt by default. `revise_failed_repair: true`
opts into one revision after concrete validation feedback. A coherent patch with failed validation can be published as a draft; no coherent
repair is also a valid stopping outcome. Evidence-only revisions can
keep the same patch and change the submitted checks. Credible but unrepaired
findings stay pending; unchanged source and Signal evidence do not trigger another
coding attempt, including in a later week. Changed source/evidence or an explicit
topic ID in `retry_topic_ids` allows reconsideration. Triage receives prior
outcomes and reasons, and stops when it cannot select new evidence. Mixed clusters
remain eligible for independent defects. Legacy failed attempts without identity
receipts wait conservatively for fresh Signal evidence or an explicit retry.

## Issue as the shared brief

A weekly triage issue indexes the complete prepared backlog and ranking across
automations. Public summaries contain counts and references; full Python output,
Signals, prompts, tool events and rankings remain in its private catalogs. The
triage agent can read the issue by section and follow its artifact references.

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

[PR #78](https://github.com/OpenHands/oss-agent-canvas/pull/78) reuses a saved native
provider-recovery repair. Two regressions fail before/pass after, 13 controls pass
on both, and the full 48-test candidate suite covers safety, failure notifications
and worker deadlines. Restoring the omitted safety receipts required no new coding
conversation: one independent review reported $0.1447 and publication completed
in 89.3 seconds. This is a recovered candidate, not a new unattended generation
run or historical review-quality replay.

## Configure and run

Copy [weekly.example.json](weekly.example.json) outside git. Configure the project,
Signal, endpoints, existing canonical checkout, approved repository/base branch,
private state directory and GitHub secret name. Create the repository label `automation-improvement`
(already installed in `OpenHands/oss-agent-canvas`). `triage_model` and `repair_model`
are configurable. Set top-level `triage_agent_profile` and `repair_agent_profile`
to saved OpenHands agent profile names. Null retains the built-in Luna triage and
Sol 6.1 repair defaults, with normal OpenHands terminal, editor, browser, task
planning and subagent tools. Each profile owns its model, tools, persona, condenser,
skills, MCP configuration and secret references. The worker resolves profiles
through the native SDK using `AGENT_SERVER_URL` and its session credential. A selected
profile is used as configured; an unavailable profile fails without silently
switching agents. `repair_model` still selects the independent evidence reviewer
and supplies the built-in agent's model when no profile is selected.
Changing profiles does not silently retry previously blocked findings; use
`retry_topic_ids` when a new attempt on unchanged evidence is intended.

The obsolete `triage_batch_size` and `max_topics_per_automation` settings no longer
limit output. Configure the global `comparative_validation`
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
or reserved topic investigations. Ranked plans produce focused PRs up to
`max_prs_per_run`; the ledger deduplicates topics across invocations. Each child
updates its own snapshot. The parent merges only that topic and its handled
events into the canonical ledger; publication reservations use a shared lock.

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
usage remains unknown. Per-stage receipts distinguish SDK-estimated agent costs
from proxy-reported review costs and retain latency/completions. The shared cost
threshold remains a safety limit; incomplete accounting blocks further inference.

## Frozen local iteration and bounds

The batch directory contains `collection/input.json` with the complete lightweight
inventory. Global `triage/` contains the prepared input, reference map, validated
priority queue and weekly issue reference. `triage/triage-agent/` retains the SDK
prompt, context/access manifest, completions, tool events and cost/latency receipt.
Each automation's `topics/TOPIC_ID/` directories contain a
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
Each parallel worker receives the remaining dollar/time allowance at dispatch.
The parent accounts for its spend when it finishes and stops new dispatches when
the threshold is reached. Already-running siblings can continue, so aggregate
spend can exceed the threshold. This is not a strict shared provider billing cap. Proxy-reported
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
The triage agent receives previous topic keys, issue context and conclusions.
Unchanged source/Signal identities block a renamed retry before coding. Explicit
retry requests can recover the original issue namespace from overlapping finding
groups; cluster overlap alone does not override a new global planning decision.

The repair worker is a normal OpenHands SDK `Agent`/`Conversation` powered by
`openai/gpt-6.1-sol`, with the default terminal, editor, browser, task tracker,
context condenser and subagent tools. It works in an isolated Git worktree,
can create/delete files, install dependencies and run tests. Code changes come
from its actual Git diff. Repository scope and publication stay in the harness.
The coder and its checks run under the worker identity; a Git worktree provides
source separation, not an operating-system sandbox. Use a dedicated development
worker with access appropriate for the source and evidence being inspected.

Configure `triage_agent` and `repair_agent` for effort, iteration and conversation
time limits. Triage defaults to a 600-second, 40-iteration best-effort planning
conversation; it does not launch repair agents itself.
For a saved profile, LLM effort/output/retry settings come from that profile;
iteration/time limits and the shared run budget remain controlled by the worker.
Frozen standalone runs also accept `--repair-agent-profile NAME`. Agent profile
name, ID/revision, actual model and tools are recorded in the private receipts.
The SDK receives the remaining run budget. The example allows $100 and three
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
Accepted validation evidence permits ready-for-review publication; failed or
inconclusive results remain drafts. Readiness reflects the stated evidence scope,
including mechanism-only checks, and never authorizes merging. Credentials are
checked before pushing.

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

Transient provider errors get one SDK retry by default. This does not restart the
repair conversation or reroll failed behavioral evaluations. Unknown/unsafe
publication outcomes stay reserved for reconciliation, rather than blindly retrying writes.
