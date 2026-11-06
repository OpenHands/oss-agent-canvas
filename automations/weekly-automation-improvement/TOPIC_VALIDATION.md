# Topic-level repair prototype — 2026-10-28

One automation can produce multiple focused repair PRs. A topic is one concrete failure mechanism and desired behavior change that can be fixed and reviewed independently. Luna proposes topics in per-call batches; Sol investigates each separately. There is no total topic or PR-count quota. Topic assignment is a model judgment, not a proven root-cause label.

## Deterministic checks

120 tests pass across the weekly pipeline, triage, native Flow-1 adapter and publisher. Ruff, Python compilation and whitespace checks pass. New coverage includes:

- Seven topics in one automation are processed across multiple batches, with separate Sol calls, publication results and durable reservations. A failed/uncertain publication blocks only its topic.
- Real local Git worktrees and the publisher produce distinct topic branch names, while GitHub writes are intercepted.
- Duplicate groups can merge; a mixed group can split using distinct events. Separate topics may use different events from one conversation. Unknown/reused event refs and invalid topic keys fail validation.
- Each Sol request contains one topic and its selected findings. Typed `other_topic` rejections stay pending instead of being marked handled.
- Topic markers recover individual weekly slots. Old completed automation-wide slots do not block new topics, while uncertain legacy reservations remain conservative. Open topic PRs block duplicates across weeks.
- Generated and reused repair PRs receive `automation-improvement`, including with an arbitrary future coordinator model. The real label is attached to open repair PRs #53 and #54.

## Saved production inventory

The native input was a saved lightweight collection of **12 Signal events across three automations**. It contained no Laminar leaf memberships, so the existing signature/event fallback supplied grouping hints.

An initial dry run exposed one copied UUID error in Luna's output. Selection now uses short enumerated refs (`e1`, `e2`, …); trusted code resolves UUIDs and group membership from an owner-only map. The subsequent **three real Luna calls** selected five topics distributed **1 / 2 / 2**, with valid references throughout. Their proxy-reported inference cost totaled approximately **$0.00163**. This was triage-only, with the inventory's existing collection warnings retained.

The earlier full live-hydration attempt is also retained. Its selected reviewer trace had changed after Flow-1 analysis, so the existing freshness guard blocked repair. Two legacy reviewer topics lacked an approved source mapping. Those are coverage/source limitations, not successful repair demonstrations; no stale evidence was forced through the pipeline.

## Two-topic functional control

A separate AI-authored native-format fixture supplied two independent defects in one automation: publishing against a changed head and double-counting cached input costs. This is a functional control, not production-quality evidence.

One real Luna call selected two topics. Two real `gpt-6.1-sol` calls each produced a mechanically valid patch. AST comparison confirmed that one changed only `review_target`, while the other changed only `estimated_cost`. Both passed source scope, patch application, whitespace and Python syntax checks. Exact coordinator requests were checked for a single topic and topic-owned event IDs. Proxy-reported inference cost totaled approximately **$0.01505**; individual Sol requests took 6.37 and 6.68 seconds, excluding setup and other work.

The controls blocked all external GitHub operations. Temporary candidate worktrees were removed. Both successful checks verified an unchanged hash of the production ledger and absence of an experimental handled-event ledger. No generated repair PR was opened during these checks.

Private requests, responses, mappings, snapshots and measurements remain on the OSS VM under `automation-topics-20261028/c46f4ecd02c9` and `automation-topic-controls-20261028/c46f4ecd02c9`, beneath `~/.local/state/`. The earlier failed attempt remains under content version `45d2f0729977`. Subsequent schema construction cleanup preserves the tested three-topic request shape; the current code is covered by the tests above.

The scheduled worker has not been replaced; the new code was also run in a publisher-enabled one-off pilot described below. Existing publication/review safeguards remain in force. Semantic topic duplication, large-backlog quality, actual fix benefit and post-merge acceptance still require evaluation. Earlier real-publication evidence for the previous per-automation version remains in [VALIDATION.md](VALIDATION.md).


## Live publication and resource-budget follow-up

The artificial three-topics-per-automation/week quota was removed. `triage_batch_size`
(default three) now controls only one Luna response. Processing continues across
batches until no further actionable topics are selected, representatives are
exhausted, or a configurable cost/time threshold is reached. New tests cover
more than three publications, deferred-backlog retention, wall-clock checks,
unknown-cost handling, and cost accounting in the actual HTTP model wrapper.

A one-off run used live production Laminar findings, current approved `main` source
and the real GitHub publisher, with a copy of the production handled-event ledger.
It opened [draft #59](https://github.com/OpenHands/oss-agent-canvas/pull/59), a
single-topic, seven-line prompt change grounding PR-description claims in fetched
evidence. Readback confirmed the `automation-improvement` label, draft status,
head commit `2780c8dd8493965c6dce130e19d28e79e99f782a` and the one-file change.
The generated commit matches the worker's mechanically validated candidate.

The first pass used a $0.25 threshold and stopped after three inference requests
reported $0.40419. Thresholds are checked between requests, so one call can
overshoot; this is explicitly not a hard provider billing cap. A continuation
used the same pilot backlog with the remaining allowance under a $1 pilot
threshold. It made nine requests costing $0.33601. Total reported cost was
**$0.74021 across twelve requests**. These are proxy-reported costs, not an invoice.

The continuation opened no additional PRs. Sol declined provider-outage leads and
an already-addressed publication issue. Two topics were blocked because their
traces changed after Flow-1 analysis; two legacy topics lacked a unique approved
source match. Five Signal events remained pending, with no count quota suppressing
them. Neither source nor freshness checks were bypassed. The continuation reports
partial failure for the blocked stale analyses rather than claiming full coverage.

Production ledger hashes were unchanged in both passes. Generated candidate
worktrees were removed. Private evidence remains below
`~/.local/state/automation-live-pilot-20261028/`, in code versions `6ba158c09eb6`
and `continue-f80697b6a0b0`, with collection snapshots, exact model requests,
responses, measurements, per-topic results, validation records and a pilot ledger.
The scheduled worker and Laminar Signal configuration were not changed.
