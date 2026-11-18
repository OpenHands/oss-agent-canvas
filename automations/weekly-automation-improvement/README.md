# Weekly Automation Improvement

A standalone prototype for OSS-14275: persisted Flow-1 findings become a prioritized
queue of repair topics. Native OpenHands agents investigate and repair the underlying
problem, including SDK bugs, and open PRs with before/after evidence. An independent
agent reviews the actual changes and execution records. Humans merge and deploy.

Start with [How the self-improving automation works](HOW_IT_WORKS.md) for the
high-level workflow, agent responsibilities, evidence and main controls. This
README covers setup, operation and validation history.

## Weekly path

1. Laminar runs Flow-1 on matching finished automation traces according to its sampling
   configuration. Python reads the persisted Signals, tracks a private backlog and
   prepares compact counts/examples by automation and cluster/signature. Clusters
   are leads, not confirmed root causes. Findings with missing or ambiguous owners
   remain selectable, with candidate origins and attribution warnings.
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

Repair and review form a bounded feedback loop. The default
`max_repair_attempts=2` allows an initial repair and one revision using the review
feedback, followed by another independent review. Revisions reuse the same
native repair conversation, worktrees, current-run issues/PRs, original baseline
and frozen comparison cohort. A short follow-up message announces the completed
review and points to its feedback and evidence. The repair agent retains its
history and tools, allowing the provider to reuse cached input prefixes; actual
cache hits depend on the model/provider. Each independent review starts a separate
conversation.
The reviewer does not edit repair source or require unrelated inherited defects
to disappear. An unrepaired topic with a clear explanation is valid.

Each reviewer records a concise decision and feedback: **accepted**, **revise** for
agent-addressable code or validation gaps, or **blocked** for an external/human
prerequisite. Python enforces the attempt limit and reuses the existing topic
budget, charging only the incremental spend of each resumed repair round. Accepted
or blocked decisions stop; missing/invalid review decisions and
incomplete reviews hold PRs draft and stop. Failed or incomplete repair executions
do not automatically start another repair, even when their PRs can be reviewed.
An exhausted attempt limit leaves
unresolved PRs draft. No replacement topics are dispatched to fill a quota.

## Issues and evidence

Triage completes before repair dispatch. It checks open issues/PRs, including
human-created work, and previous self-improvement work even if closed. For a
matching problem, the triage agent posts an idempotent recurrence comment with new
Signal/trace references and a private evidence catalog; it leaves descriptions
unchanged and skips repair. These topics do not consume `max_topics_per_run`:
the next eligible new topics fill the queue, then repairs run in parallel. Comment
failures remain pending without launching duplicate repairs. Discovery failures
prevent dispatch rather than treating unavailable history as absent work.

Repair agents also check for existing work as a safeguard against changes after
triage. For a newly selected problem, create a focused issue and cross-reference
related work. Keep one main issue per affected repo.
Repository issues link related work when visibility permits; private relationships
remain in the private evidence index. Each PR links its local
issue and describes dependencies and any required merge/release order. One issue
is the main entry point; a separate umbrella is unnecessary.

Issue sections are **Problem and impact**, **Observed example**, **Proposed
prevention**, **Validation**, **Current status**, **Evidence**, and **Attempts and
decisions**. The opening sections explain expected versus observed behavior,
impact, a sanitized concrete example, the proposed prevention and current results
without requiring private-data access. Agents rewrite these sections after
diagnosis, validation and review, correcting stale claims instead of appending
competing summaries. Agents update a new issue's body or their own current-run
brief comment on a reused issue; descriptions from humans or earlier runs remain
unchanged. New issues include the `automation-improvement` label, stable topic
marker and run marker; reused issues carry those markers in the brief comment.
Discovery checks body or comment ownership. Both distributed issue-triage
workers skip these managed issues before reading comments or dispatching work.

Public issues contain safe summaries, source/trace references and private evidence
catalogs. Original inputs, prompts, Signal payloads, SDK events/completions and logs
remain in owner-only worker files. Immutable content-addressed catalogs are indexed
under `state_dir/issue-evidence/<owner>/<repository>/<topic>/`.
`evidence_location` tells authorized readers how to reach the worker. Public GitHub
access alone does not grant access to private artifacts.

