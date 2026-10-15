# Native health-review prototype

The existing `tarball/main.py` supports native-data review alongside its original
SDK execution mode. The scheduled entrypoint and production prompt are unchanged.
The prototype derives its four review lenses from that same prompt; it does not
require a Laminar export or a conversion to a different trajectory format.

Python 3.12 or later is sufficient for the native mode. It uses only the standard
library and does not import the SDK or start an agent server.

## Native inputs

- SQLite `automations` and `automation_runs` tables.
- `dev_conversations/<uuid-without-dashes>/base_state.json`.
- The conversation's `events/event-*.json` records.
- Saved health-review state, either its raw KV GET response or its state object.

Host mode defaults to the paths already documented by the production prompt.
Each path can be overridden. Replay accepts `automations.db` or the exported
`replay.sqlite`, the native conversation directories, and
`monitor_state_api.json`. A bundle manifest is optional; when present, its
selection cutoff and coverage limitations are retained.
When available, `monitor_prompt.txt` supplies the captured review policy;
`--policy-file` overrides it. Otherwise the bundled prompt is used. The report
records its path and hash.

```bash
python automations/weekly-automation-conversation-health-review/tarball/main.py \
  --replay /private/replay-data --output /private/review-collection
```

The same readers can inspect host inputs directly:

```bash
python automations/weekly-automation-conversation-health-review/tarball/main.py \
  --review-host \
  --db /private/copied/automations.db \
  --conversations /private/copied/dev_conversations \
  --state-file /private/copied/review-state.json \
  --until 2026-10-15T00:00:00Z --output /private/host-review
```

Without `--state-file`, host mode GETs `--state-url` using
`OPENHANDS_AUTOMATION_API_KEY`. Only a verified HTTP 404 initializes an empty
state. An unreadable or malformed state is an error, not a reset.

## Analysis

Set `LLM_API_KEY` and `LLM_BASE_URL` in the environment. `LITELLM_API_KEY` and
`BASE_URL` are accepted aliases. Add `--analyze` to run Luna at low reasoning
effort. `--model`, `--api responses|chat`, and `--effort` are explicit options.
Use Chat Completions for compatible non-OpenAI routes; effort for that API is
the provider's default and is recorded as such.

```bash
python automations/weekly-automation-conversation-health-review/tarball/main.py \
  --replay /private/replay-data --output /private/review-analysis \
  --analyze --model openai/gpt-6-luna --workers 2
```

The algorithm has no agent loop:

1. Aggregate the database window in a read-only transaction.
2. Pair native actions and observations across each available conversation.
   Keep an ordered compact timeline, task instructions, usage and skill facts.
   Expand failed/missing results, reported exit markers, adjacent retry context,
   repeated operations, and the last steps within a fixed detail budget.
3. Analyze each selected conversation independently, with at most two concurrent
   requests. Give the model short case/evidence references and resolve them back
   to native IDs and supplied excerpts in code.
4. Optionally run one verification request with `--coordinator
   openai/gpt-6.1-sol`. It receives the candidates and the relevant timelines.
   Cited episodes and nearby follow-up steps receive focused output-line windows;
   the complete compact timeline remains visible. Abbreviated task context is
   explicitly marked and cannot establish a policy violation or final outcome.
   This costs additional time/tokens; an existing evidence reference alone does
   not establish that a claim is correct.

Candidates must distinguish observed errors, explicit policy violations, and
measured regressions. Error claims need a cited failed result/error marker;
policy claims need task policy plus an observed action. Regression claims are
rejected because this pilot does not load a comparable historical baseline.
This deliberately limits the pilot's recall; it is not a general anomaly detector.

Select particular examples with repeatable `--conversation-id` arguments.
The default limit is three conversations; at most five are permitted. The default
model input budget is 200,000 bytes per request, with 1,800 output tokens and no
automatic retries. Large verification batches can need an explicitly larger
`--max-input-bytes`. These are workload limits, not a dollar spending cap.

## Inspecting prompts and results

Every actual LLM request is recorded under `requests/` before transmission.
The recorded JSON is the exact redacted request body; authorization headers are
never recorded. Responses and per-call usage, timing, and proxy-reported cost
are saved under `responses/` and `measurements/`. Missing cost remains unknown.
Parallel wall time and the sum of request times are separate measurements.

`inputs.json` preserves all deterministic window rollups and selected native
context. `evidence.json` maps model references to supplied records, native event
IDs, and available event filenames. `prompt_audit.json` records payload sizes.
The model does not receive generic agent system prompts, complete tool schemas,
full skill catalogs, accumulated stats events, or agent reasoning transcripts.
Only the relevant automation rollups accompany each independent case.

`review.json` and `report.txt` retain Security, Resources, Errors, and Skills.
Invalid references are rejected and recorded separately. The report's original
IDs come from code rather than model-generated copies. Output directories must
be new; files are owner-only and should remain outside git.

## Limits of this prototype

The original production invocation still uses the existing SDK path; these modes
must be explicitly selected. Deploying host reads into a sandbox still requires
the appropriate read-only data mount or host-side collection. This change does
not make host paths automatically visible inside an isolated container.

The local modes never send Slack messages or update production KV/SQL. Sampled
coverage does not advance `last_reviewed_through`. Unavailable host/security
checks and historical baselines are reported as unavailable. A completed run row,
running conversation state, missing result, or clipped excerpt is not itself an
outcome label. Cumulative token usage is not verified billing, and available
skill content is not proof that it was injected.

Redaction is best effort. Review private artifacts before sharing. The pilot
does not establish detector precision/recall, a production model winner, or an
evaluated automation improvement.

Run the targeted tests with:

```bash
python -m pytest automations/weekly-automation-conversation-health-review/tests -q
```
