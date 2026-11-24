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

## Portable discovery and executor handoff, November 24

The follow-up to [#158](https://github.com/OpenHands/oss-agent-canvas/issues/158) used isolated worker state and a write-scoped GitHub relay for discovery. The initial model run produced an empty queue because it could not read the repository guide from a bare checkout. The ranker now reads the guide at the prepared Git revision. A separate failed relay preflight stopped publication. The operator restored the relay and reran discovery.

| Path | Starting evidence and observations | Result visible to a maintainer |
| --- | --- | --- |
| Laminar | A fresh four-second collection window returned the same Flow-1 finding as the frozen input. The finding-scoped reader retrieved its 3,396-character payload in private. Native Luna ranked the frozen observation P2. | The [weekly plan](https://github.com/OpenHands/oss-agent-canvas/issues/165) and [epic](https://github.com/OpenHands/oss-agent-canvas/issues/163) link the [permission-preflight topic](https://github.com/OpenHands/oss-agent-canvas/issues/166). The customer executor claimed it once, then [reported the unverified permission preflight as blocked](https://github.com/OpenHands/oss-agent-canvas/issues/166#issuecomment-7132120384). |
| No Laminar | The collector compared the draft claim on [#149](https://github.com/OpenHands/oss-agent-canvas/issues/149) with [#157](https://github.com/OpenHands/oss-agent-canvas/pull/157). A second observation came from a controlled empty-report fixture. The worker had a model credential and no Laminar credential or project configuration. | The [plan](https://github.com/OpenHands/oss-agent-canvas/issues/167) and [epic](https://github.com/OpenHands/oss-agent-canvas/issues/162) list the [observed status discrepancy, P2](https://github.com/OpenHands/oss-agent-canvas/issues/168) and the [fixture, P3](https://github.com/OpenHands/oss-agent-canvas/issues/169). The P3 rationale identifies the limit on production impact. The [P2 execution record](https://github.com/OpenHands/oss-agent-canvas/issues/168#issuecomment-7132356021) links the draft implementation, its current revision, test limitations and next actor. |
| Software Factory | The real dispatcher CLI ran against isolated GitHub and Linear services for both routes, beginning with an existing human-written issue. The checks failed on one head, succeeded on its replacement, and an approval for the old head arrived before approval for the new head. | Each route produced one status comment and one reviewer request through retries. The issue reported the accepted SHA, first as draft and then as ready following promotion. No private ticket link appeared in the comment. The shared deployment and its real reviewer were outside this run. |

Repeating both live discovery inputs returned `no_unhandled_findings`. The epics still contained one Laminar issue and two portable issues. The three validation topics' managed sections were refreshed to the final external-status wording and verified against GitHub. All three link back to the originating weekly summary. A new commit on the real draft implementation changed the P2 execution record from revised to incomplete. The same status comment exposed the changed SHA and requested fresh validation.

### How to repeat the checks

1. **Laminar.** Run discovery against a bounded Signal window, retain the frozen inventory and compare an authorized `read_signal` with its selected event. Include an ambiguous owner in a controlled input. Verify the number of distinct origins and the attribution warning. Repeat an unchanged input, then add a matching new occurrence. Check the existing topic, its recurrence comment, the weekly backlinks, and the durable publication receipt after an interrupted write. Preserve any priority or hold applied by a maintainer.
2. **Customer.** Remove Laminar settings and use a model-only credential. Submit an observation with a stable occurrence ID, inspect its public rationale, and replay the same batch. Separately enroll an existing human issue with the customer label. Invoke claim twice with one attempt ID, run the customer implementation once, then report and reconcile. Compare the public links and SHAs with GitHub. Change a reported head and confirm the issue requests revalidation. Preserve the issue author's text and unrelated labels.
3. **Factory.** Run the configured CLI for an imported issue, first with discovery readiness alone and then with trusted Factory enrollment. Verify one assignment or one ticket, one implementation start, a draft with a canonical issue backlink, an explicit independent reviewer request, and the resulting status on the original issue. Repeat after losing a write acknowledgement. Exercise a failed check, stale approval, changed head and competing PR before inspecting the final status and next actor.

A separate full-loop exercise used a single isolated GitHub service. The OSS resolver published a draft against a real Git commit. A regression check failed on its base and passed on the change. The shared reviewer posted its approval for that commit, and the dispatcher reflected the SHA, draft state and next actor on the original human-authored issue. Duplicate deliveries produced one coding conversation, one PR, one reviewer request and one review. Both model responses were controlled.

The real Git-sync importer also loaded each generated package into local storage as an inactive automation. Import and archive readback ran offline. The install exercise verified the entrypoints, schedules, timeout and setup scripts. Tenant secret binding and delivery from installed GitHub or Linear integrations remain deployment checks.

The native controller ran separately through its ordinary subprocess entrypoint with controlled agent and GitHub responses. Its first run accepted the reviewed change, a repeat started zero repairs, a changed head returned the PR to draft and emitted a failed callback, and another replay started zero repairs. The CI verifier also read the actual heads and checks on [#156](https://github.com/OpenHands/oss-agent-canvas/pull/156) and [#157](https://github.com/OpenHands/oss-agent-canvas/pull/157). These exercises cover controller decisions. They use the existing live repair PRs as references for coding outcomes.
