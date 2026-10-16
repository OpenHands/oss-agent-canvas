# Native health-review prototype

The existing `tarball/main.py` supports native-data review alongside its original
SDK execution mode. The scheduled entrypoint and production prompt are unchanged.
The prototype derives its four review lenses from that same prompt; it does not
require a pre-existing Laminar export. Flow-1 analysis imports selected native
events into local Laminar; collection and the alternate LLM analyzer work directly
from the same native inputs.

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

## Random sampling

The default window is the seven days before `--until` (now, or the replay
manifest's cutoff). `--since` overrides it. The saved production review cutoff
does not extend this window: sampled reviews never advance that state.

Sample uniformly without replacement from unique conversations associated with
runs created in this window and with available native state files. Each available
conversation has the same chance; duplicate run records, status, cost and
automation family do not change its weight. Sampling is across the whole
available population, not separately per automation. Missing exports can limit
that population; the report records this coverage limitation.

The default sample is three conversations; `--conversation-limit` accepts 1–20.
`--seed 42` reproduces selection for an unchanged population. Without it, a fresh
seed is generated and recorded. The manifest includes the population size/hash,
sample size, inclusion probability and selected IDs. Selected read errors abort
collection; analysis errors and oversized traces are reported without replacing
them with another sample. A metadata row-limit overflow also aborts rather than
sampling a truncated window.

Repeatable `--conversation-id` arguments explicitly select development examples
and are labeled `explicit_ids`, not a random sample.

## Flow-1 analysis

`--analyze` uses the local Laminar Signals/flow-1 path by default. Set up the
isolated instance with [experiments/flow1](experiments/flow1/README.md), then run:

```bash
python automations/weekly-automation-conversation-health-review/tarball/main.py \
  --replay /private/replay-data --output /private/random-review \
  --seed 42 --conversation-limit 3 --analyze \
  --flow1-config "$HOME/experiments/laminar-flow1-local/local-project.json"
```

The private configuration contains the local project/profile IDs and ingest-only
key; the Cloud inference key stays in Laminar's configured profile. Re-run
`configure.py` for an older configuration that lacks `profileId`. Internal test
routes are restricted to loopback HTTP origins. On a VM, run Laminar there or
forward its ports to loopback; override `--flow1-api-url` and
`--flow1-frontend-url` for different local ports.

1. Read the database window and randomly select unique available conversations.
2. Pair actions/results and import their full redacted native payloads, assistant
   messages, original instructions and error events as Laminar spans. A manifest
   maps them to native IDs and lists omitted event kinds, such as accumulated
   statistics. A snapshot hash rejects data changed after collection.
3. Run one Signal per selected conversation, with at most two concurrent calls.
   Laminar prepares flow-1's trace-reading context. The Signal asks for at most
   one actionable finding, checks recovery, and requires supported citations.
4. Resolve evidence labels and unambiguous Laminar span citations to native
   events. Reject unsupported or malformed candidates. Optionally verify the
   candidates with one general-purpose model request using `--coordinator`.

There is no outer agent loop or inference retry. Laminar's bounded trace reader
uses at most four steps and 4,096 output tokens by default. Set
`--flow1-max-steps`, `--flow1-output-tokens` and `--max-trace-bytes` explicitly to
change these limits. The imported trace budget defaults to 2 MB; exceeding it is
an analysis error, not silent clipping. These are workload limits, not a dollar
spending cap.

Error claims need a cited failed result/error marker; policy claims need task
policy plus an observed action or assistant message. Regression claims are
rejected because this pilot has no comparable historical baseline. Valid
references establish provenance, not the truth of every model judgment.

## Alternate LLM analysis and verification

`--analyzer llm` retains the compact native-context analyzer. Set `LLM_API_KEY`
and `LLM_BASE_URL`; `LITELLM_API_KEY` and `BASE_URL` are accepted aliases.
The default model is Luna at low reasoning effort. `--model`,
`--api responses|chat`, and `--effort` are explicit options. Chat Completions uses
the provider's default effort and records that fact.

```bash
python automations/weekly-automation-conversation-health-review/tarball/main.py \
  --replay /private/replay-data --output /private/llm-review \
  --seed 42 --analyze --analyzer llm --model openai/gpt-6-luna
```

This path keeps an ordered compact timeline and expands failed/missing results,
retry context, repeated operations and final steps within a detail budget. Only
the final two assistant messages accompany ordinary analysis; verification also
includes any earlier messages cited by a candidate, without duplicating finals.
`--coordinator openai/gpt-6.1-sol` works with either analyzer and requires the same
LLM environment settings. Verification receives candidates, the compact timeline,
and focused cited episodes with adjacent follow-up output. Abbreviated task
context cannot establish policy violations or final outcomes. The default general
LLM request budget is 200,000 bytes and 1,800 output tokens; large verification
batches can need a larger `--max-input-bytes`.

## Inspecting prompts and results

For flow-1, `requests/` captures the Signal definition; `laminar/` captures the
imported redacted spans and native reference manifest. Laminar constructs the
actual provider prompts internally: this adapter does not capture those bodies.
For the alternate analyzer and coordinator, `requests/` contains the exact
redacted provider request body before transmission. Authorization headers are
never recorded. Responses, usage, timing and reported cost are saved under
`responses/` and `measurements/`. Flow-1 additionally calculates usage cost from
published fresh/cache/output rates, before credits; this is not a verified invoice.
Missing usage/cost stays unknown.
Parallel wall time and the sum of request times are separate measurements.

`inputs.json` preserves all deterministic window rollups and selected native
context. `evidence.json` maps model references to supplied records, native event
IDs, and available event filenames. `prompt_audit.json` records payload sizes.
The alternate analyzer receives only the relevant automation rollups for each
case. Both paths omit the native skill catalog, tool schemas and accumulated
stats events. Native instructions supplied as MessageEvents remain available to
flow-1; Laminar adds its own trace-reader prompt.

`review.json` and `report.txt` retain Security, Resources, Errors, and Skills.
Invalid candidates are recorded separately. Incomplete or failed Signal calls
produce `partial_analysis`, with errors and any returned usage retained; zero
findings is not proof that every case was analyzed successfully. The report's
original IDs come from code rather than model-generated copies. Output directories must
be new; files are owner-only and should remain outside git.

## Limits of this prototype

The original production invocation still uses the existing SDK path; these modes
must be explicitly selected. Deploying host reads into a sandbox still requires
the appropriate read-only data mount or host-side collection. This change does
not make host paths automatically visible inside an isolated container.

The local modes never send Slack messages, open fix PRs or update production
KV/SQL. Sampled coverage does not advance `last_reviewed_through`. Unavailable host/security
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
