# GitHub backlog validation, 2026-11-23

The isolated worker used a seeded sample of eight saved real Laminar findings.
Each standalone repair run had a one-topic cap, concurrency one, up to two
finding-linked inputs and two completed no-finding controls, and up to two
repair attempts. Each invocation had a $20 inference guard and a one-hour
duration guard. The validation used `customer_stack` scope in this repository.
The scheduled configuration kept its existing topic, concurrency and cohort
limits.

## Discovery and claims

Ready topics have one P0–P3 label and no active or blocked marker. Selection
uses current GitHub priority, then issue age. Claims consume readiness under
the worker lock. Open repair PRs prevent dispatch. A new attempt requires an
explicit ready label after the prior claim and open work are reconciled.

Two native Luna 6 runs published six topic issues. GitHub readback verified
all seven public brief sections and a private evidence reference on every issue.
The saved Laminar credential returned HTTP 401, so subsequent live evidence
reads used the active worker credential.

- Weekly summaries [#148](https://github.com/OpenHands/oss-agent-canvas/issues/148) and [#150](https://github.com/OpenHands/oss-agent-canvas/issues/150).
- Topic issues [#149](https://github.com/OpenHands/oss-agent-canvas/issues/149), [#151](https://github.com/OpenHands/oss-agent-canvas/issues/151), [#152](https://github.com/OpenHands/oss-agent-canvas/issues/152), [#153](https://github.com/OpenHands/oss-agent-canvas/issues/153), [#154](https://github.com/OpenHands/oss-agent-canvas/issues/154), and [#155](https://github.com/OpenHands/oss-agent-canvas/issues/155).

The standalone repair preview first selected the oldest P1, #149. Changing
#154 from P2 to P0 moved it ahead of all P1 topics. An actual dispatch consumed
its ready label and added the in-progress label. The active-run readback
excluded #154, and an overlapping repair invocation returned `skipped_overlap`.
The worker opened the private issue index and retrieved its original Signal
through Laminar. The validation state contained zero private queue files.

A separate native Luna 6 preview used one finding and the final issue-guide
code. It recorded two repository guide reads against the prepared source and left
GitHub and canonical event state unchanged. Unit tests cover additional issue-template fields, repository
labels and draft handling for failed, pending, unavailable or stale CI.

## Native repair and review

The first repair opened [draft PR #156](https://github.com/OpenHands/oss-agent-canvas/pull/156)
from [issue #154](https://github.com/OpenHands/oss-agent-canvas/issues/154).
It proposed reporting changes for managed acceptance criteria while retaining
author criteria. The PR carried `automation-improvement` and **Before / After
Evidence**. Its captured-input reporting replays passed on both versions for
one negative and two controls, so the PR identified them as compatibility
evidence. It recorded 18 passing focused tests and a shared unrelated full-suite
failure. The new CI verifier independently read the exact PR head and found
successful GitHub CI. [CI run](https://github.com/OpenHands/oss-agent-canvas/actions/runs/45217954339/job/137844111748)

A separate native review verified the merge-preview tests and requested a
revision for a newline-only classification error and a replay harness that
supplied the candidate verifier to both versions. It kept the PR draft.
[Independent review](https://github.com/OpenHands/oss-agent-canvas/pull/156)

This run started before the default moved from Sol 6.1 to Sol 6.2. The
revision resumed the original repair conversation and was charged only its
incremental spend. The provider returned an internal error before an updated
head was published. A second, separate reviewer kept the same head draft.
The worker released the claim and left the issue unqueued. Reported repair
and review inference for the run was **$4.0768**.

The follow-up ran Sol 6.2 and selected the previously queued P1 [#149](https://github.com/OpenHands/oss-agent-canvas/issues/149).
The completed P0 was bypassed. The repair opened [PR #157](https://github.com/OpenHands/oss-agent-canvas/pull/157)
with `automation-improvement` and a **Before / After Evidence** section.
In the controlled publication replay, two captured empty approvals produced
zero submissions under the candidate. Both sampled controls still produced
one valid approval each. The replay used the native conversation lifecycle,
a stub response model and a local GitHub recorder. The repair recorded 144
passing unit tests and GitHub CI passed on the published head.
[CI run](https://github.com/OpenHands/oss-agent-canvas/actions/runs/45227523598/job/137877901334)

The Sol 6.2 reviewer reproduced the publication result and identified an
event-only docs routing regression relative to the configured plugin pin.
The revision reused the repair conversation and restored that routing.
A fresh Sol 6.2 review accepted the revised head after the repository's 149
tests passed. [Revised-head CI](https://github.com/OpenHands/oss-agent-canvas/actions/runs/45230887528/job/137889792428)

The controller initially held the accepted change draft because the worker's
GitHub CLI 2.23 lacks the `baseRefOid` view field. The compatibility fix
reads base and head through REST and checks the same head's CI rollup.
A readiness-only rerun on the worker used the existing reviewer verdict,
matched the exact head, and made #157 ready. It spent no additional inference
and dispatched no repair.

The discovery rerun found zero unhandled findings and created zero duplicates.
The repair preview skipped both attempted topics and selected the next ready
P1, #151. Four validation topics remain ready. Reported orchestration inference
was **$11.1314** across Luna discovery and preview ($0.0238), the first repair
run ($4.0768), and the Sol 6.2 run ($7.0307). These are SDK-reported costs for
the controller's agents. Replays launched by the agents have separate costs.

## Maintainer review of the initial issues

I reviewed the six issue creation payloads before reading the repair agents'
edits. All six explain the expected and observed behavior, proposed prevention,
and a validation check before the private reference. A maintainer can understand
each failure from GitHub. The quality varies when deciding whether the
investigation is ready to assign.

| Issue | GitHub-only assessment |
| --- | --- |
| [#149](https://github.com/OpenHands/oss-agent-canvas/issues/149) | Strongest brief. Two concrete examples show why an empty approval is harmful. The invalid-input and exactly-once valid-input checks are actionable. |
| [#151](https://github.com/OpenHands/oss-agent-canvas/issues/151) | Clear symptom and impact. Retry, fallback and resumption have different owners and tradeoffs. The brief leaves the affected layer and the preferred recovery path open. |
| [#152](https://github.com/OpenHands/oss-agent-canvas/issues/152) | Clear observed failure. The brief separates a permission remedy from preflight behavior, but the latter can be investigated before changing a credential. It overlaps with #155. |
| [#153](https://github.com/OpenHands/oss-agent-canvas/issues/153) | Concrete contradiction. Comparing the recorded test results, published review and final summary on the same head is a useful acceptance test. |
| [#154](https://github.com/OpenHands/oss-agent-canvas/issues/154) | Clear problem and strong preservation criterion. The prevention section compares two post-write views. Detecting an append versus replacement also requires the original body. |
| [#155](https://github.com/OpenHands/oss-agent-canvas/issues/155) | Concrete explanation of an unsupported permission claim. Its read-back requirement overlaps with #152's success check, although the underlying claim can be false even when access is sufficient. |

The weekly summaries list the topic titles and priorities as plain text.
They omit the actual issue links because the summary block is rendered before
publication. A maintainer has to search GitHub again to find those issues.
The priority labels make sorting easy, but the issue bodies do not explain why
a topic received its specific priority.

The validator checks structure, references and privacy patterns. It does not
verify the accuracy of the prose or whether two differently keyed issues would
need the same repair. The #152/#155 pairing illustrates this limit. Repository
guidance is provided to the agent and extra template fields and labels are
supported. The live home repository had general guidance and no dedicated issue
form, so this run does not establish compliance with a strict issue form.
