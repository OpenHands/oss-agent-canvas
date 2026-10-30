# Weekly Automation Improvement

A standalone prototype for OSS-14275: persisted Flow-1 findings become a prioritized
queue of repair topics. Native OpenHands agents investigate and repair the underlying
problem, including SDK bugs, and open PRs with before/after evidence. An independent
agent reviews the actual changes and execution records. Humans merge and deploy.

## Weekly path

1. Laminar runs Flow-1 on matching finished automation traces according to its sampling
   configuration. Python reads the persisted Signals, tracks a private backlog and
   prepares compact counts/examples by automation and cluster/signature. Clusters
   are leads, not confirmed root causes.
2. A native OpenHands triage agent, default Luna, reads a structured weekly issue
   indexing the preparation output, raw data, previous attempts and existing PRs.
   It merges duplicates, splits independent problems and ranks one finite queue
   globally by P0–P3 priority. A topic can span automations and repositories.
3. Python validates references and dispatches the highest-priority topics once,
   in parallel. A failed attempt does not cause a replacement topic to be selected.
4. Each native repair agent, default Sol 6.1, owns its topic end to end: diagnosis,
   repository selection, edits, tests, commits, issues and PRs. It uses normal
   OpenHands tools and GitHub access. There is no patch-proposal protocol or separate
   publisher in the weekly path. One topic may require several related PRs.
5. A separate native agent reviews current diffs, exact revisions, CI and actual
   execution records. It checks the complete fix across repositories, records a
   verdict on the issues/PRs, and makes sufficiently evidenced PRs ready for review.
   Failed, inconclusive, unsafe or dependency-blocked changes remain drafts.
   Python also holds PRs draft when review is incomplete or revisions changed.

The repair agent makes one best attempt and stops. The reviewer does not restart
coding or require unrelated inherited defects to disappear. An unrepaired topic
with a clear explanation is valid. There is no loop trying to fill a PR quota or
force every repair to pass.

## Issues and evidence

Create or reuse one structured issue in **each repository changed by a topic**.
All repository issues link one another and all related PRs. Each PR links its local
issue and describes dependencies and any required merge/release order. One issue
is the main entry point; a separate umbrella is unnecessary.

Issue sections are **Goal**, **Problem and impact**, **Scope and source**,
**Evidence**, **Validation**, and **Attempts and decisions**. Existing human text
is preserved. The initial issue creation includes the `automation-improvement`
label and stable topic marker. Both distributed issue-triage workers skip these
managed issues before reading comments or dispatching work.

Public issues contain safe summaries, source/trace references and private evidence
catalogs. Original inputs, prompts, Signal payloads, SDK events/completions and logs
remain in owner-only worker files. Immutable content-addressed catalogs are indexed
under `state_dir/issue-evidence/<owner>/<repository>/<topic>/`.
`evidence_location` tells authorized readers how to reach the worker. Public GitHub
access alone does not grant access to private artifacts.

Agents can page through issue sections/catalogs and use `search_runs`, `read_signal`,
`list_spans` and `read_trace` via the CLI described in their task. The repair agent
receives the frozen inputs/cohort, repository revisions, live automation metadata,
prior attempts and the full prepared inventory. It can inspect additional prepared
automations when a shared failure requires it. Missing source/runtime access is
explicit context, not a reason to force an automation-only fix.

The repository catalog is starting context, not an allowed-repository list.
Existing canonical checkouts supply isolated worktrees; there are no file-count
or patch-size limits. A worktree separates source, not operating-system access.
Use a dedicated development worker with the intended source/data permissions.

## Validation

`validation_sample_size` is a top-level positive integer, default **5**: up to N
topic-linked negative conversations and N uniformly sampled completed no-finding
conversations across the represented automations. The cohort is frozen before
repair, with no replacement after failures. No-finding does not mean known-good.
Missing inputs and collection failures remain explicit.

The agent chooses a suitable isolated replay or regression/control check. Compare
coherent baseline SDK+automation versions against the complete proposed fix;
intermediate combinations are necessary only for compatibility claims. Retain
commands, working directories, input hashes, revisions and actual outputs privately.
Synthetic mechanism evidence must reproduce the actual baseline defect, not just
a missing new import or a new implementation contract.

Each PR has **Before / After Evidence**: observed differences, available/tested
negative and control counts, checks, version combinations, limitations and linked
issues/evidence. The independent reviewer evaluates the stated scope rather than
demanding population-wide proof from a small sample. Ready status is not approval
to merge. The existing [repair evaluator](../../evals/automation-repair/README.md)
remains available as a tool; the weekly path does not require a prebuilt per-task
adapter or run the legacy publisher.

## Configure and run

Copy [weekly.example.json](weekly.example.json) outside git. Configure Laminar
project/Signal/endpoints, proxy endpoint, home repository/canonical checkout,
private state directory, native automation API and GitHub secret name. Register
other useful existing checkouts under `repositories`, including the SDK. If no SDK
entry is supplied, its expected sibling checkout is included with any access error.
The agent must report missing prerequisites rather than clone an extra repository.
Ensure `automation-improvement` exists in repositories where repairs will be opened.

Top-level settings:

- `max_topics_per_run`: default **10**, maximum topic investigations selected once
  from the ranked queue. It counts attempts, not successful repairs or PRs.
- `repair_concurrency`: defaults to the topic limit; example **10**. One process per
  selected topic, each with its own repair and independent review conversations.
