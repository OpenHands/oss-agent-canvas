# Weekly Automation Improvement

A standalone prototype for OSS-14275. Flow-1 analyzes eligible automation traces
in self-hosted Laminar. Each week, the worker aggregates findings **per automation**,
asks Luna to identify focused repair topics, then asks Sol for one focused patch
per topic. **A new draft PR is published only after a baseline/candidate comparison
demonstrates the targeted improvement on frozen development cases.** Unsupported,
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
4. One validation stage runs original and proposed code on the same saved inputs.
   A Sol validation agent inspects task outcomes and can read full execution logs
   and pinned repository files. Code checks actual deliverables and receipts.
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

### On-demand evidence access

Sol can call `search_runs`, `read_signal`, `list_spans`, and `read_trace` while
preparing a patch. Searches use the monitored window; raw reads verify that the
trace belongs to the same automation. Full fields are paged rather than silently
clipped. The controller exposes no arbitrary SQL, shell, write endpoint or model
credential. Every tool request/result is retained privately. The default
`max_evidence_reads` is eight; normal cost/time/input bounds apply across turns.
Frozen standalone replays without a reader remain snapshot-only.

The controller's model transport is shared by stateless Luna calls and the
interactive Sol investigation. Tool execution is separate from proposal parsing
and publication. Responses continuation items are preserved as required by the
[official function-calling protocol](https://developers.openai.com/api/docs/guides/function-calling).

This uses the repository's supported git-synced `automation.yaml` + `tarball/`
layout. Plugin-backed automations are a second supported packaging mechanism;
copying the controller into another package would add a second source to maintain.
The private file ledger and lock deliberately target the current single-VM cron
runtime. A multi-host deployment would need the platform KV dispatcher pattern.
