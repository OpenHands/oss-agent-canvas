# Weekly Automation Improvement

A separate, disabled-by-default automation for OSS-14275. It reads existing
self-hosted Laminar traces, samples conversations uniformly, investigates them,
and asks a stronger coordinator for one improvement proposal. It can evaluate
one patch and optionally publish a draft PR. It does not change the existing
health monitor or ingest traces.

This is a stdlib-only prototype, reusing the HTTP/model, redaction, evidence
mapping and replay patterns developed in #17 and the earlier Laminar replay.
There are no agent repair loops, automatic inference retries, merges or deployments.

## Configure

Supply these environment variables; keep credentials outside this repository:

```bash
export LMNR_BASE_URL=https://laminar-api.app.all-hands.dev
export LMNR_PROJECT_ID=YOUR_PROJECT_UUID
export LMNR_APP_URL=https://laminar.app.all-hands.dev
export LLM_BASE_URL=https://llm-proxy.eval.all-hands.dev
# Set LMNR_PROJECT_API_KEY and LITELLM_API_KEY privately.
```

`LLM_API_KEY` is an alternative to `LITELLM_API_KEY`. The project key controls
which SQL data is accessible; the project ID records provenance and builds links,
and must match that key. Use a SQL-capable key, not an ingest-only key.

For agent-server execution, missing keys can be read from the owner's configured
`LMNR_PROJECT_API_KEY` and `LITELLM_API_KEY` secrets using the injected session key.
Non-secret configuration still needs to be supplied to the execution environment
or through entrypoint arguments. Completion callbacks follow the existing custom
automation pattern. The YAML registers a disabled Monday schedule; merge does
not demonstrate that a deployed run has these settings or credentials.

## Load inputs before analysis

From the repository root:

```bash
python3 automations/weekly-automation-improvement/tarball/main.py \
  --collect-only --seed 42 --output /private/improvement/input-run

python3 automations/weekly-automation-improvement/tarball/main.py \
  --input /private/improvement/input-run/input.json \
  --output /private/improvement/review-run
```

The second command needs only LLM credentials: no Laminar calls, host files,
database mounts, ingestion, or changing live inputs. Reuse that file to iterate
on prompts/models. Without `--input`, collection completes before inference in
the same run. Output directories must be new; artifacts are owner-only.

Sampling is uniform without replacement across unique session/conversation IDs
with `automation.conversation` spans starting in a seven-day window. There is no
status, cost or automation-family weighting. The default cutoff is ten minutes
ago; this buffer does not prove completion. Override `--since`, `--until`,
`--limit` and `--seed` explicitly. A generated seed, population hash/count,
inclusion probability and selected IDs are saved. The frame covers instrumented
conversations visible to the key, not all automation runs. Duplicate spans do
not increase a conversation's sampling weight. Collection is sequential, not an
atomic snapshot of a running conversation; replay freezes what was collected.

Collection loads each selected conversation's available history, original SDK
task-message inputs and existing Signal events. There are no payload reads during
LLM replay. Accumulated LLM inputs, reasoning/provider metadata and empty/container
wrappers are omitted from model context. Tool/default text has explicit excerpt
limits and original lengths, with distinct SQL aliases to avoid hiding clipping.
The coordinator gets task instructions, a compact whole-session timeline, cited
spans, adjacent counterevidence and final visible steps. Reference integrity does not establish claim correctness.

Oversized/unreadable selections remain collection errors and are never replaced
with easier examples. Failures and rejected citations produce a partial report
and a failed exit; no findings does not mean healthy. Raw responses and measured
usage remain available. Redaction is best effort; inspect private artifacts before
sharing them. Use the saved source files and snapshot hash to reproduce a run.

## Models

The working proxy defaults are `--model openai/gpt-6-luna` for trajectory
analysis and `--coordinator openai/gpt-6.1-sol` for investigating candidates and
writing a source-grounded patch. Sol is called only when Luna finds candidates.
Responses API uses low effort by default. `--api chat` supports compatible
providers and records provider-default effort. Model IDs are configurable;
the proxy returned `model_not_found` for `openai/gpt-6.1-luna` on October 19.
The [official model catalog](https://developers.openai.com/api/docs/models)
lists GPT-6 Luna and GPT-6.1 Sol; it has no Luna 6.1 entry.

Flow-1 execution is deferred to [#50](https://github.com/OpenHands/oss-agent-canvas/issues/50).
It requires a compatible shared Laminar deployment, a Cloud-backed profile and a
supported authenticated invocation API. This automation reads existing Signal
events but does not invoke flow-1 or depend on that upgrade.

## One patch and configured validation

Ordinary runs generate diagnostic proposals with no source patch. To permit a
source-grounded patch, copy `source.example.json` outside git and configure the
approved repository, its existing checkout, a full baseline SHA, authorized
automation IDs, permitted files and validation argv arrays. Freeze source upfront:

```bash
python3 automations/weekly-automation-improvement/tarball/main.py \
  --collect-only --source-config /private/source.json \
  --output /private/improvement/source-input

python3 automations/weekly-automation-improvement/tarball/main.py \
  --input /private/improvement/source-input/snapshot-with-source.json \
  --source-config /private/source.json --evaluate-fix \
  --output /private/improvement/fix-review
```

This performs one patch attempt in isolated baseline/candidate git worktrees.
Checks come from trusted configuration, never from model instructions. Include
a regression expected to fail on the baseline and healthy-case checks expected
to pass. Every candidate check must pass; timeouts, changed source and files
outside the allowlist are rejected. Tests receive a small environment without
inference/publication credentials. Keep worktrees/logs for inspection. This is
not an isolation boundary for arbitrary hostile repositories or tests.

Add `--publish-fix` only for an approved target repository and configured checks.
This checks the current remote base, matches an existing PR by stable branch,
then commits/pushes the accepted patch and opens a **draft** PR. The body includes
trace links, baseline revision, hypothesis, evaluation plan, actual check exit
codes and recovery criteria. GitHub CLI authentication is required. A repeated
signature reuses the existing PR; model-generated signatures are not guaranteed
to deduplicate differently worded incidents. Interrupted publication is not yet
automatically resumable.

The configured revision is not proof of what executed in production. Check
results are evidence about those configured checks, not a general task-quality
evaluation. Human review controls merge/deployment; revision uptake and observed
production benefit remain follow-up work.

## Bounds and verification

Defaults: three conversations (maximum five), one analysis call each and at most
one coordinator call, sequential execution, 4,096 output tokens/call, 750 KB model
request limit, 1,000 spans/conversation, 2 MB projected sample and 8 MB HTTP
response limits. One patch attempt, up to four configured checks per revision,
120 seconds/check. These are workload limits, not a dollar spending cap.

`requests/`, `responses/` and `measurements/` retain redacted provider requests,
responses, effective settings, token usage and proxy-reported cost. Missing cost
stays unknown. `report.json` links findings to actual span/run/conversation IDs
and records both configured models, errors and validation status.

```bash
python3 -m pytest automations/weekly-automation-improvement/tests -q
```

See [VALIDATION.md](VALIDATION.md) for real-data replay and synthetic end-to-end
results. No production rollout or real automation fix is claimed by these tests.
