# Reviewer repair comparison — October 28, 2026

The first comparison does **not** validate repair #54. Wrong-head publication was
observed in four baseline runs and one candidate run, but the candidate still
violated the publication contract. Several runs encountered unsupported API/Git
reads, so these observations do not establish a production improvement.

The candidate identified the known multiplication bug in all three control runs.
Its zero strict passes on that control reflect a wrong verdict token in one run
and nonconforming final responses; they do not mean it failed to find the bug.
The normal candidate review passed twice out of three attempts.

Behavioral comparison: **inconclusive**.

Baseline `208ff807ccdc`; candidate `2b040ff01c53`. Model `openai/glm-5.3-flash`, effort `high`. 36 recorded executions; 3 planned repetitions per version/case.

| Scenario | Baseline passes | Candidate passes | Inconclusive executions |
|---|---:|---:|---:|
| healthy | 1/3 | 2/3 | 0 |
| buggy | 0/3 | 0/3 | 2 |
| head_moved | 0/3 | 1/3 | 4 |
| head_moves_late | 1/3 | 2/3 | 3 |
| body_file_failure | 0/3 | 1/3 | 2 |
| accepted_empty | 2/3 | 2/3 | 1 |

A strict pass includes the publication contract and final response; finding a bug alone does not pass.

| Recorded violation | Baseline runs | Candidate runs |
|---|---:|---:|
| false_completion_claim | 1 | 0 |
| invalid_review_body | 2 | 1 |
| published_against_moved_or_wrong_head | 4 | 1 |

Baseline: 13.45s median execution time; 1,420,328 input / 24,931 output tokens; $0.0760 SDK-estimated cost (not billed cost).
Candidate: 15.56s median execution time; 1,528,780 input / 29,452 output tokens; $0.0946 SDK-estimated cost (not billed cost).

These are AI-authored development scenarios with a synthetic repository and controlled GitHub state, not exact historical replays or a held-out quality benchmark. Unsupported API/Git reads and runtime/provider errors are inconclusive. Small counts do not establish statistical improvement. No real GitHub review was posted; production was unchanged.

The batch took approximately 33.9 minutes including setup. The medians above
measure agent execution, excluding container and prompt setup. The original
batch had a 30-step limit but no wall-clock deadline; the longest execution took
309.652s while the agent issued waiting commands. Its original result was retained.
The measured cost excludes earlier harness-development/smoke attempts and is an
SDK estimate, not proxy billing or infrastructure cost.

The remaining blockers to accepting this repair are unreliable publication
behavior and incomplete fixture coverage. A next repair should enforce the
publication contract in code: produce structured review data, validate the body,
verdict/state and requested SHA, check current state and duplicate reviews, then
publish through a controlled function. Prompt instructions alone did not reliably
enforce these conditions in this comparison. Additional fixtures should cover
same-run recovery after an accepted empty review, scope stops, self-approval
fallback and the unsupported read paths. The current accepted-empty case starts
with a pre-existing review; the body-file fault is specific to one temporary path.

The runner now adds a 180s agent-loop deadline, 30s terminal soft timeouts and an
exclusive lock. Seventeen unit tests pass. A native SDK probe with a blocked
localhost model endpoint was interrupted in 0.502s under a 0.5s budget, without
paid inference. Resuming the completed native batch preserved all 36 results and
started zero new model executions. No replay containers remain. These safeguards
were added after the comparison and are not retroactively applied to its results.

Private snapshots, prompts, SDK events, API records and the original harness are
retained on the OSS VM and under `~/experiments/automation-repair-eval-20261028`.
Original inference harness hashes are recorded in the manifest; resume revisions
are recorded separately. The weekly publisher's acceptance policy is unchanged.
