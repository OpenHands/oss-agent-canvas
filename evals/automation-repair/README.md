# Behavioral comparison prototype

Run the actual reviewer prompt code at two pinned revisions with real SDK/LLM
execution, a fresh conversation and a fresh frozen GitHub world for each run.
The prototype evaluates publication behavior; it does not score substantive review
quality or reproduce entire historical production environments.

The reviewer adapter invokes `PullRequestReviewer._prompt`, including its acceptance
reporting instructions. Its workspace adapter supplies an already checked-out tiny
Python repository instead of fetching a live PR. Model, effort, SDK version,
container image ID, source SHAs, case definitions, prompt hashes and harness hashes
are recorded before inference. Baseline/candidate order alternates between repeats.

Build once, before running any agent:

```bash
docker build -t automation-repair-replay:1.58.2 \
  evals/automation-repair
```

Use Linux or macOS and Python with `openhands-sdk==1.58.2` and `openhands-tools==1.58.2`. Set
`LITELLM_API_KEY` privately; the runner never copies it into an agent container.

```bash
python3 evals/automation-repair/compare.py \
  --checkout /private/existing-repository \
  --baseline BASE_COMMIT_SHA --candidate PR_COMMIT_SHA \
  --model openai/glm-5.3-flash --effort high \
  --base-url https://llm-proxy.app.all-hands.dev \
  --output /private/new-comparison --repeats 3
```

The agent loop defaults to a 180-second wall-clock budget and 30 steps; terminal
soft timeouts are capped at 30 seconds. Setup is outside the agent-loop budget.
Timeouts are recorded as inconclusive without retrying. `--resume` with the same
arguments reuses every saved trial, including failed/inconclusive trials, and
spends inference only on missing trials. Overlapping executions are rejected.
An interrupted trial without a result must be retained explicitly as inconclusive
before resuming; it is never silently rerolled.

The frozen scenarios are AI-authored development cases derived from reviewer
findings: an initially moved head, head movement after the first state read,
a body-file write failure, and an already accepted empty review. A normal review
and a known multiplication regression in the tiny Python
repository are the completion and substantive-finding controls. The optional
`inline_rejected` case exercises rejected inline positions. These are not independent held-out examples; do not modify cases
in response to candidate results and then describe the same run as validation.
The body-file fault targets `/tmp/review_body.md`; choosing another path can avoid
that fault, so a passing case alone does not establish recovery from this failure.

The cases are derived from the two traces cited in
[repair PR #54](https://github.com/OpenHands/oss-agent-canvas/pull/54),
`14229882-d059-4426-4486-3bebc9bfcf33` and
`589b1cd1-3d61-2497-2522-5a6358d66ece`. The fixture repository and its expected
outcomes are synthetic; historical external state was not reconstructed.

The stateful API accepts malformed and duplicate reviews so assertions can detect
an agent failure; it does not implement the candidate's publication preflight for
it. Successful review cases require a valid review and the required final marker.
Stop cases require current-state observation, no write attempt and no new
completion marker. Explanation semantics are not judged by this prototype. Provider/tool failures and failure to reproduce the baseline
remain inconclusive. Improvement requires a reproduced regression to improve,
all candidate cases to pass and no observed regression in controls. Small counts
are development evidence, not a statistically established failure-rate reduction.

Agent terminal, file-editor and task-tracker operations execute inside containers
with networking disabled, no host mounts and no real GitHub credentials. The SDK
and model client run outside; candidate prompt code also runs inside the container.
Tool calls cross a narrow bridge using the native
SDK action/observation schemas and persistent terminal executor. The browser tool
is omitted; these scenarios use GitHub REST/CLI and a local checkout. This is an
execution test environment, not an adversarial sandbox or reward-hacking eval.

`manifest.json`, per-run prompts, SDK events, API requests/results, measurements
and `comparison.json` are private artifacts. The runner removes its containers and
source worktrees after execution. It never triggers or changes production
schedules, merges a repair, or posts GitHub reviews. The weekly publisher remains
explicitly unevaluated; this prototype supplies evidence for human review rather
than automatically accepting its own generated repairs.

Generate a public summary containing aggregate checks and measurements, without
private model responses or error text:

```bash
python3 evals/automation-repair/summarize.py /private/new-comparison \
  > /private/review-report.md
```

The execution/comparison runner and reviewer scorer are separate modules. A future
adapter can run issue triage in the same way and an optional scorer can incorporate
the existing PR-review content metrics. Without a suitable scorer, execution alone
must remain unvalidated. The existing configured baseline/candidate check gate in
[the monitoring PR](https://github.com/OpenHands/oss-agent-canvas/pull/49) remains
available; this CLI has not been wired into unattended weekly publication.

For a source revision containing `controlled_review.py`, the adapter exercises that
revision's actual gateway and publisher against the same frozen world. Its trusted
precheck runs first; a moved head or existing accepted review can stop before any
inference. Otherwise the agent gets only the gateway credential, submits a proposal,
and the publisher runs only after the conversation finishes. Both the raw agent
response and the controller's publication outcome are retained. The production
profile resolver and native container launcher require a separate VM integration
check; the replay adapter does not exercise those services.

The Git fixture uses the real read-only `git http-backend` for fetches. Git upload-pack
POSTs are reads. Unsupported reads remain inconclusive; forbidden mutations are
violations even if the fixture does not implement their response. If a scorer bug is
corrected, retain the original execution and rescore into a new directory without
inference:

```bash
python3 evals/automation-repair/rescore.py /private/original /private/rescored
```

The rescored manifest records the scorer hash, original evidence hashes and zero
agent reexecutions; `changes.json` retains every before/after score. This command
corrects classification only and does not add missing API responses or reinterpret
provider/runtime failures as passes. Reports must identify both scoring versions.

Recorded results: [initial prompt-only comparison](VALIDATION.md) and
[controlled publication with native VM checks](VALIDATION_CONTROLLED_PUBLICATION.md).