Before rewriting its brief, the agent archives the prior body or comment under
`validation/issue-history/`. The body links the latest catalog; every older catalog
and artifact reference remains in the comments with its private root. Related-work
links occupy one updated section. To read an earlier run's catalogs, pass the
`context.json` beside its report as the evidence CLI's `--context`, preserving that
run's data namespace. Public titles, descriptions, comments and patches must be
sanitized before publication. Credential checks and independent review provide
additional checks; incomplete issue structure or detected credentials hold PRs
draft rather than certifying them ready.

Agents can page through issue sections/catalogs and use `search_runs`, `read_signal`,
`list_spans` and `read_trace` via the CLI described in their task. The repair agent
receives the frozen inputs/cohort, repository revisions, live automation metadata,
prior attempts and the full prepared inventory. It can inspect additional prepared
automations when a shared failure requires it. Missing source/runtime access is
explicit context, not a reason to force an automation-only fix.

The evidence CLI also accepts `--finding-id EVENT_UUID` instead of
`--automation-id`: it exposes only that prepared finding's Signal and trace,
including findings carried over from earlier windows. Agents can inspect cited
spans and conversation/task boundaries before assigning an owner. Candidate
conversations are not counted as confirmed failures or selected arbitrarily for
replay; unresolved attribution stays in the cohort's `attribution_gaps`. Possible
owners are excluded from non-negative controls. Automation origin does not determine the repair destination; the configured repair
scope determines where agents may propose changes.

`repair_scope` defaults to `any_authorized`: the home `repository` and entries
in `repositories` provide starting context; other relevant repositories are
permitted when the installation authorizes access. Set `customer_stack` to restrict
repair destinations to the home repository plus the explicitly listed entries.
No SDK or extensions repositories are implicitly added. Both modes consider
customer-owned automation/SDK integration code,
prompts, skills/plugins, tools/MCP configuration, hooks, agent/LLM profiles,
compaction/memory settings, dependencies and runtime setup.

```json
{
  "repair_scope": "customer_stack",
  "repository": "acme/automations",
  "checkout": "/existing/acme-automations",
  "repositories": {
    "acme/agent-config": {"checkout": "/existing/acme-agent-config"}
  }
}
```

The example above opts into customer-only repairs. Upstream proposals are allowed
by default when authorized. In customer-only mode, agents may investigate upstream
source but record upstream blockers in customer issues; they do not publish
upstream issues or PRs. They must not invent a workaround when no sound
customer-side fix exists.

The scope is carried in private evidence and triage/repair/review instructions,
including resumed repairs. Deterministic checks reject out-of-scope outputs and
block acceptance; recurrence comments also check their destinations. These checks
do not sandbox terminal tools or undo an unauthorized write. Use a dedicated
worker and GitHub credentials whose write permissions match the intended scope;
saved agent profiles and MCPs must not expose broader write credentials.
Public upstream issues must not identify private customer repositories. Private
issues can link public counterparts; public summaries retain an opaque private
evidence index. The complete relationship map and raw references remain private.
Unknown repository visibility blocks acceptance. Broader-scope runs remember repair
repositories from their own history for future existing-work discovery. Triage can
also discover related work through normal GitHub reads; Python verifies those URLs
before posting recurrence comments or excluding a topic. Repository
worktrees isolate source, not operating-system access. There are no file-count or
patch-size limits. Missing checkouts remain explicit prerequisites.
Install `tmux` on the worker before
setup so native terminals support reliable interruption. A login-disabled service
account also needs a usable tmux default shell, for example
`set -g default-shell /bin/bash` in its `~/.tmux.conf`.

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
relevant checkouts under `repositories`. Upstream contributions are allowed by
default when authorized; set `repair_scope=customer_stack` for customer-only repairs.
The agent must report missing prerequisites rather than clone an extra repository.
Ensure `automation-improvement` exists in repositories where repairs will be opened.

