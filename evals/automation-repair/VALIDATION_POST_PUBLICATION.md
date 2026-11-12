# Post-publication comparison

On 2026-10-28, the comparison hook ran against the already-open
[reviewer repair #54](https://github.com/OpenHands/oss-agent-canvas/pull/54) and
published its **Before / After Evidence** section. This exercises the same hook
called after a new repair draft is created; local tests separately verify that
publication ordering.

Baseline: `208ff807ccdcba0532648b84f2064a3c9a663e51`.
Candidate: `e904068189798df4207a2b9b6b3fafaf7970a52c`.
Both used SDK/tools 1.58.2, `openai/glm-5.3-flash`, high reasoning effort,
30 steps, 4096 output tokens and a 180-second agent-loop limit. Each version
executed once on each frozen case, without retries.

| Frozen scenario | Baseline | Candidate |
|---|---|---|
| Normal review | Review accepted; full completion contract failed | Passed |
| Known arithmetic bug | Inconclusive: unsupported fixture read | Passed |
| Head moved after request | Inconclusive: unsupported fixture read | Passed without inference via controller precheck |

The overall verdict is **inconclusive**. Candidate 3/3 is useful development
evidence, but broken baseline controls and fixture coverage gaps prevent a claim
of improvement. These are synthetic reviewer scenarios, not exact historical
production replays or a general review-quality benchmark.

The six executions cost **$0.02915269 in SDK estimates**, not billed cost.
Their prompts, events, API records, manifests and scores remain private on the VM
under `automation-comparative-evidence-20261028/263c37825a55`. No real GitHub
review was posted. The scheduled worker and its production ledger were unchanged;
the comparison source worktrees were removed.

The first launcher attempt stopped before inference because the VM's old `gh`
does not support `baseRefOid`. The corrected launcher used the repair's pinned
original baseline. Description publication then failed through `gh pr edit`;
the REST body update succeeded and is now used by the publisher.

The original execution report was retained. A separate rescoring receipt records
the stricter requirement that controls pass on both versions. The description was
rendered from those retained results with expected behavior, per-run outcomes,
write/review counts, resource measurements, exact revisions and evidence hashes.
Rescoring and publication retries performed **zero agent reexecutions**. The PR
remained a draft at its tested head.

Validation covered 162 tests across the worker/comparison suites, plus Ruff,
compilation and whitespace checks. The hook tests exercise a real subprocess,
credential filtering, timeouts, immutable request/revision matching, receipt reuse,
failed-run retention, scoped REST updates and rejection of stale or foreign PRs.
