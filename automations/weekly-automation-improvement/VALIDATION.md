# Local validation — October 19, 2026

Private inputs, credentials, requests, responses, patches and logs remain outside
git in an owner-only experiment directory. These checks demonstrate execution and
specific controls; they do not establish production detector accuracy or an
improvement to a deployed automation.

## Upstream collection and frozen replay

The existing SQL-capable project key authenticated successfully to the self-hosted
Laminar SQL API. A seven-day frame with a ten-minute arrival buffer contained 940
unique instrumented conversations. Seed 42 selected three without replacement:

| Selected automation | Available spans | Usable original task messages | Analysis result |
|---|---:|---:|---|
| GitHub PR Reviewer, multi-repo | 484 | 2 | No findings |
| Issue Triage, multi-repo | 99 | 1 | No findings |
| GitHub PR Reviewer, reviewer-request events | 207 | 1 | No findings |

Collection made ten read-only SQL requests totaling approximately 3.54 MB of
responses. No traces or Signals were created or ingested. The saved input retains
the sampling window/seed, population hash/count, selected IDs, clipping metadata,
full native span references, and existing Signal coverage. No Signal events were
returned for these selected traces.

Early collection revealed that this deployment has empty LLM input spans. The
collector now reads original `conversation.send_message` inputs, and projects
recognized assistant output messages to omit duplicated reasoning/provider data.
Original lengths use distinct SQL aliases so excerpting cannot hide clipping.
The initial 500 KB sample budget was too small for the selected long review;
the final explicit caps are 2 MB projected sample and 750 KB model request. The
same selections were retained across these collector iterations.

The replay subprocess had Laminar credentials and host configuration removed.
It loaded all input from the saved file before inference and completed all three
`openai/gpt-6-luna` low-effort analysis calls in 11.626 seconds of wall time,
with no errors or rejected references. The coordinator was not called because
there were no candidates.

| Call | Input tokens | Output tokens | Request time | Proxy-reported cost |
|---|---:|---:|---:|---:|
| Review, multi-repo | 177,565 | 17 | 4.645 s | $0.02220085 |
| Triage | 41,044 | 17 | 3.861 s | $0.005135725 |
| Review, reviewer-request | 56,114 | 17 | 2.935 s | $0.007019475 |

Total proxy-reported cost: $0.03435605, not a verified invoice. No findings is an
analyzer result, not an independently established healthy label. This random
sample contains two automation families and is too small to estimate quality.

## Synthetic investigation → patch → checks

Three frozen synthetic cases covered a false success report after a failed test,
an acknowledged failure followed by a passing rerun, and a healthy passing run.
The supplied source was a tiny local test repository whose reporting function
always returned success. It was frozen at a full commit SHA before inference.
The target repository was fictional and publication was disabled.

An initial control run produced explanations inside evidence-reference strings;
the validator rejected them. The evidence schema/prompt now require bare IDs.
An ambiguous exact-output instruction in the recovery control was also corrected
before the final run. These are development controls, not a held-out evaluation.

In the final run:

- Luna identified the false success report with references to the failing tool
  output and contradictory assistant message. The recovery and healthy cases
  produced no findings.
- `openai/gpt-6.1-sol` at low effort received the cited evidence, task,
  counterevidence and frozen source, then proposed one conditional return
  instead of unconditional success.
- The configured regression failed on the baseline and passed on the candidate.
  A configured healthy-case check passed on both revisions.
- Baseline and candidate ran in separate git worktrees. The original checkout
  was unchanged. No branch was pushed and no improvement PR was published.

The three analysis calls plus coordinator took 14.781 seconds of inference wall
time; check time was not included in that measurement. Proxy-reported inference cost was $0.0050954. The rejected initial control
attempt separately cost $0.0003143; it is not included in the final run's cost.
The real-data replay, both control attempts and their costs are separate results.
After tightening patch-index validation, the final pipeline re-executed the saved
model outputs and passed the same baseline/candidate checks without new inference.

After removing flow-1 execution, a fresh inference run repeated the three
controls with Luna 6 and Sol 6.1. It found only the false success report and
produced one patch. The regression failed on baseline and passed on the candidate;
the healthy check passed on both. The run completed without errors or rejected
citations in 14.452 seconds including checks, at $0.0044774 proxy-reported cost.
Laminar credentials were absent, requests/responses and usage were retained,
and no PR was published. These remain synthetic development controls.

## Tests and remaining integration

Sixteen targeted tests cover random conversation sampling, retained failed
selections, SQL excerpt lengths, behavior-preserving message projection,
trace-scoped/ambiguous/virtual-span citations, counterevidence, frozen replay,
partial failures, unchanged baseline source, configured validation, source/file
scope, new-file patch rejection and publication gating. Ruff and Python
compilation pass.

The upstream UI still reports EE 0.1.637. An empty-body probe of the prospective
Signal test route returned HTTP 404 before inference. The experimental flow-1
execution adapter was removed from this PR; [#50](https://github.com/OpenHands/oss-agent-canvas/issues/50)
tracks the shared upgrade, profile and supported authenticated invocation API.
The Luna/Sol pipeline does not depend on that work. The proxy lists Luna 6 and
Sol 6.1; an explicit `openai/gpt-6.1-luna` request returned `model_not_found`.

Before production enablement: configure the approved private source and task
checks, verify executed revision attribution, and review representative findings.
Durable resumability, stronger cross-run deduplication and production source
uptake remain follow-ups.


## Flow-1 persisted-Signals adapter — October 22

- 33 targeted tests cover both existing Luna behavior and native Signal replay,
  completed no-event cases, direct Sol handoff, project/trace/span/version scope,
  missing/failed/pending runs, latest-failed-run handling, missing events,
  post-analysis trace changes, lifecycle-only content, shared-trace ownership and zero model usage.
- A synthetic persisted-Signals fixture with one false success claim, one recovered
  run and one healthy run exercised the real Sol proxy and existing patch checks.
  Only the false success case supplied a synthetic finding. One Sol call proposed
  one patch: regression failed before/passed after, healthy check passed on both.
  Final-code replay: wall time 12.062s; Sol latency 10.928s;
  873 input / 323 output tokens; proxy-reported $0.004976. No improvement PR was published. This tests the adapter
  and patch path, **not Flow-1 detector accuracy**.
- The production dedicated Cloud-backed Signal was created with a reserved trigger
  for manual backfills only. A one-trace authenticated UI backfill persisted a
  COMPLETED/no-event record with zero model tokens. The trace contained lifecycle
  metadata, so this is job-persistence evidence, not proof of model inference.
- A public-API collection froze this native Signal run and its conversation.
  Offline replay rejected insufficient execution content and did not call a model
  or publish a PR. Production SQL column names/status values were exercised.
- Production historical spans are readable, but `traces` currently exposes only
  October 22 entries. A historical trace visible in `spans` is missing from both
  SQL `traces` and UI backfill selection. Frontend startup reported
  `traces_replacing has no readable rows; nothing to migrate`. The image-shipped
  backfill uses `min(start_time)` and treats epoch/unparseable values as absent;
  this is a possible early-exit cause, requiring direct database verification.
- Project-wide daily counts show LLM/TOOL spans through October 19 and only DEFAULT
  spans on October 20–22. Recent random examples have missing task/execution
  content. This predates the October 22 production upgrade; cause is unconfirmed.
  Neither issue is papered over by resampling or treating no-event as healthy.

Private inputs, native run receipts and model artifacts are outside git at
`~/experiments/laminar-flow1-integration-20261022`. No production database/cache
mutation, broad historical backfill, live detector trigger or schedule enablement
was performed. Deployment blockers remain in issue #50.