Configuration repairs must use the actual settings of the cited runs and their
source of truth. Retain effective baseline/candidate settings privately. A profile
stored only through a UI/API needs a supported application path; adding an unused
JSON file is not a fix. Repair agents propose changes without changing live
settings. Validate affected shared configurations across automations where data
exists, and report task quality, cost and latency when relevant.

Top-level settings:

- `repair_scope`: **any_authorized** (default) or **customer_stack**. The default
  can discover additional authorized destinations. Customer-only mode restricts
  repairs to the home repository and explicit `repositories` entries.

- `max_topics_per_run`: default **10**, maximum topic investigations selected once
  from the ranked queue. Revisions do not consume another topic slot; the limit
  counts topics, not successful repairs or PRs.
- `max_repair_attempts`: default **2**, total repair attempts per topic, including
  the initial attempt. **1** disables feedback revisions. Each attempted repair is
  followed by a new review when PRs exist and budget remains; inner agent tool-call
  limits remain configured separately under each agent's `max_iterations`.
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
- `retry_topic_ids`: explicit reconsideration of failed topics without tracked
  issues/PRs, including an attempt already dispatched this week. It does not
  bypass existing-work exclusion. Prior conclusions remain visible.
- `isolated_validation`: default **false**. Quality-control runs set it true with
  a fresh private state directory: previous prototype issues/PRs and conclusions
  are excluded from triage context, and repair agents create fresh outputs without
  reading or citing prior validation work. This does not change production reuse.

Migrate `max_prs_per_run` to `max_topics_per_run`; the old setting fails explicitly.
Legacy publisher/revision/existing-PR-comparison settings no longer control the
weekly path. Triage sees prior unsuccessful approaches; tracked work receives
recurrence comments instead of another repair agent. Bounded revisions within
one current run remain controlled by `max_repair_attempts`.

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

The first attempt's execution files remain at the worker root; later attempts
live under `attempts/002/`, `attempts/003/`, and so on. Each attempt has an immutable
`attempts/NNN/outcome.json` record. The top-level `topic-report.json` points to the
latest attempt and records the complete attempt index and stop reason. Revision
context references the previous review, reviewer events, validation artifacts and
existing remote resources. The repair conversation persists under the first
attempt's `repair-agent/conversation`; each round retains its own new events,
completions, conversation ID and incremental cost. Earlier evidence catalogs
remain available. The repair conversation closes when the topic finishes or
exits with an error.

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

## Live validation of retained conversations and recurrence

