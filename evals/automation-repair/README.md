# Automation repair comparisons

The weekly publisher compares the actual automation source before opening a new
repair PR. It selects a trusted task contract, freezes its inputs and execution
settings, runs baseline/candidate code, and publishes **Before / After Evidence**
only when the target improvement is demonstrated. The full pipeline is reviewed in
[#49](https://github.com/OpenHands/oss-agent-canvas/pull/49).

## One validation stage

Freeze the original tasks and a pinned repository snapshot, run the baseline and
candidate on identical inputs, then ask **one Sol validation agent** to inspect
the recorded before/after evidence. There is no precomputed gold-label pipeline.
Sol judges the original task, the complete deliverable and the specific repair
hypothesis. It can inspect full tool events and repository files when needed.

Code enforces the parts that should not depend on a model's assertion:

- Exact input/source identities and retained execution artifacts.
- Actual body/comment/label deliverables, preservation of human text and final
  readback. Rejected write attempts are distinct from successful writes.
- No improvement claim from identical rendered prompts, missing executions or
  contradictory success/regression judgments. Prompt repairs need at least two
  valid paired observations; these remain development evidence, not statistical proof.

Top-level `validation_sample_size` defaults to **5**. Freeze up to N topic-linked
negative conversations and N uniformly sampled non-negative conversations from the
same automation before the repair is proposed. Non-negative means completed
Flow-1 analysis with no finding, not verified healthy. No selected input is
replaced after failure. N counts distinct inputs; `repeats` separately controls
executions and defaults to two in the example configuration.

The historical adapter currently runs issue-triage tasks. Its repository snapshot
contains real implementation files and directory listings, not a synthetic example.
Related GitHub reads are captured upfront. Both versions see the same pinned
present-day environment with the original task data; this is not a reconstruction
of all historical environment state. Unknown API reads are explicit coverage gaps.
Sol determines whether missing exploratory data was supplied by other captured
evidence; required missing inputs and runtime errors cannot establish improvement.

Reports separate target reproduction, paired fixes, observed regressions, actual
deliverable failures, control coverage and experiment validity. A clean publication
safety test remains useful when unrelated trials are invalid; it does not establish
general review quality. Reviewer publication/factual-claim fixtures remain explicit
mechanism experiments, not replacements for a historical-input adapter.

Repairs without demonstrated benefit remain drafts or pending. Human review
controls merge and deployment. Full prompts, model/tool events and private receipts
identify what ran. The validation agent's judgments are auditable, not infallible.

## Install and configure

Use an existing checkout, Docker, and Python with `openhands-sdk==1.58.2` and
`openhands-tools==1.58.2`. Build the pinned execution image:

```bash
docker build -t automation-repair-replay:1.58.2 evals/automation-repair
```

Configure the [weekly worker](../../automations/weekly-automation-improvement/weekly.example.json)
outside git. The runner path must reference the installed evaluator, outside
candidate worktrees. For example:

```json
{
  "validation_sample_size": 5,
  "require_improvement": true,
  "validate_existing_repairs": true,
  "comparative_validation": {
    "command": ["/private/sdk-venv/bin/python", "/private/evaluator/pr_comparison.py"],
    "automatic_contracts": true,
    "model": "openai/glm-5.3-flash",
    "base_url": "https://llm-proxy.eval.all-hands.dev",
    "effort": "high",
    "repeats": 2,
    "llm_timeout": 120,
    "max_run_seconds": 420,
    "timeout_seconds": 7200
  }
}
```

Alternatively, `profile_id` resolves the native Agent Server's model, endpoint,
effort and model credential. Model credentials are passed only to the trusted
SDK host process. The analyzing container receives no real GitHub, Laminar or
model credential. Tools and candidate prompt code run with networking disabled,
no host mounts and bounded CPU/memory/processes. Oracle files and the private RPC
credential are removed before agent execution. This is an execution environment,
not an adversarial sandbox or reward-hacking benchmark.

The SDK defaults to 30 agent steps, 120 seconds per model request and 180 seconds
per agent trial; terminal soft timeouts are capped at 30 seconds. Setup is outside
the trial budget. Comparison bounds are separate from the weekly Luna/Sol proxy
cost threshold; SDK costs are estimates, not billed amounts. Requests are not
retried. Runner interruption requests cleanup before terminating a stuck process.

## Run and inspect

The normal end-to-end entrypoint reads persisted Flow-1 findings, uses Luna/Sol,
validates proposed repairs and publishes qualified drafts:

```bash
python3 automations/weekly-automation-improvement/tarball/weekly.py \
  --config /private/weekly.json
```

Validate or refresh existing labeled repair PRs, using their owned Laminar traces
to capture native inputs:

```bash
python3 automations/weekly-automation-improvement/tarball/validate_repairs.py \
  --config /private/weekly.json --output /private/repair-validation
```

`--pr NUMBER` narrows that command for an experiment. It paginates open PRs;
there is no PR-count quota. Cached receipts are keyed by head, settings and harness
fingerprint. The cohort is cached separately by PR/head/N, so a harness correction
does not select a new favorable dataset. A changed head or closed PR rejects the description update. Existing
text is preserved, except superseded pipeline-owned unevaluated notices when a
comparison completes. Coverage gaps, observed regressions and supported mechanisms remain visible separately.

To inspect a pinned pair directly:

```bash
python3 evals/automation-repair/compare.py \
  --checkout /private/existing-repository \
  --baseline BASE_COMMIT_SHA --candidate CANDIDATE_COMMIT_SHA \
  --model openai/glm-5.3-flash --effort high \
  --base-url https://llm-proxy.eval.all-hands.dev \
  --cases reported_paid_harm,reported_harm_unknown_amount,speculative_cost_risk \
  --output /private/new-comparison --repeats 3
python3 evals/automation-repair/summarize.py /private/new-comparison
```

`--case-file` accepts frozen input overrides, never altered acceptance checks.
Baseline/candidate execution order alternates across repeats. `--resume` preserves
saved failures/inconclusive trials and spends inference only on missing trials;
changed request timeouts are rejected. An interrupted trial requires an explicit
inconclusive record before resuming, never a silent reroll. Source worktrees and
containers are removed; the publisher retains private candidate Git bundles.

Corrections to a scorer write a separate report without inference:

```bash
python3 evals/automation-repair/rescore.py /private/original /private/rescored
```

The receipt records original evidence hashes, both scoring implementations and
zero agent reexecutions. It cannot invent missing API responses or turn provider
failures into passes. The trusted runner can also adopt `recorded_runs` after
checking pinned source/model/endpoint/timeouts, cases and input values, then rescore
all executions. This supports publication recovery without repeated inference.
For recovery, a trusted source configuration can set `prepared_candidate_revision`
to the retained candidate commit and `recorded_runs` to its completed comparison.
The publisher checks its parent and complete diff against the frozen proposal,
then reuses that exact commit; a changed patch or base rejects recovery.

Recorded earlier results remain retained:
[evidence-gated publication and live verification status](VALIDATION_END_TO_END.md),
[initial prompt comparison](VALIDATION.md),
[controlled publication and native integration](VALIDATION_CONTROLLED_PUBLICATION.md),
[post-publication evidence update](VALIDATION_POST_PUBLICATION.md).
