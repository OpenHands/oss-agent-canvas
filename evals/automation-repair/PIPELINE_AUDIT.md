# Independent pipeline audit — October 29, 2026

The main remaining bottleneck is repair generation and experiment selection, not a
shortage of Laminar findings. Data quality and evaluator calibration also matter.
The corrected #60 comparison shows no demonstrated benefit even under a less
conservative interpretation. Passing harness tests does not establish repair quality.

This read-only audit was performed by a separate agent against the consolidated
implementation and retained executions. It ran no new inference. Three small
feedback/reporting fixes were implemented afterward; broader recommendations below
are not implemented or presented as successful experiments.

## Data: a historical incident is not a reproducible current failure

For #60, the historical policy and replay baseline policy are exactly identical:
4,303 bytes after excluding the credential/repository footer. Both use the same
nominal model. The historical failure therefore cannot be attributed to a newly
fixed priority instruction. Repository/API state, SDK/tools, setup, routing and
stochastic execution still differ or are not established as identical.

The corrected N=1 canary recorded eight attempts. Both baseline negative runs chose
medium priority; the candidate completed one equivalent result and hit its step
limit in the other. Both control pairs passed. There is no observed priority gain.
The original incident can still be real, while this proposed repair remains unproven.
The older marker-corrupted N=5 experiment is diagnostic only.

Replay uses original task payloads with pinned present-day repositories and captured
GitHub reads. It is not exact historical environment reconstruction. Native
historical-task execution currently supports issue triage; reviewer fixtures are
explicit mechanism experiments. More random traces alone will not repair these gaps.

Evidence: [retained runs](VALIDATION_END_TO_END.md),
`automation-simple-validation-20261029/{full-review-artifacts,canary-artifacts}.json`.

## Repair: the automatic proposer cannot express the strongest reference fix

The [edit converter](../../automations/weekly-automation-improvement/tarball/patches.py)
allows at most four existing-file replacements and a 20 KB patch, with no new files.
Sol has Laminar read tools, but no coding workspace or test runner while proposing.

The current #54 reference repair is 63,191 patch bytes across eight files, including
six new files. That exact change exceeds this automatic envelope. A smaller
equivalent guard might be possible; this has not been tested. The restriction
plausibly favors easy prompt additions, but that causal bias remains a hypothesis.
Its positive publication-safety evidence is not proof that the current automatic
proposer generated a successful structural repair.

For #60, the policy already contained the desired priority rule. Appending another
instruction targets unreliable rule following rather than missing task information.
#59 also has no demonstrated improvement on its factual-error target. The newer
prompt discourages redundant instructions, but no fresh proposal under that updated
instruction has yet been demonstrated. Retesting an older patch with a newer
validator does not validate the new proposer.

## Evaluation and feedback: real defects, plus an unresolved acceptance policy

The audit identified three defects now fixed in the existing implementation:

- Semantic failures with structurally valid outputs were absent from repair
  feedback. One unrelated unfinished trial suppressed all useful feedback. Valid
  failed pairs now reach the existing single revision, with unresolved pairs kept
  explicit; this adds no revision loop or extra grading stage.
- The revision got an inaccessible host path but not the failed patch. It now gets
  the bounded previous patch alongside the critique and original frozen source.
- Reproduction depended on both executions completing, although it is a baseline
  observation. It is now reported independently. Rescoring the same canary with
  the same Sol judgments changes target reproduction from unknown to **not
  reproduced**, while qualification remains **inconclusive**. No SDK/model rerun
  was used for this correction.

One handoff gap remains: the repair/revision agent cannot retrieve the frozen replay
artifacts through its current Laminar-only tools. It receives critique, checks and
the previous patch, but cannot inspect replay tool events/final state using the
host artifact path. This audit does not claim that adding the previous patch supplies
all necessary context. A future coding agent needs access to those existing artifacts;
this does not require another agent or another grading stage.

The latest fixed-candidate canaries did not exercise the weekly revision path.
Its missing feedback is confirmed by code and integration tests, not a demonstrated
cause of those specific live outcomes.

The [gate](evaluation.py) still requires every candidate to be mechanically
conformant, including controls with inherited baseline failures, and complete
paired coverage. That is stricter than fixing one topic without degradation.
Mandatory safety checks should remain distinct from unrelated task perfection.
This policy needs calibration; it was not relaxed during the audit. It does not
explain #60's clean control results or create missing evidence of priority benefit.

#54 has useful positive mechanism evidence, especially the two clean wrong-head
pairs. That supports a narrow safety claim, not general reviewer-quality improvement.
The report must retain that distinction instead of reducing everything to a single
inconclusive label.

## Next experiment: separate the causes on one reproduced task

Use one frozen publication-safety cohort with a demonstrated unsafe baseline and
successful-review controls. Predeclare repetitions and compare four candidates:

| Candidate | Expected result |
|---|---|
| Known-good guard | Prevent unsafe publication and retain useful reviews |
| No-op | Receive no improvement credit |
| Always skip reviews | Fail successful-review controls |
| Fresh Sol repair | Investigate and implement using a scoped worktree and tests |

Keep Sol's evaluator blind to candidate identity; objective publication-state
checks are the primary endpoint. Give the repair agent ordinary coding/test tools
and permission to create files within the approved automation, while retaining
source-scope, credential and publication checks.

If the known-good guard is rejected, investigate replay/evaluation. If no-op or
always-skip qualifies, the evaluator is miscalibrated. If the sentinels discriminate
correctly but Sol cannot produce a useful repair, focus on repair generation.
This is one calibration experiment, not another permanent pipeline of judges.

The proposed context handoff is a structured issue per actionable repair topic:
problem/impact, evidence with source/run links, repair hypothesis and unknowns,
validation plan, and attempts/results. The same Sol coding agent can investigate,
implement and record its work there. Referenced raw artifacts must be readable through
its actual tools; an issue link or host path alone does not provide access. This is
a recommendation, not an implemented issue-orchestration subsystem.

The post-audit implementation passes 255 tests, including semantic-feedback revision
with an unrelated unfinished control and baseline-only reproduction reporting.
The stronger proposer and four-candidate experiment remain next work. Production
schedules, deployments and the production ledger were not changed.