Commit `4a115cb` completed a fresh VM run using 63 persisted Flow-1 findings,
live Laminar evidence reads, Luna triage, four parallel Sol 6.1 repair agents and
independent reviews. Six prior validation issues were archived and closed first.
The entry point is [weekly issue #102](https://github.com/OpenHands/oss-agent-canvas/issues/102).

| Topic | Outputs | Final independent result |
| --- | --- | --- |
| Evidence-based priority | [canvas #108](https://github.com/OpenHands/oss-agent-canvas/pull/108), [issue #104](https://github.com/OpenHands/oss-agent-canvas/issues/104) | Ready: one fresh false escalation corrected; all three negatives stay low and five control decisions remain unchanged |
| Review publication | [canvas #110](https://github.com/OpenHands/oss-agent-canvas/pull/110), [extensions #890](https://github.com/OpenHands/extensions/pull/890); issues [#103](https://github.com/OpenHands/oss-agent-canvas/issues/103), [#888](https://github.com/OpenHands/extensions/issues/888) | Ready: original empty requests blocked; complete controls preserved in 52 HTTP-boundary expectations and 12 focused tests per copy |
| Verification claims | [canvas #109](https://github.com/OpenHands/oss-agent-canvas/pull/109), [issue #105](https://github.com/OpenHands/oss-agent-canvas/issues/105) | Draft after two attempts: negative support improves from 0/3 to 2/3; unsupported claims and unreliable semantic auditing remain |
| Interrupted review delivery | [issue #106](https://github.com/OpenHands/oss-agent-canvas/issues/106) | Unrepaired: 32 baseline HTTP checks confirm existing SDK retries; durable delivery work needs the owning service checkout/test environment |

The revision used the **same repair conversation**, separate reviewers and unchanged
frozen inputs/cohorts. Its first resumed request cached 71,195 of 73,436 input
tokens (97%). Incremental phase costs reconcile exactly to **$21.69** in recorded
native agent inference, over approximately 90 minutes. Standalone replay inference
is outside that accounting. No worker exhausted its cost guard.

A follow-up live reader found 44 additional findings. Targeted recurrence triage
used 36 belonging to the same automations, matched four existing topics and posted
nine idempotent evidence comments without changing existing descriptions. All
four stayed out of the repair queue. Five new topics remain queued in
[issue #111](https://github.com/OpenHands/oss-agent-canvas/issues/111); this follow-up
was a triage/comment check and did not dispatch their repairs. Its recorded cost
was $0.0155.

Read-only audits verified 6,678 catalog references, frozen evidence, conversation
identity, reviewer independence and final published patches. No missing artifacts,
credential findings or retired-output references were detected. These are scoped
captured-input projections and HTTP-boundary checks, not full historical task
replays or population-wide prevention evidence. Existing suite failures and
unavailable CI remain explicit in each PR. Generated outputs were not manually
polished; scheduled workers and production Signals were unchanged. All four PRs
carry `automation-improvement`; 304 local pipeline tests pass.

## Fresh issue-lifecycle validation — 2026-11-02

An isolated run started with the frozen 63-finding inventory and no prior prototype
issues, PRs or conclusions in agent context. Fresh Luna triage selected four topics;
four parallel Sol 6.1 agents opened five PRs across three repositories. All four
independent reviews completed. The entry point is
[weekly issue #97](https://github.com/OpenHands/oss-agent-canvas/issues/97).

| Topic | Generated output | Independently reviewed outcome |
| --- | --- | --- |
| Credential output | [SDK #6389](https://github.com/OpenHands/software-agent-sdk/pull/6389) | Terminal redaction mechanism confirmed; draft pending CI and intermittent-test adjudication; prefix disclosure remains uncovered |
| Review publication | [canvas #101](https://github.com/OpenHands/oss-agent-canvas/pull/101), [extensions #885](https://github.com/OpenHands/extensions/pull/885) | Payload preflight verified; autonomous recovery remains inconclusive; both draft |
| Review verification claims | [extensions #886](https://github.com/OpenHands/extensions/pull/886) | One target corrected, remaining errors and a control regression; draft |
| Triage priority evidence | [canvas #100](https://github.com/OpenHands/oss-agent-canvas/pull/100) | One target corrected, another still fails; draft |

The agents rewrote their current issue sections as diagnosis and validation changed,
archived prior versions privately, and retained raw-data references and execution
catalogs. Deeper source reads corrected initial Signal interpretations, including
an evaluation result that existed but belonged to an older revision. Public issues
explain the mechanism, prevention, comparison and remaining work before evidence.
No generated issue or PR was manually polished to improve the validation result.

Recorded native triage/repair/review inference was **$20.28**, excluding separately
invoked replay inference. The pipeline has **272 passing tests** and clean Ruff
checks. The run exposed output-discovery errors on ordinary source issues and
synthetic fixture URLs, plus a credential-pattern false positive on SDK CI's public
image tags. The fixes preserve actual-output failure checks and raw credential
detection. Read-only checks using the updated code passed for all four saved
output sets: ownership, public structure, links, credentials, tracking labels and
unchanged reviewed revisions. Original run reports and native draft verdicts
remain intact. This is validation with
bookkeeping fixes, not an uninterrupted production-ready pass or a claim that all
four topics were solved. Scheduled workers and production Signals were unchanged.

## Earlier staged repair validation

The saved 63-finding inventory and five-topic Luna queue were continued through
parallel Sol 6.1 repair workers and independent review. The shared entry point is
[weekly issue #92](https://github.com/OpenHands/oss-agent-canvas/issues/92).

| Topic | Output | Original negative / non-negative cohort | Independent outcome |
| --- | --- | --- | --- |
| Credential-safe diagnostics | [#84](https://github.com/OpenHands/oss-agent-canvas/pull/84) | 1 / 0 | Ready for human review; scoped SDK execution/persistence mechanism accepted |
| Failed reviewer execution outcomes | [#94](https://github.com/OpenHands/oss-agent-canvas/pull/94) | 2 / 5 | Ready for human review; reporting-boundary improvement, no provider-recovery claim |
| Complete candidate pagination | [#86](https://github.com/OpenHands/oss-agent-canvas/pull/86) | 1 / 1 | Draft; incomplete autonomous evidence and a conditional API rate-budget concern |
| Reviewer check-result integrity | [#93](https://github.com/OpenHands/oss-agent-canvas/pull/93) | 2 / 5 | Draft; partial prompt improvement with control regressions |
| Publication content verification | [extensions #878](https://github.com/OpenHands/extensions/pull/878) | 3 / 5 | Scoped source mechanism accepted; draft until the required human PR-template note is supplied |

The outputs include Before / After Evidence, source versions, tested/available
counts, limitations and private evidence links. Cohort counts are per topic;
controls can overlap across topics and are not presumed healthy. Boundary/action
projections and synthetic perturbations are identified separately from full
historical task execution. The canvas candidates retain the same unrelated
Dependabot-default suite failure as baseline; no green-CI claim is made.

This was staged validation with recovery, rather than a clean unattended run:
the worker needed tmux with a usable service-account shell, the missing canonical
extensions checkout was supplied from an existing canonical repository, and one
pagination continuation preserved its worktrees/inputs after an inference timeout.
The setup/source-catalog fixes are included here. Original failures and subsequent
attempts remain in the private receipts. No existing PR was closed, merged or
deployed, and no production Signal configuration was changed.

## Live validation — 2026-10-30

The pilot loaded 59 findings across ten automations upfront. After accounting for
previous work, Luna ranked 43 eligible findings across eight automations and
selected two topics. Both Sol workers launched in parallel; each had three
negative and five randomly selected no-finding original inputs.

- [PR #90](https://github.com/OpenHands/oss-agent-canvas/pull/90), linked to
  [issue #73](https://github.com/OpenHands/oss-agent-canvas/issues/73), adds an
  isolated-baseline worktree recipe. Independent review kept it draft: the
  decision-point replay observed zero target mutations on both baseline and
  candidate, so behavioral improvement was not demonstrated.
- [PR #91](https://github.com/OpenHands/oss-agent-canvas/pull/91), linked to
  [issue #89](https://github.com/OpenHands/oss-agent-canvas/issues/89), adds a
  priority-evidence checkpoint. Its three selected negatives change from
  high/medium/medium to low/low/low. Independent review kept it draft because
  one original control's SDK-timeout reproduction changes medium to low and
  its intended treatment needs policy adjudication. The 32 recorded decisions
  include separate synthetic controls; they are not 32 historical incidents.

These are checkpoint projections with captured context, not complete historical
replays or evidence of population-wide benefit. Both candidate suites retain the
same unrelated Dependabot-default failure present in baseline; neither has green
GitHub CI. Independent reviewers did not treat that inherited failure as a reason
to reject a scoped improvement.

The run required operator recovery. It exposed blocking task delegation waiting
on a parent-owned candidate update, oversized evidence catalogs, expensive indexing
of environments/checkouts, and a missing issue-to-PR body link. The implementation
now documents blocking delegation, enforces deadlines outside ordinary tool error
handling, filters non-evidence directories, stores oversized catalogs as complete
private indexes, and maintains related-work links while preserving human content.
Completed repair/review records can be finalized without another coding run.

All **249 pipeline tests pass**, including recovery, deadline, large-catalog and
cross-repository reference checks. The pilot exercised the stages with recovery;
a clean uninterrupted unattended run and a net-improvement acceptance remain
unproven. Original partial receipts, SDK-resume failure and corrected attempts
remain private and indexed from the issues. The scheduled worker and Laminar
Signal configuration were not replaced by this one-off validation.

A separate read-only attribution regression check reloaded the same 63 candidate
findings from Laminar. All 59 previously retained findings remained, and all four
previously excluded findings became selectable, including an SDK-recovery lead.
Their two possible conversations remain explicit; none was arbitrarily assigned a
negative replay input. Both agents' finding-scoped readers retrieved the original
Signal and trace, and rejected access to an unrelated trace. This check used no
LLM inference and created no repair PRs; it verifies collection/evidence access,
not that any recovered lead is a confirmed defect or a successful repair.

Earlier [validation reports](TOPIC_VALIDATION.md) concern previous versions and
should not be treated as validation of this architecture.

## Repair-scope validation — 2026-11-06

Two independent native-agent runs used the same saved **63 production findings
across ten automations**, with live private Laminar reads and fresh issues/PRs.
Each selected five topics. Customer-only mode created five PRs in its one configured
repository; broader mode created four there and one in the SDK. All ten carry
`automation-improvement`. Independent review accepted five; five remain draft.

| Scope | Output | Reviewed outcome |
| --- | --- | --- |
| Customer | [#132](https://github.com/OpenHands/oss-agent-canvas/pull/132) | Ready: safe diagnostic preparation removes captured credential values while retaining useful flags; 14 independent focused tests pass. |
| Customer / broader | [#130](https://github.com/OpenHands/oss-agent-canvas/pull/130), [#134](https://github.com/OpenHands/oss-agent-canvas/pull/134) | Ready alternative publication safeguards: three captured empty-body accepts become zero; readable controls remain intact. Choose one variant. |
| Broader | [#135](https://github.com/OpenHands/oss-agent-canvas/pull/135) | Ready: verify required triage updates before acknowledging completion or suppressing retries; 16 focused tests pass. |
| Broader | [SDK #6552](https://github.com/OpenHands/software-agent-sdk/pull/6552) | Ready regression coverage for instrumented stream-startup retries. Tests only; it does not deploy a runtime fix. Deployment/application prerequisites remain explicit. |
| Customer | [#129](https://github.com/OpenHands/oss-agent-canvas/pull/129) | Draft: provider-interrupted review was retried; reporting replays still lack valid paired behavior/control evidence. |
| Customer | [#131](https://github.com/OpenHands/oss-agent-canvas/pull/131) | Draft: fixture permissions, SDK isolation and incomplete reconstructions prevent a valid workspace-behavior comparison. |
| Customer | [#136](https://github.com/OpenHands/oss-agent-canvas/pull/136) | Draft: an independently reproduced clarified-request regression remains after the bounded revision. |
| Broader | [#128](https://github.com/OpenHands/oss-agent-canvas/pull/128) | Draft: the exhausted-publication path still attempts a forbidden extra submission. |
| Broader | [#133](https://github.com/OpenHands/oss-agent-canvas/pull/133) | Draft: some reporting improves, but delivered bodies retain factual errors and a control regression. |

These are scoped projections, real loopback/SDK checks and regression tests—not
complete historical-task replays. Exact historical environments, missing controls,
shared inherited suite failures and CI/human requirements remain visible in each
PR. Ready means ready for human review. The two QC runs intentionally excluded
previous and parallel prototype outputs; their publication variants overlap.

**338 local tests pass.** Coverage includes scope propagation through resumed
repairs, out-of-scope output/recovery holds, private/public link handling, complete
private evidence indexes, standard GitHub issue references, verified new existing
work and history discovery without widening customer scope. Live readback checks
all ten PRs, comments and patches against scope, links, known worker credentials
and captured editor-token values.

The full repair runs started on `2be890a`. Follow-up privacy/discovery fixes were
validated through the local suite, native latest-code triage checks, an additional
native review and live GitHub readback/probes. Latest triage checks used the full
63 findings for customer-only mode and a deterministic 12-finding sample for
broader mode; a full broader preview timeout is retained in private receipts.
The original repair cohorts were not replaced. Native inference for the two full
runs was approximately **$76.61**; standalone projections and supplementary checks
are outside that accounting. Both runs ended within their configured guards.
Scheduled workers, production settings and deployments were unchanged.
