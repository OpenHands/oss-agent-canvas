# Evidence-gated publication

The weekly publisher now requires a successful behavioral comparison before
pushing a repair branch or creating its PR. Its initial description includes
**Before / After Evidence** for the exact candidate commit. Inconclusive or failed
comparisons leave the finding pending. A completed behavioral failure can trigger
one Sol revision; both attempts remain recorded. Provider/runtime failures do not
trigger that revision.

The implementation is in [#55](https://github.com/OpenHands/oss-agent-canvas/pull/55),
stacked on [#49](https://github.com/OpenHands/oss-agent-canvas/pull/49).
This report records the verification state on 2026-10-29. **A new live repair PR
with demonstrated improvement has not yet been verified.**

## Local verification

The integration tests execute the real publisher against temporary Git commits
and a trusted comparison subprocess. They verify that an improving candidate
reaches push/PR creation with evidence already present, while failed comparisons
and absent comparison configuration cannot publish. They also verify immutable
receipt reuse, source cleanup, retained candidate bundles, and a bounded revision
whose failure preserves the backlog without leaving a publication reservation.
These tests validate orchestration; their controlled evaluator is not evidence
that a model-generated production repair improves task quality.

The task adapters cover issue-priority decisions, explicit PR-description claims
and reviewer publication. Other mechanisms remain unsupported and cannot produce
new evidence-qualified PRs. The [comparison documentation](README.md) describes
the narrow outcome checks and the limits of reconstructed draft checkpoints.

## Live experiments and remaining verification

The live pilot read the existing Flow-1 finding for issue triage, used Luna to
select a topic, and used Sol to generate a focused prompt edit. Sol proposed an
audit of reported user harm immediately before priority-label writes, with an
explicit distinction between actual harm and speculative technical risk. The
proposal was generated from the captured native input and frozen repository
source; its patch was not edited manually for the publication experiment.

The initial comparison encountered a model-request timeout. Its evidence was
retained. A separately recorded comparison of the **same saved proposal** uses a
120-second model-request timeout instead of 60 seconds; it does not resample the
Signal or regenerate the Luna/Sol proposal. Its publication gate can create a
draft only if the target baseline violation is reproduced, every candidate case
passes, both versions pass the controls, and all executions complete without
errors. This is development-case evidence, not a statistical improvement claim.

Private VM experiment directories:

- Original full pipeline: `automation-evidence-publisher-20261029/cbd351ad62a0`.
- Same proposal with the revised timeout protocol:
  `automation-evidence-publisher-20261029/protocol120-80680c059ba6`.
- Existing repair comparisons: `automation-complete-evidence-20261029/`.

Google Cloud required passkey reauthentication after these detached jobs started.
Both CLI access and the Cloud Console are blocked on that confirmation. Their
final reports and any resulting repair PR must be retrieved and inspected before
claiming live end-to-end success. The old drafts
[#53](https://github.com/OpenHands/oss-agent-canvas/pull/53) and
[#59](https://github.com/OpenHands/oss-agent-canvas/pull/59) have not been promoted
to evidence-qualified repairs. The earlier inconclusive evidence on
[#54](https://github.com/OpenHands/oss-agent-canvas/pull/54) remains retained.

The pilot uses a private copy of the weekly ledger, explicitly scopes its input
to one historical finding, and excludes #53 from that pilot's deduplication view
so a replacement can be evaluated. It checks the production ledger hash. No
scheduled worker, production automation, deployment or Laminar Signal definition
was replaced by this work. A replacement PR must be verified before closing #53.
