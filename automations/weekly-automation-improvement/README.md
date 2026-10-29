# Weekly Automation Improvement

A standalone prototype for OSS-14275. Flow-1 analyzes eligible automation traces
in self-hosted Laminar. Each week, the worker aggregates findings **per automation**,
asks Luna to identify focused repair topics, then asks Sol for one focused patch
per topic. **A new draft PR is published only after a baseline/candidate comparison
demonstrates the targeted improvement on frozen development cases.** Unsupported,
regressed or inconclusive candidates remain pending. Merges and deployment require
human review.

## Weekly path

1. The dedicated Signal runs at execution finish (`conversation.arun`), filtered
   to traces containing `automation.conversation`. Sampling is disabled for full
   coverage of matching instrumented traces; missing telemetry and failed analysis
   still create coverage gaps. It neither ingests traces nor replaces the health monitor.
2. Collect every latest finding event in the previous seven days, with a ten-minute
   arrival buffer. Sampling-only configuration versions are compatible; changed
   instructions, schemas or LLM routes are separate cohorts. Read lightweight
   event/ownership records upfront without loading every full trajectory. Retain
   failed/pending runs and completed no-event counts separately.
3. Merge findings into the existing private JSON backlog. Pending findings survive
   the collection window and weekly boundaries. Deduplicate event IDs, discard
   superseded findings, and group by automation UUID. Incompatible older detector
   cohorts remain visible in the backlog but are not mixed into the current analysis.
4. Within each automation, reuse Laminar leaf clusters; otherwise group by exact
   normalized signature and cause. Empty signatures stay separate. If an event has
   several leaf memberships, use its first sorted leaf once. Count distinct
   conversations so repeated events do not inflate affected-run counts. Cards have
   up to three distinct-conversation examples, outcome/cause counts and recency.
5. Use `openai/gpt-6-luna` to select focused repair topics in batches. Clusters are semantic hints: Luna may merge duplicate groups or split
   a mixed group using different representative events. Each topic gets a title,
   stable key, reason and up to three representative conversations. A single
   event cannot be assigned to two topics, while different events from one
   conversation may support different topics. Luna selects compact `e1`-style
   references; trusted code resolves their event and group IDs from a saved map.
   Unselected findings stay pending.
6. For each topic separately, load its representatives and freeze their evidence,
   the approved source and only that topic's aggregate context. Make a separate
   `openai/gpt-6.1-sol` call. Sol confirms the issue against the task, trace,
   recovery, current source and existing PRs, then proposes one focused patch or
   declines. Independent fixes and incidental cleanup cannot be bundled into
   another topic. An unrelated but potentially valid finding is deferred in the
   backlog; unsupported findings can be rejected.
7. Apply the patch in a detached worktree and check approved file scope,
   whitespace and Python syntax. Commit locally and retain a private Git bundle.
   Select a trusted task contract from the affected source and failure mechanism;
   freeze native inputs, source revisions, model settings and case definitions.
8. Run both versions through the actual SDK against identical isolated GitHub
   worlds. Require a reproduced **target violation**, passing controls on both
   versions, all candidate cases passing and no required-input/runtime gaps.
   Unrelated failures cannot establish the intended improvement. Missing contracts
   or inconclusive results produce no branch push or new PR.
9. A completed behavioral failure may receive one Sol revision with compact case
   feedback, followed by a new comparison. The previous patch must change;
   provider/runtime failures and unchanged patches are not rerolled. Retain both
   attempts and disclose development feedback in the evidence report.
10. Publish one draft per demonstrated topic, labeled `automation-improvement`,
    with **Before / After Evidence already in its description**. Recheck the base
    revision after evaluation. Later topics see the newly created PR. Release the
    reservation when no publication occurred; findings remain pending. Worktrees
    are removed while source bundles and all comparison artifacts remain private.

The worker continues across Luna batches until no actionable topics remain or a
resource threshold is reached. There is no total topic or PR-count quota. Dry runs
perform analysis and mechanical checks without PR writes or backlog changes.
The initial contracts cover issue-priority/readiness decisions, reviewer factual
claims and reviewer publication. Other mechanisms cannot publish until an
appropriate execution contract exists. This is development-case evidence, not a
general automation-quality benchmark. See the [comparison setup](../../evals/automation-repair/README.md).

## Configure and run

Copy [weekly.example.json](weekly.example.json) outside git. Configure the project,
Signal, endpoints, existing canonical checkout, approved repository/base branch,
private state directory and GitHub secret name. Create the repository label `automation-improvement`
(already installed in `OpenHands/oss-agent-canvas`). `triage_model` and `repair_model`
are configurable. `triage_batch_size` controls topics returned per Luna call
(default three), not a total PR limit. The obsolete `max_topics_per_automation`
setting no longer limits output. Configure the global `comparative_validation`
runner and use `require_improvement: true` (the default). `validate_existing_repairs`
refreshes descriptions for existing repair PRs. Keep the credentials file owner-only:

```json
{"LMNR_PROJECT_API_KEY":"...","LITELLM_API_KEY":"..."}
```

The worker defaults to `~/.config/automation-improvement/config.json`. On the OSS
VM it reads the configured GitHub secret through the native Agent Server session;
elsewhere set `GH_TOKEN` privately. The Flow-1 Cloud key stays in Laminar's LLM
profile. The worker only needs the SQL-capable self-hosted project key, proxy key
and existing GitHub access. Keep keys outside git and global agent profiles.

```bash
python3 automations/weekly-automation-improvement/tarball/weekly.py \
  --config /private/weekly.json --dry-run

python3 automations/weekly-automation-improvement/tarball/weekly.py \
  --config /private/weekly.json
```

`--limit 1..3` bounds representative conversations **per topic**, not the
number of findings collected. Dry runs spend inference tokens but neither publish
nor change the backlog, handled-event ledger or weekly reservations. The YAML
runs `python3 weekly.py` on Mondays at 08:00 UTC; owner configuration must exist.
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

`max_inference_cost_usd` (default $1) and `max_run_seconds` (default 900) apply to
the whole invocation. They are checked before new model calls and topic work.
A completed request and its publication can finish, so one request can exceed the
spending threshold. This is not a strict provider billing cap. Proxy-reported
costs are recorded; an unknown/invalid cost stops further inference. Timeouts and
failed requests are not silently retried. Deferred topics stay in the backlog,
and reports distinguish budget exhaustion from "no actionable findings".

Technical bounds remain: 10,000 Signal run rows per seven-day collection window;
representative conversations per topic set by `--limit 1..3`; 30 source files /
250 KB; 20,000 characters per native Flow-1 finding text field; 750 KB per model
request; 4,096 output tokens; one focused proposal and at most four exact source
edits per repair. Each conversation retains the existing 1,000-span, 2 MB
projected-sample and 8 MB HTTP-response limits. Oversized requests fail visibly
with pending data retained. These bounds constrain individual requests/patches,
not how many useful topics the run may publish.

```bash
python3 -m pytest automations/weekly-automation-improvement/tests -q
python3 -m ruff check automations/weekly-automation-improvement
```

See [TOPIC_VALIDATION.md](TOPIC_VALIDATION.md), earlier [VALIDATION.md](VALIDATION.md), and rollout tracker
[#50](https://github.com/OpenHands/oss-agent-canvas/issues/50). Detector accuracy,
behavioral benefit, executed revision uptake and post-merge benefit remain unproven.
