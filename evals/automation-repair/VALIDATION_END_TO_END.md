# Evidence-gated publication

On 2026-10-29, the prototype produced
[repair PR #60](https://github.com/OpenHands/oss-agent-canvas/pull/60) with
**Before / After Evidence already in its initial description**. Its label is
`automation-improvement`; it remains a draft for human review.
The implementation is [#55](https://github.com/OpenHands/oss-agent-canvas/pull/55),
stacked on [#49](https://github.com/OpenHands/oss-agent-canvas/pull/49).

## Executed chain

1. Read a persisted Flow-1 finding and native issue-triage input from self-hosted
   Laminar. The pilot explicitly selected the historical finding behind #53 and
   used a private ledger; it was not a random sample or population-quality study.
2. Luna selected the priority-underrating topic. Sol generated one focused edit:
   audit reported user harm immediately before writing priority, keeping scope
   and readiness separate. The generated patch was not edited manually.
3. Run the actual baseline/candidate prompt through SDK/tools 1.58.2 against frozen
   GitHub state. Retain prompts, model/tool events, API requests and outcome checks.
4. Rescore all six completed executions under the corrected acceptance rule,
   reuse the exact tested candidate commit, and let the normal publication gate
   push/create the labeled draft. No new agent inference was used in recovery.
5. Verify the published head, source diff and evidence receipt agree. Close #53
   as superseded, retaining its separate comparison and development history.

Baseline: `5dba224799d97e3ac8e16568288601fcecd4f6ea`.
Candidate and published PR head: `de27818424adbbafbe20d525a2b31c2d6396c855`.
The patch changes only `plugins/issue-triage/scripts/worker.py` and adds ten lines.
This is the configured baseline; the historical executed revision was not verified.

## Observed behavior

Both versions used `openai/glm-5.3-flash`, high effort, 30 steps, 4096 output
tokens, a 120-second request timeout and a 420-second trial budget. There was one
execution per version/case, with no retries inside a cohort.

| Frozen scenario | Baseline | Candidate |
|---|---|---|
| Original reported paid harm | Medium priority; passed | Medium priority; passed |
| Reported harm, unknown monetary amount | Low priority; failed | Medium priority; passed |
| Speculative risk without reported harm | Low priority, but incorrectly ready for development | Low priority; readiness and preservation checks passed |

Candidate: **3/3 strict passes**, no execution errors. Baseline: **1/3 strict
passes**, including the specific priority violation being repaired. Human text
and unrelated labels were preserved by the candidate. One candidate optional
research read and two baseline optional research reads were unsupported; the
priority contract observes the complete required issue and verifies final labels.
It does not evaluate full repository research or general triage quality.

Baseline median agent-loop time: **68.40s**; candidate: **29.77s**. SDK-estimated
comparison costs were **$0.0279 baseline / $0.0192 candidate**, not billed costs.
Luna/Sol generation separately reported **$0.063651475** through proxy headers.
These timings/costs describe this small development comparison, not population
performance. Container setup and publication are outside agent-loop timings.

## Acceptance correction and retained attempts

The earlier rule required every baseline control to pass. That rejects a repair
when the old version has an additional genuine behavioral failure, even if the
candidate passes the control and fixes the specific reproduced target error.
The corrected rule requires complete, error-free executions on both versions,
a paired target-specific baseline violation, every candidate check passing and
passing candidate controls. Baseline behavioral failures stay visible. Missing
required inputs, provider/runtime failures and unmodeled mutations still block
acceptance. Unrelated formatting improvements cannot prove a factual-claims fix.

This rule was revised during development. The original verdict was inconclusive;
PR #60 explicitly discloses that rescoring changed it. No frozen inputs, candidate
patch, model settings or raw executions were changed for recovery. This is
post-development evidence, not independent confirmation or statistical proof.
Some cases use reconstructed draft checkpoints derived from observed failures;
they are not exact full-history production replays or a held-out benchmark.

Earlier cohorts remain retained: the initial 60-second request timeout failed,
the 180-second trial budget also timed out, and another cohort exposed fixture
handling of trailing-slash reads and invalid named-label POST requests. The final
execution cohort fixed those responses and strengthened checks against transient
human-text replacement. None of those earlier results was silently rerolled.

Private VM artifacts, under the agent's state directory:

- Original Flow-1 → Luna → Sol run:
  `automation-evidence-publisher-20261029/cbd351ad62a0`.
- Revised request timeout: `.../protocol120-80680c059ba6`.
- Revised trial budget: `.../protocol420-b744998bae51`.
- Completed corrected-fixture executions: `.../fixture420-86c92dad9c8f`.
- Rescoring and exact-commit publication: `.../recovered-193e10263f91`.
- Legacy draft backfills: `automation-evidence-backfill-20261029/86c92dad9c8f`.

The public PR contains full source revisions and manifest/comparison hashes.
Private receipts also bind the frozen request and scoring implementations.
Recovery validates the retained commit's parent and complete diff before reuse.

## Other repairs and checks

[#53](https://github.com/OpenHands/oss-agent-canvas/pull/53) received its own
positive six-execution comparison, then was closed as superseded by #60.
[#59](https://github.com/OpenHands/oss-agent-canvas/pull/59) received twelve
executions and remains explicitly **inconclusive**: baseline factual-claim errors
were not reproduced, so its formatting-only advantage cannot establish the target
fix. The earlier incomplete/fixture-limited evidence on
[#54](https://github.com/OpenHands/oss-agent-canvas/pull/54) remains retained.

The full worker/comparison suite passes: **198 tests in 87.66 seconds**. Ruff and
whitespace checks pass. Integration tests exercise real Git/subprocess operations,
publication gating, immutable receipt reuse, mismatched-commit rejection, retained
candidate bundles, cleanup and a bounded Sol revision. Failed or inconclusive
repairs retain their findings and release publication reservations.

The supported contracts cover issue-priority decisions, explicit PR-description
claims and reviewer publication. Other mechanisms remain pending. No general
cross-automation quality evaluator is claimed.

The pilot verified that the production weekly ledger hash was unchanged. Source
worktrees were removed and candidate bundles retained. No real GitHub review,
production automation, scheduled worker, deployment or Laminar Signal definition
was replaced by this work. Merge/deployment remain separate reviewed actions.
