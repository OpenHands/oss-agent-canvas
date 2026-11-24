# Assignment and review validation

On November 24, 2026, the Factory dispatcher, OSS issue resolver and shared PR reviewer processed the same issue through their normal handlers against isolated GitHub and coordination services. The original issue asked for a specific behavior: trim surrounding whitespace from a name so that `greet(" Ada ")` returns `"Hello Ada"`. This run used a temporary Git repository and controlled coding and review model responses.

| Step | Evidence observed | Who acts next |
| --- | --- | --- |
| Trusted enrollment | The dispatcher assigned the existing issue. The resolver received the original acceptance criterion and opened one draft PR. | The executor implements the change. Once the draft exists, CI runs. |
| Behavior check | Running `python check.py` on the base exited with code 1. The same command on the candidate exited with code 0 and printed `PASS: greeting trims surrounding whitespace`. | The dispatcher requests the configured reviewer. |
| Independent review | The reviewer read a separate checkout, ran the check and submitted an approval for `a95119c4e1ff968ae8c5fc3636d380d80671a51a`. Its review commit ID matched the PR head. | The maintainer inspects the evidence and decides whether to mark the draft ready. |
| Redelivery | Repeated delivery produced one coding conversation, one draft PR, one reviewer request, one submitted review and one current status comment. The original issue body was preserved. | The maintainer retains the promotion and merge decision. |

The [machine-readable run receipt](validation/assignment-roundtrip.json) contains the acceptance criterion, observed status transitions, regression output, submitted review commit and write counts. Its sample GitHub URLs refer to the isolated service. The final record identified the resolver as owner, the maintainer as next actor, and the PR as a draft. Its `head` and `reviewed_head` matched the approval. The approval above belongs to the isolated example. The public implementation has its own review history and GitHub CI. [Implementation PR](https://github.com/OpenHands/oss-agent-canvas/pull/170)

## Did the issue follow the PR when its revision changed?

The [portable topic](https://github.com/OpenHands/oss-agent-canvas/issues/168) asks whether the issue still tells the truth after promotion or a changed revision. A separate run exercised the native repair entrypoint with controlled GitHub and agent responses. These values were read back from the issue body and PR after each invocation.

| Invocation | Repairs started | PR and issue readback |
| --- | ---: | --- |
| Independent acceptance | 1 | The PR was ready. The issue reported `accepted`, named the maintainer and linked checks for the reviewed head. |
| Repeat on the same head | 0 | The issue and PR still agreed on the ready, reviewed revision. |
| Replace the head | 0 | The PR returned to draft. The issue reported `revision_changed`, retained the previously reviewed SHA, linked checks for the new SHA and requested fresh review. The workflow callback reported failure. |
| Repeat after the change | 0 | The issue kept the revision-change notice. The repeat delivered a completed callback. |

For the discovery inputs, live issue outcomes, installation scope and repeat instructions, see the [backlog validation report](BACKLOG_VALIDATION.md). The repository also contains the [dispatcher CLI scenarios](tests/test_factory_cli.py) and [public status reader tests](tests/test_public_status.py).
