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
Luna-low analysis calls in 11.626 seconds of wall time, with no errors or rejected
references. The coordinator was not called because there were no candidates.

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
- Sol-low received the cited evidence, task, counterevidence and frozen source,
  then proposed one conditional return instead of unconditional success.
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

## Tests and remaining integration

Nineteen targeted tests cover random conversation sampling, retained failed
selections, SQL excerpt lengths, behavior-preserving message projection,
trace-scoped/ambiguous/virtual-span citations, counterevidence, frozen replay,
partial failures, unchanged baseline source, configured validation, source/file
scope, new-file patch rejection, publication gating, and Signal trace/origin
scope. Ruff and Python compilation pass.

The upstream UI still reports EE 0.1.637. An empty-body probe of the prospective
Signal test route returned HTTP 404 before inference. The flow-1 adapter has unit
coverage but has not executed on upstream. Shared upgrade, profile and authorized
route verification remain prerequisites; the validated analyzer here is Luna,
not flow-1. The separate local Laminar tests in #17 do not remove that dependency.

Before production enablement: verify the deployed Signal API/profile if selecting
flow-1, configure the approved private source and task checks, verify executed
revision attribution, and review representative findings. Durable resumability,
stronger cross-run deduplication and production source uptake remain follow-ups.
