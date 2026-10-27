# Weekly MVP validation — October 27, 2026

The production Signal is active at version 5, using the execution-finish trigger,
automation filter and 5% sampling. A fresh native canary produced an automatic
completed Flow-1 run with 8,762 input and 256 output tokens and no finding. No
manual backfill was used for that version.

The weekly entrypoint completed two native VM dry runs, including run
`3112c80d-a4ff-4628-b90c-1181f52a36d0`. The first live weekly run, `1f239e2e-e729-4e2b-a5f8-a91f0759b8e1`, also
completed successfully, wrote the current-week ledger and retained the enabled
Monday 08:00 UTC schedule. One completed no-event Signal run was present; no
eligible findings were returned, so Sol and publication were correctly skipped. Live source probes resolved both plugin
PR reviewers and issue triage to their declared source paths on the approved
repository's main branch. Historical executed source is not proven equal.

A frozen real-data replay completed in 19.899 seconds with two findings, one
proposal and zero errors. This development sample used the earlier detector
version and does not demonstrate current-version accuracy or improvement.

All 68 local tests pass; Ruff, compilation and Git whitespace checks pass.
The suite covers event sampling, superseded/handled findings, ambiguous
ownership, context traces without Signals, plugin source boundaries, exact legacy
bundle matching, unevaluated draft publication using real temporary Git
worktrees with simulated GitHub calls, concurrency, event-ledger recovery, dry-run
behavior and publication limits after uncertain failures. No synthetic improvement
PR was published. Behavioral evaluations and acceptance demonstration are deferred.

The following historical notes preserve the earlier prototype's experiments.

---

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

## October 19 integration status (historical)

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

## October 27: native production Flow-1 and source-grounded Sol replay

Historical indexes and Signal-event links were repaired October 26. Three
conversations were selected uniformly without replacement from 150 instrumented
conversations starting October 13, using seed 1027. All three had execution
content and exclusive trace ownership. Each exact trace was submitted once
through the authenticated Backfill UI to the dedicated Cloud-backed Signal,
version 2. Its reserved trigger and all other Signals stayed unchanged.

| Case | Native result | Input tokens (including cache) | Cached input | Output | Estimated gross USD |
| --- | --- | ---: | ---: | ---: | ---: |
| c1 | Completed, one finding | 73,952 | 40,384 | 4,612 | 0.003466 |
| c2 | Completed, no event | 245,394 | 189,440 | 8,582 | 0.007267 |
| c3 | Completed, no event | 103,070 | 62,784 | 3,681 | 0.003746 |