- `validation_sample_size`: default **5** inputs per cohort.
- `triage_agent_profile`, `repair_agent_profile`, `review_agent_profile`: saved native
  OpenHands profiles. Null uses built-in defaults; review falls back to the repair
  profile when its setting is omitted. Explicit null selects the built-in reviewer.
- `triage_model`, `repair_model`, `review_model`: built-in model defaults. Saved
  profiles own their models, tools, skills, MCP, persona, condenser and secrets.
- `triage_agent`, `repair_agent`, `review_agent`: time/iteration limits and built-in
  LLM settings. Triage defaults to 600 seconds/40 iterations; repair 3600/200;
  independent review 900/80.
- `max_inference_cost_usd`, `max_run_seconds`: resource guards (defaults $15 and
  10800 seconds). Parallel workers inherit the remaining allowance at dispatch;
  costs are reconciled when they finish. Already-running siblings can exceed the
  aggregate dollar threshold. This is not a strict shared billing cap.
- `retry_topic_ids`: explicit reconsideration of known topics, including an attempt
  already dispatched this week. Prior conclusions stay visible to both agents.

Migrate `max_prs_per_run` to `max_topics_per_run`; the old setting fails explicitly.
Legacy publisher/revision/existing-PR-comparison settings no longer control the
weekly path. Triage sees prior unsuccessful approaches and should reconsider only
when new evidence, source changes or explicit retry justify doing so.

The existing GitHub worker credential needs issue/PR and push access in affected
repositories. The SQL-capable self-hosted Laminar project key and proxy key stay in
an owner-only credentials file outside git:

```json
{"LMNR_PROJECT_API_KEY":"...","LITELLM_API_KEY":"..."}
```

The Flow-1 Cloud key stays in Laminar's LLM profile. Saved agent profiles resolve
through `AGENT_SERVER_URL` with the native session credential; profile failures do
not silently switch agents. The worker honors `AUTOMATION_CONFIG_PATH`, defaulting
to `~/.config/automation-improvement/config.json`.

```bash
AUTOMATION_API_URL=http://localhost:8000/api/automation bash automations/weekly-automation-improvement/tarball/setup.sh

automations/weekly-automation-improvement/tarball/.venv/bin/python automations/weekly-automation-improvement/tarball/weekly.py \
  --config /private/weekly.json --input /private/frozen-inventory.json --dry-run
```

`--input` loads an inventory upfront; deeper reads/cohort hydration can still need
Laminar. `--dry-run` prepares and triages only, spending inference tokens but making
no GitHub writes, repair dispatches or canonical ledger changes. Omit it to run
repairs. `--limit 1..3` bounds representative conversations per topic, not topic
count or total collected Signals. The YAML schedules Mondays at 08:00 UTC.

## State and recovery

The single-worker weekly lock prevents overlapping invocations. Python records
selected topic ownership before dispatch and records remote URLs as native events
arrive. Completed reports can contain arrays of issue/PR resources across repos.
Discovery checks actual GitHub objects and topic markers, including links captured
outside the starting catalog. Interrupted work stays visible for reconciliation;
there is no automatic rerun/filler loop. Lower-priority deferred findings remain
pending. Retain the worker state directory when replacing its VM.

The factory issue-triage automation and weekly triage share the native OpenHands
profile foundation, but have different policies and lifecycles. Factory triage
maintains acceptance criteria/readiness labels on individual GitHub issues using
a persistent dispatcher; weekly triage awaits one finite cross-automation ranking
from Laminar evidence. Importing one independently packaged plugin's worker into
another would couple deployment and apply the wrong policy. The ownership guard
prevents both from rewriting the same managed issues.

## Laminar sampling

[signal.example.json](signal.example.json) is a paused installation template with
the detector payload, execution-finish trigger and automation filter. `sampleRate:
null` means all matching traces. The public numeric range is 1–95; zero and 100
are not substitutes for disabling sampling. Apply either choice explicitly:

```bash
python3 automations/weekly-automation-improvement/tarball/configure_signal.py \
  --config /private/weekly.json --no-sampling

python3 automations/weekly-automation-improvement/tarball/configure_signal.py \
  --config /private/weekly.json --sample-rate 50
```

The setup command changes only sampling and verifies the detector's other settings
by reading it back. Reapplying the same setting creates no version. It does not
backfill history. Production Signal `fadaa9d7-a70a-42df-ab63-04c88f3b77ac` belongs
to project `e41ee96e-851b-45ad-bfe8-b986861c331d`. Rotate the Cloud credential before
November 15. At previously measured volume/usage, full coverage is estimated at
$18–36/month in Flow-1 inference, excluding subscriptions, Luna and Sol.

Reports estimate Flow-1 gross cost from token usage and the dated
[pricing snapshot](tarball/flow1-pricing.json); that is not an invoice. Unknown
usage remains unknown. Per-stage receipts distinguish SDK-estimated agent costs
from proxy-reported review costs and retain latency/completions. The shared cost
threshold remains a safety limit; incomplete accounting blocks further inference.

## Local implementation status

This architecture is implemented locally. Focused mocked unit checks cover its
orchestration and metadata safeguards; live end-to-end validation is deliberately
pending. Earlier [validation reports](TOPIC_VALIDATION.md) describe previous
versions and do not validate this redesign. No existing PRs were changed as part
of the local implementation.
