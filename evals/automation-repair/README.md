# Automation repair comparisons

The weekly publisher compares the actual automation source before opening a new
repair PR. It selects a trusted task contract, freezes its inputs and execution
settings, runs baseline/candidate code, and publishes **Before / After Evidence**
only when the target improvement is demonstrated. This PR is stacked on
[#49](https://github.com/OpenHands/oss-agent-canvas/pull/49).

## Contracts and evidence

The normal weekly path freezes a historical cohort **before Sol sees the repair**.
Top-level `validation_sample_size` defaults to **5**; users can set any positive
integer within their time/cost budgets. Select up to N distinct topic-linked
negative conversations and N uniformly random non-negative conversations from the
same automation and detector window. Non-negative means completed Flow-1 analysis
with no finding, not verified healthy. Unscored, failed and mixed-negative
conversations are excluded from controls. Sampling seeds, eligible-frame hashes,
selected identities and the complete evidence hash are retained. Missing selected
inputs or fewer than N eligible controls make the cohort incomplete; no rerolls.
Negative candidates are the topic representatives plus exact cause/signature
matches. A shared Laminar cluster alone cannot pull another repair topic into the cohort.

- **Historical issue priority:** execute `IssueTriage._prompt` on each original
  native task payload. Independently derive expected priority/readiness from the
  frozen baseline policy, task data and pinned root repository guidance using Luna;
  shuffle opaque input IDs and hide Signal classification, candidate code and
  candidate outcomes. The annotation prompt includes only the baseline `_prompt`
  policy rather than unrelated dispatcher source. Luna extracts an evidence
  category and a verbatim quote; trusted code maps categories to outcomes and
  rejects quotes absent from the input. Dependency mentions do not establish a
  linked PR's merge state. Quotes make annotations auditable, not infallible.
  Unscorable expectations block acceptance. Inspect labels,
  human-text/comment preservation and unrelated labels. Current root guidance is
  shared across versions; complete historical repositories and linked discussions
  are not reconstructed. This adapter supports priority topics only.
- **Legacy issue-priority fixtures:** original author body plus AI-authored
  unknown-amount and speculative-risk cases remain available for development.
- **Review faithfulness:** execute the reviewer against a complete PR description
  and a reconstructed working draft. Detect explicit false test-absence claims
  while requiring retention of a known independent code bug. A genuine-absence
  control prevents rewarding blanket suppression of findings. The current checks
  cover these explicit claims, not general semantic review quality.
- **Review publication:** exercise requested-head checks, duplicate/empty review
  handling and publication formatting, with normal/known-bug controls. For source
  containing `controlled_review.py`, run its actual gateway and trusted publisher.
  The historical body-file fault targets `/tmp/review_body.md`; a different path
  can avoid that particular fault, so it alone cannot establish general recovery.

Contracts are selected by source family and failure mechanism, without a manual
per-automation/topic mapping. Unsupported mechanisms stay pending. A suitable
execution adapter and outcome checker are necessary for each additional family.
Historical execution adapters for the reviewer contracts are not yet implemented.
Their AI-authored fixtures remain available for explicit experiments using
`legacy_development_fixtures: true`; they cannot satisfy the weekly N-input gate.
Some fixtures start from reconstructed draft checkpoints. Neither mode reproduces
the entire production environment or establishes a statistical failure-rate reduction.

Improvement requires a baseline execution with the **specific target violation**,
a paired passing candidate, complete execution and passing negative candidate cases.
Historical controls must introduce **no new violations** relative to paired
baseline executions; inherited failures remain visible. Legacy fixtures retain
their stricter all-candidate-checks-pass rule. An unrelated formatting failure does not prove a
factual-accuracy fix. Runtime/provider failures, absent required inputs and
unreproduced failures remain inconclusive. The narrow priority contract requires
observation of the complete current issue and checks the verified final labels;
unsupported optional research reads are retained as warnings. Unsupported writes
remain failures. Reviewer contracts require complete read coverage.
Completed baseline behavioral failures remain visible; they do not invalidate an
otherwise sound execution. New control violations, missing inputs and execution
errors on either version still block acceptance.

Reports compare expected behavior and observed outcomes side by side, including
labels, violations, retained code findings, timing, tokens and estimated cost.
Exact source SHAs, SDK/image/model settings and evidence hashes identify what was
executed. Full prompts, events, API records and manifests stay private. A successful
comparison supports human review; it never merges or deploys the repair.

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
    "repeats": 1,
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
comparison succeeds. Inconclusive results are visible and never promoted to passes.

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