The estimate totals **$0.014479**, using the October 27
[published rates](https://laminar.sh/pricing): $0.05/M fresh input, $0.01/M cached
input and $0.30/M output. It is not a billing receipt and excludes account credits.
The self-hosted run surface does not expose effective model, inference duration
or billed cost. Those fields remain unknown rather than accepting its `$0.00`.

The worker then collected and froze all three conversations in the current
Signal-version frame, including both no-event runs. Public SQL collection and
credential-free Laminar replay passed ownership, version, coverage, nonzero-usage
and citation checks. The approved source was pinned to
`208ff807ccdcba0532648b84f2064a3c9a663e51`, with two reviewer source files and the
reviewer automation ID explicitly allowed. The executed source revision was not
established. This is a smoke test, not a detector-accuracy benchmark.

The final replay made one Sol 6.1 call in 6.601 seconds total, costing
**$0.0973445** as reported by the proxy. It rejected the candidate: interpreter
and collection failures did not establish that the review ultimately stopped,
the proposed fixture change was outside the review-only task and source allowlist,
and later recovery was unresolved in the supplied evidence. The reason is saved
in `rejected_findings`. No patch, improvement PR, merge or deployment was produced.
An earlier replay before rejection reporting cost $0.095637; these are two
iterations over the same saved inputs, not two independent detection experiments.

The Signal prompt was shortened to one issue, consistent with its scalar event
schema. Random sampling remains over all conversations with a current-version
Signal run; there is no finding-only filter or replacement of failed cases.

Fresh-data investigation confirmed that the OSS VM's host Agent Server 1.56.0
does not pass tracing configuration to Docker conversations. A live container
had no `LMNR_*` or `OTEL_*` settings despite a configured host. The fix is already
merged in [SDK #5557](https://github.com/OpenHands/software-agent-sdk/pull/5557)
and included in 1.58.x. A host-only isolated canary restored LLM/tool spans, but
its cached `main-python` image was also 1.56.0 and failed the credential-isolation
check. Both host and conversation image therefore need a matched, pinned upgrade;
the shared service has not been changed on this evidence alone.

A second isolated canary matched host and conversation runtime at **1.58.2**,
using the released image pinned to
`ghcr.io/openhands/agent-server@sha256:a297711073c7d9c00350d3e78b82d2240bcd0e07e9bf379981c39a3deb120cd6`.
The verify-agent-server harness passed health, readiness, REST authentication
and WebSocket authentication checks. A real Luna conversation executed
`python3 -c "import os; assert 'LMNR_PROJECT_API_KEY' not in os.environ; print(6*7)"`
with exit code 0 and output `42`. Its LLM and tool spans arrived in the production
Laminar project under the canary's session ID. Only the new canary service and
its containers were restarted. This proves the matched-version repair, not that
the shared OSS service has been upgraded or fresh automation attribution verified.

Private receipts, snapshots, prompts, responses and canary evidence are under
`~/experiments/laminar-self-improvement-20261027`. The weekly schedule and PR
publication remain disabled. Local regression suite: **37 tests passed**.

## October 27: shared restart and calibrated native execution

The shared OSS launcher was upgraded to Agent Server/SDK/tools/workspace 1.58.2
and the pinned 1.58.2 Docker image above. A separate canary first passed using
the exact unconstrained launcher dependency resolution. No automation jobs were
active at cutover. Before installing the version settings, the service was stopped,
the 1.3 GB SQLite database was backed up and passed `quick_check`, and mutable SDK
metadata/settings were archived. Root-only recovery files are retained at
`/var/backups/agent-canvas-flow1-20261027/restart-20261027T101802Z` on the VM.

Health, readiness, REST and WebSocket authentication checks passed. A native
manual-only automation, run `77221c10-7ec1-4377-9b41-715e2f6ec304`, completed and
sent its completion callback. Its conversation
`1b1018d8-8800-4b8e-a03d-b36d9ab5a538` has LLM/tool spans in trace
`d4ea01c9-6191-569d-22e8-89961d02df93`, correctly attributed to that automation
and run. The terminal assertion confirmed that the tracing credential was absent
from the tool environment. Its proxy-reported inference cost was $0.001820375.
Sampled catalog errors came from a pre-existing zero-byte metadata file, byte-identical
to the pre-upgrade backup; no historical metadata was deleted to make startup pass.

Two prompt revisions reused the same three historical conversations; none were
replaced based on findings. Version 3 produced two candidates: the unsupported
terminal-failure claim and recovered command-shape friction. Sol rejected both.
Version 4 excludes recovered friction and explicitly distinguishes reviewed-software
errors from automation defects. It produced:

- c1: a supported violation of the review's explicit no-file-modification rule,
  with a confirmed temporary repository test file. Later cleanup does not erase
  the original boundary violation; final task failure is not established.
- c2: no event.
- c3: a questionable locale comparison in a published review. Sol rejected it
  because the worker's comparison excerpt is clipped and no material effect on
  the review verdict is shown.

Version 4's three native runs cost an estimated **$0.0198341 gross**. A fresh
healthy automation control produced no event ($0.00027948 estimated); its worker
replay made no Sol call. A separate diagnostic control with an honestly reported
assertion failure also produced no event. That is an error-reporting control,
not a positive defect example. Native Signal receipts are reused during replay;
replaying them does not cause another Flow-1 charge. These are development cases,
not independent held-out accuracy measurements.

The real worker was uploaded and manually dispatched through the automation
service. Its first attempt exposed an inherited-key conflict: `LLM_API_KEY`
belonged to a different route and overrode the configured proxy key. It failed
closed with HTTP 401 and a FAILED callback. The corrected worker prefers
`LITELLM_API_KEY`; run `20546b69-a714-4f38-bdaa-013ab19ad7b6` then completed,
rejecting the old unsupported candidate. Credentials stay in an owner-only VM
configuration file loaded by a thin deployment launcher, outside the tarball and git;
shared global custom secrets were not changed.

The first version-4 replay proposed a relevant prompt change but emitted invalid
diff hunk markers. The coordinator contract now requests exact text replacements.
The worker requires each replacement to match once in an allowed frozen file and
generates the unified diff itself, without a repair/model retry loop.

Final native run `3102b5e9-f7ac-491b-b6ae-0dffa22f2db2` completed in **16.479s**
of analysis wall time. Its single Sol call used 52,833 input / 684 output tokens,
with **$0.138857** proxy-reported cost. It proposed one 1,385-byte patch to keep
review-test adaptations outside the repository, and explicitly rejected c3.
The generated patch applied to the pinned baseline, passed Python parsing and
`git diff --check`, and stayed within the source allowlist. Its behavioral benefit
has **not** been evaluated. An explicit publication attempt with the real proposal
and missing regression checks stopped at the expected validation gate before any
GitHub publication operation.

The disabled manual prototype is automation
`81aa02f5-da15-4b9c-9c07-9a903cc4f7fc` on the OSS VM. Its reusable entrypoint
reads directly from Laminar, collects inputs before analysis and creates a new
private output directory per run. A separate frozen replay retains the source
snapshot and proposed patch for iteration. The shared default model/profile was
restored after test setup; isolated canary processes and their SSH forward were stopped.
Run `26de8107-c7eb-4cab-b239-f257cf0990da` also completed the reusable live-collection
entrypoint: it read the current Signal-version frame directly from Laminar,
froze the one recent analyzed conversation, made no Sol call and sent a successful
completion callback. This is separate from the historical frozen-input replays.

The regression suite now has **52 passing tests**, including credential precedence,
exact-edit ambiguity/scope rejection, applicable diffs with and without a final
newline, and the full coordinator-edit → baseline-fail/candidate-pass path.
Publication requires both a failing regression and a passing control on the
baseline, with all checks passing on the candidate; either category missing is rejected.
Those synthetic checks validate mechanics, not the quality of the real prompt fix.
Live Signal triggering, the weekly schedule and improvement PR publication remain
off. Before enabling them, validate `conversation.arun` finish triggering on a
bounded sample, configure source/revision attribution and run the proposed
read-only-review behavioral regression plus a normal-review control.
