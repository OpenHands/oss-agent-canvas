# Weekly Automation Improvement

A standalone automation for OSS-14275: Laminar continuously analyzes a sample of
automation traces with Flow-1; a weekly worker reads those Signals and asks
`openai/gpt-6.1-sol` to investigate up to three findings and propose at most one
source patch. It opens an **unevaluated draft PR** for human review. Behavioral
evaluations, automatic merges and deployment are deferred.

## Weekly path

1. Upstream, the dedicated Signal runs Flow-1 on 50% of matching traces when
   `conversation.arun` finishes, filtered to traces containing
   `automation.conversation`. The creation span itself finishes too early.
2. Monday at 08:00 UTC, `weekly.py` reads the previous seven days of the current
   Signal version, with a ten-minute arrival buffer. It uniformly samples up to
   three unhandled finding events from each trace's latest completed run.
   Failed/pending runs and completed no-event runs are counted separately.
   Percentage sampling upstream is not an exact-size uniform conversation sample.
3. Before inference, it freezes Signal versions, run/event IDs, usage, ownership,
   original task messages, compact trajectory evidence and approved source.
   Other conversation traces provide context without needing their own Signal.
   Ambiguous ownership, stale analysis, missing citations and unavailable data
   remain visible errors; the worker does not replace difficult selections.
4. Sol checks task requirements, recovery, counterevidence, current source and
   existing improvement PRs. It can reject every finding. External outages or
   missing credentials do not justify workflow patches.
5. One supported proposal may become a draft. The publisher checks the remote
   base, applies exact replacements in a detached worktree, restricts changes to
   approved regular files, and checks whitespace and changed Python syntax.
   It does not run behavioral evaluations or claim the fix improves quality.

The collector reads the public self-hosted Laminar SQL API. It neither ingests
trajectories nor invokes Flow-1 itself. Flow-1's Cloud key remains in Laminar's
LLM profile; the worker uses a SQL-capable self-hosted project key and Sol proxy
key. Existing health monitoring remains separate.

## Configure and run

Copy [weekly.example.json](weekly.example.json) outside git. Configure the project,
Signal, endpoints, existing canonical checkout, approved repository/base branch,
private state directory and GitHub secret name. Keep the credentials file owner-only:

```json
{"LMNR_PROJECT_API_KEY":"...","LITELLM_API_KEY":"..."}
```

`weekly.py` defaults to `~/.config/automation-improvement/config.json`.
On the OSS VM, the configured existing GitHub token is read through the native
Agent Server session; outside it, set `GH_TOKEN` privately. Git credentials use
an ephemeral helper. No keys belong in YAML, source, logs or global agent profiles.

```bash
python3 automations/weekly-automation-improvement/tarball/weekly.py \
  --config /private/weekly.json --dry-run --seed 42

python3 automations/weekly-automation-improvement/tarball/weekly.py \
  --config /private/weekly.json
```

Dry runs collect and analyze but neither publish nor modify the handled-event
ledger. They can still spend inference tokens. The YAML enables the Monday
schedule and runs `python3 weekly.py`; the VM owner configuration must already
exist. Native completion callbacks follow the existing automation convention.

Current plugin automations resolve to their declared GitHub repository, branch
and `plugins/<name>` directory. Legacy bundles require a unique byte match for
all deployed Python/text files under `automations/<name>/tarball`. Foreign repos,
branches, ambiguous matches and excessive source are skipped with visible errors.
This MVP supports one approved target repository. It does not prove the historic
executed revision equals the current source; the model context and PR disclose
that limitation. Each case includes its own resolved source scope.

The durable ledger and process lock prevent overlapping batches and repeated
weeks. Event markers in PR bodies recover published findings if local state is
lost. The stable branch is derived from Signal/event identity, and existing PRs
are supplied to Sol to help reject semantically duplicate work. This is not a
complete semantic deduplicator. Publication reserves the week's one-PR slot
before pushing: an interrupted/uncertain publication blocks further automatic
attempts that week and requires inspection. Existing remote branches are never
silently overwritten. A week with no findings completes without a model call.

## Laminar setup

[signal.example.json](signal.example.json) describes the six-field detector
payload, execution-finish trigger, automation filter and 50% sample. It is paused
as an installation template. Supply the workspace's Laminar Cloud Flow-1 profile
and activate it deliberately after checking access and sampling. The configured
production Signal is `fadaa9d7-a70a-42df-ab63-04c88f3b77ac` in project
`e41ee96e-851b-45ad-bfe8-b986861c331d`. The October 27 configuration is active,
version 6 after increasing `sampleRate` from 5 to 50. The original 5% automatic
path produced a completed, nonzero-usage analysis on a fresh native automation run. Rotate its Cloud credential before November 15.

`sampleRate` is the upstream percentage parameter (0–100). Change it explicitly
with the setup command; the weekly reader does not overwrite the Signal:

```bash
python3 automations/weekly-automation-improvement/tarball/configure_signal.py \
  --config /private/weekly.json --sample-rate 50
```

This patches only the percentage and reads back the Signal to verify its trigger,
filter, prompt, profile and enabled state remain unchanged. Reapplying the same
value avoids creating another version. It affects future sampled analyses and
performs no historical backfill. At the previously measured traffic and token
usage, 50% sampling is estimated at roughly $9–18/month in Flow-1 inference,
excluding subscriptions and downstream Sol. This is an estimate, not a cost cap.

Reports retain native run statuses and usage; Flow-1 billed cost, effective model
and latency are not exposed by this SQL surface. Token-rate estimates use the
dated [pricing snapshot](tarball/flow1-pricing.json), include all observed attempts
and exclude credits. They are not invoices. Failed/skipped runs with no usage
make cost unknown. Sol records proxy-reported cost and request latency separately.

## Frozen local iteration

The weekly run directory contains `collection/input.json`, a source configuration,
`snapshot-with-source.json`, analysis requests/responses/measurements and
`weekly-report.json`. All artifacts and state are owner-only. Replay the frozen
input to inspect prompts without collecting again:

```bash
python3 automations/weekly-automation-improvement/tarball/main.py \
  --input /private/run/snapshot-with-source.json \
  --source-config /private/run/source-config.json \
  --output /private/replay
```

Repeated LLM histories, reasoning/provider metadata and empty wrappers are
omitted. Original instructions, tool excerpts, cited spans, adjacent
counterevidence, compact whole-session chronology and final visible steps remain.
Clipping is explicit. Saved provider requests allow prompt inspection. Citation
integrity does not establish that a finding is correct; redaction is best effort.

`main.py` also retains the earlier Luna comparison mode and opt-in
`--evaluate-fix` / `--publish-fix` path with trusted fail-before/pass-after checks.
Those are local research options, not requirements of the weekly MVP.
`--publish-draft` explicitly selects unevaluated publication.

## Bounds and verification

Weekly limits: three finding events, one Sol call, one patch attempt and at most
one draft PR per ISO week; 30 source files / 250 KB, 750 KB model request and
4,096 output tokens. Sources exceeding the aggregate budget are reported and
skipped without replacement. Collection retains the existing 1,000-span,
2 MB projected sample and 8 MB response bounds. These are workload limits, not a
dollar cap. Partial analysis fails rather than publishing.

```bash
python3 -m pytest automations/weekly-automation-improvement/tests -q
python3 -m ruff check automations/weekly-automation-improvement
```

See [VALIDATION.md](VALIDATION.md) for measured development checks and
[#50](https://github.com/OpenHands/oss-agent-canvas/issues/50) for rollout history.
The prototype does not establish detector accuracy, behavioral improvement,
deployed revision uptake or post-merge benefit.
