# Weekly Automation Improvement

A standalone prototype for OSS-14275. Flow-1 analyzes eligible automation traces
in self-hosted Laminar. Each week, the worker aggregates findings **per automation**,
asks Luna to prioritize them, then asks Sol for at most **one unevaluated draft
repair PR per automation**. It can decline every candidate. Behavioral evaluation,
automatic merges and deployments are deferred.

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
5. Make one `openai/gpt-6-luna` call per automation to rank up to three problem
   groups and select at most three representative conversations. It can combine
   obvious duplicate groups. Summaries and severity labels are unverified leads;
   cleanup does not erase an explicit instruction violation. Unselected findings
   remain pending, and an empty selection requires an explanation.
6. Load detailed evidence only for those representatives, freeze it with that
   automation's approved source and aggregate context, then make one
   `openai/gpt-6.1-sol` call. Sol checks the original task, recovery, counterevidence,
   existing PRs and current code. It produces at most one coherent source patch,
   accounts for rejected findings and may propose no repair.
7. Apply the patch in a detached worktree, restrict edits to approved regular files,
   and check whitespace and changed Python syntax. Live publication creates one
   explicitly unevaluated draft PR per automation per ISO week. Dry runs perform
   the mechanical checks without pushing or opening PRs.

This is one weekly process, two bounded model calls per investigated automation,
and the existing JSON state file. There is no custom embedding service, new
clustering infrastructure, repair loop or generalized evaluation framework.

## Configure and run

Copy [weekly.example.json](weekly.example.json) outside git. Configure the project,
Signal, endpoints, existing canonical checkout, approved repository/base branch,
private state directory and GitHub secret name. `triage_model` and `repair_model`
are configurable. Keep the credentials file owner-only:

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

`--limit 1..3` bounds representative conversations **per automation**, not the
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
stores assessed event IDs, and `automation_weeks` stores completed/reserved
investigations. Each automation gets at most one completed investigation per ISO
week. A failure in one automation is reported and does not stop the others.

Before publication, reserve that automation's weekly slot atomically. An uncertain
publication blocks another attempt for that automation that week. Stable branch
names derive from automation ID and week; PR bodies carry automation/week/event
markers so GitHub can recover publication state if the local ledger is lost.
Existing remote branches are not overwritten. Existing PR descriptions are given
to both models to reduce semantic duplicates; this is not a complete semantic
deduplicator. Old global completion records do not block the new per-automation
pipeline; an unresolved legacy publication reservation remains conservative.

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
inventory. Each automation has `triage/triage-input.json`, saved model requests,
responses and measurements, its frozen `snapshot-with-source.json`, source config,
analysis and mechanical-validation artifacts, and `automation-report.json`.
`weekly-report.json` reports all automation outcomes. Artifacts/state are owner-only;
redaction is best effort. Never share them without inspection.

Replay a frozen Sol input without Laminar collection or publication:

```bash
python3 automations/weekly-automation-improvement/tarball/main.py \
  --input /private/run/AUTOMATION_UUID/snapshot-with-source.json \
  --source-config /private/run/AUTOMATION_UUID/source-config.json \
  --check-draft --output /private/replay
```

The compactor omits repeated LLM histories, reasoning/provider metadata and empty
wrappers. Original task instructions, cited/adjacent spans, compact whole-session
chronology and final visible steps remain, with explicit clipping. The earlier
Luna trajectory-analysis mode and optional configured behavioral-check path remain
available through `main.py`; the weekly path reuses persisted Flow-1 findings.

Bounds: 10,000 Signal run rows per seven-day collection window; one Luna call and
at most one Sol call per automation/week; three representative conversations;
30 source files / 250 KB; 750 KB per model request; 4,096 output tokens. Oversized
requests fail visibly with pending data retained. Each conversation retains the
existing 1,000-span, 2 MB projected-sample and 8 MB HTTP-response limits. There is
no silent replacement sampling or inference retry. More automation groups mean
more model calls; the former one-PR-global limit has been replaced deliberately.

```bash
python3 -m pytest automations/weekly-automation-improvement/tests -q
python3 -m ruff check automations/weekly-automation-improvement
```

See [VALIDATION.md](VALIDATION.md) and rollout tracker
[#50](https://github.com/OpenHands/oss-agent-canvas/issues/50). Detector accuracy,
behavioral benefit, executed revision uptake and post-merge benefit remain unproven.
