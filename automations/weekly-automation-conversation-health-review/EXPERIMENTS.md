# Native-input development replay

October 15, 2026. These are development experiments on five purposively selected
conversations, not a held-out evaluation or a model ranking. No improvement to
an automation has been evaluated or deployed.

## Data shift

The earlier Laminar experiment selected 120 spans from three conversations and
omitted accumulated LLM inputs. The host export instead contains 1,237 native
events from five conversations, 31 automation definitions, 1,383 run records,
and saved review state. Native events expose action/result links, initial tasks,
subprocess exit codes, conversation lifecycle state, usage and skill metadata.
The datasets are different, so their finding counts and latency are not a fair
before/after quality comparison.

Generic agent system prompts, tool schemas and skill catalogs occupy substantial
space but are not repeated into analysis prompts. Three sampled conversations
each report 68 available skills containing about 486,632 content characters;
available content is not proof that the entire catalog entered their prompts.
Stats-update events, agent thoughts and duplicate wrappers are also excluded.

## Iterations and findings

| Development step | Observation | Change |
|---|---|---|
| Native verbose collection | Three conversations produced about 546 KB of serialized context | Pair actions/results; retain a compact full timeline and targeted detail |
| First compact pass | About 149 KB of context; several quoted citations failed validation | Preserve more task policy, link each case to its actual automation, reject invalid candidates explicitly |
| Batched model analysis | The model mixed a usage number between conversations and mistyped long IDs | Analyze cases independently, use short case IDs, resolve native IDs in code |
| Five isolated analyses plus full verification | Several speculative token-volume/readiness claims; verifier dominated cost | Require an explicit support type; reject unsupported regression and observed-error claims |
| Focused verification | Large raw detail was still expensive | Expand cited episodes and nearby results with bounded output-line windows; retain the full compact timeline |

One native test also caught a compaction bug: repeated-command candidates could
crowd retry context out of the detail budget. Failed steps and their immediate
neighbors now take priority. Another test caught a copied `automations.db`
filename being overlooked by replay; both native and exported filenames work.

## Controlled verifier comparison

The final comparison reused the same four supported candidates, model, settings
and instruction from the preceding five-case run. Only verifier context selection
changed. Each configuration was run once; cache state and provider load were not
controlled, and the model's final selection changed.

| Measurement | Broad verification | Focused verification |
|---|---:|---:|
| Request bytes | 261,990 | 173,227 |
| Input tokens | 71,797 | 47,789 |
| Request latency | 15.254 s | 9.909 s |
| Proxy-reported cost | $0.185187 | $0.1210902 |

The five cheap analysis calls in the preceding run reported $0.00997325 in total.
Collection took 0.436 seconds. That full run, including broad verification, took
25.365 seconds of inference wall time with two workers. The focused verifier was
tested separately; there is no measured complete end-to-end focused-run time.
Reported cost is not a verified invoice. These observations do not establish
expected production latency, precision, recall or an accuracy improvement.

## Evidence inspection

Spot checks of the referenced native records confirmed:

- A typecheck/lint subprocess reported exits 134/137 while its wrapper returned
  zero; output included JavaScript heap exhaustion. A zero wrapper status does
  not establish successful validation, and exit 137 does not prove a specific
  infrastructure cause.
- A Docker PostgreSQL fixture could not connect to the Docker socket. Later
  local database startup was observed; this is not proof of final task success.
- A local pytest command printed five passing tests, a closed-stream logging
  traceback, and `EXIT=1`. Passing assertions do not establish a successful command.

These are investigation candidates, not three proven workflow defects. Some
wrappers intentionally return zero after collecting diagnostics. Executed source,
task requirements, recovery and actual improvement evaluation still matter.

Two copied conversation states remain `running` despite terminal run rows; one
continued producing events between exports. Replay therefore preserves unknown
outcomes, missing-data indicators and the prior review cutoff. It does not
advance production state or claim full-window conversation coverage.

## Reproducibility and next checks

Every request body, response and usage measurement is saved privately outside git.
Per-run source snapshots and input/policy hashes identify the experiment variant.
Native IDs and event filenames are resolved from input records, not model output.
No credentials or raw production exports are committed here.

The original thirteen targeted tests cover read-only native input handling, the original
entrypoint's replay branch, middle-of-session failure/retry preservation,
embedded exit markers, reference validation, prompt interception and redaction,
input budgets, captured policy selection, and focused error-line retention.

Before publishing automated findings, create independent human labels, test the
support gates for missed semantic issues, audit negative cases, and measure repeated
cold/warm runs. The explicit gates favor precision and can miss issues with no
tool error or explicit policy evidence. The October 16 extension below adds local
Laminar flow-1 analysis. Production integration remains a separate milestone.


## Random sampling and native flow-1 replay — October 16

Selection is now uniform without replacement over unique available conversations
in the metadata window. A recorded seed and population hash reproduce selection;
run duplication, cost and status do not affect it. The default window is a fixed
week, independent of the saved production cutoff. No weighting, family quotas,
statistical outlier selection or outer improvement loop is implemented.

The analyzer imports full redacted action/result payloads and assistant messages
into local Laminar. Native IDs are retained in a reference manifest. This avoids
feeding flow-1 the alternate analyzer's already clipped timeline. Laminar builds
its own preview/read context; the adapter records Signal definitions and imported
spans, not its internal provider prompts. This is an extension of the native
monitor entrypoint, not a separate production pipeline.

A small synthetic native bundle contained six conversations in SQLite plus native
state/event files. Seed 42 selected conversations 6, 1 and 5: a healthy run, a failed
test followed by a false success claim, and an explicitly acknowledged failed
check followed by a successful rerun. Their 14 native events exercised the same
readers and adapter used for host replay.

| Sampled case | Signal result | Native evidence | Inference time | Usage cost before credits |
|---|---|---|---:|---:|
| Healthy test run | noEvent | — | 1.217 s | $0.00017617 |
| Failure hidden by success claim | event | task, e1, m2 | 2.444 s | $0.00030777 |
| Acknowledged failure and successful rerun | noEvent | — | 1.598 s | $0.00027897 |

The run completed in 4.614 seconds of wall time with two workers and three Signal
calls. Total published-rate usage value was $0.00076291, with 5,952 cached input
tokens per call. There were no rejected references or analysis errors. These are
tiny warm-cache connectivity/control cases, not a production performance or
quality evaluation. This run did not exercise the optional coordinator.

Earlier attempts exposed two integration issues: a 1,800-token cap truncated one
response, and flow-1 sometimes cited Laminar's six-character span IDs rather than
the requested evidence labels. The default Signal output cap is now 4,096 tokens;
full/short span IDs, including Laminar's virtual LLM output spans, resolve only
through the selected trace's manifest. Unknown or ambiguous citations fail closed.
A recovered control initially claimed a code fix absent from its events; that
unsupported claim was removed before the final run.

Twenty-nine targeted tests now pass, adding coverage for unweighted sampling,
duplicate run records, missing export coverage, full native output retention,
trace-specific citations, changed/oversized snapshots, incomplete Signal results,
local-only test routes, cached-token pricing and bounded message context for
optional verification. Changed/oversized traces and
Signal failures are never replaced with new samples; errors and returned usage
remain visible. No new production-data experiment or automated fix was run for
this extension.
