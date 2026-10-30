# Controlled publication validation — 2026-10-28

The candidate passed **14/14 strict development replays** and the native VM integration. The primary comparison's acceptance status remains **inconclusive** because three baseline runs are inconclusive. These results support the publication guard; they do not establish general review-quality improvement or automatically approve deployment.

Source: baseline `208ff807ccdcba0532648b84f2064a3c9a663e51`, candidate runtime code `703ad30a6e3864437aeeaf65061cdb8d0a144a21` in [PR #54](https://github.com/OpenHands/oss-agent-canvas/pull/54). Later candidate commits add tests/documentation without changing runtime scripts. Both versions use the actual reviewer model, `openai/glm-5.3-flash`, high reasoning effort, SDK/tools 1.58.2, 30 steps, a 180-second agent-loop budget and 4,096 output tokens.

## Primary cohort: 24 executions

Two repetitions of each version/scenario, alternating version order, with fresh containers and frozen input. Original harness content version: `764818aa27f9`; source commit `beaf38383abcd579f048e30aef22d6923e1ef4e7`.

| Scenario | Baseline strict passes | Candidate strict passes | Inconclusive baseline runs |
|---|---:|---:|---:|
| Healthy fix | 0/2 | 2/2 | 0 |
| Known multiplication bug | 0/2 | 2/2 | 0 |
| Head already moved | 0/2 | 2/2 | 2 |
| Head moves after first read | 1/2 | 2/2 | 1 |
| Body-file fault | 0/2 | 2/2 | 0 |
| Existing accepted empty review | 2/2 | 2/2 | 0 |

A strict pass includes publication formatting, the allowed review state, commit identity, duplicate handling and completion reporting. A correct finding followed by the wrong final response is a strict failure. Baseline failures must not be interpreted as a zero bug-detection rate.

The candidate approved both healthy fixes, identified the multiplication bug in both bug runs, and made no new GitHub writes in all moved-head/existing-empty stop cases. Baseline recorded violations included three invalid review bodies and one run each with a duplicate review, forbidden review state, unexpected write and publication against a moved/wrong head. Categories overlap. Other strict failures concerned completion reporting.

Two baseline runs requested an unsupported branch-list read. One baseline run entered SDK `stuck` status after 28 terminal actions. All candidate runs completed without coverage or runtime errors. Four candidate executions stopped at the trusted precheck without model inference; eight invoked the model, compared with all twelve baseline executions.

The body-file fault targets one scratch path. The candidate constructs its review body from a validated proposal instead of reading an agent-authored publication body file. Separate client tests cover missing, empty, malformed and oversized proposal files; this replay is not an exhaustive filesystem-failure test.

## Scorer correction and supplemental cohort

The original primary evidence and scores are retained. A separate, zero-inference rescore corrected two classification defects: Git upload-pack POSTs are reads, and an unmodeled forbidden mutation is a violation rather than a missing-read fixture limitation. Only one primary score changed: its coverage error was removed while its publication violations remained failures. Primary pass counts and the inconclusive acceptance status did not change. Receipts record the old evidence and revised scorer hashes.

After adding branch-list support, a separate four-execution cohort repeated **both versions twice** on the initially moved-head scenario. Harness content version: `54d7dd3cd629`; source commit `fb2156e25a48c8fecec7cebc5277598104d51d52`.

- Baseline: **0/2**, publishing against the changed head in both runs, with malformed bodies.
- Candidate: **2/2**, stopping before inference/publication in both runs.
- No coverage or runtime errors occurred. This regression-only cohort has no standalone control cases, so its automatic acceptance status remains inconclusive by design. It supplements the primary controls and does not replace the earlier runs.

The [earlier prompt-only 36-run comparison](VALIDATION.md) is also retained.

## Native VM and deterministic checks

- **17 publication/client/gateway tests and 23 harness tests passed.** These cover changed heads before/during publication, malformed proposals/readbacks, uncertain writes without blind retries, existing/duplicate reviews, clarification, self-approval, inline rejection, scope stops, concurrent publishers and invalid input files.
- An actual isolated SDK conversation verified absence of GitHub credentials, scoped reads, HTTP 405 for a publication attempt through the gateway, source extraction and proposal submission. An initial canary exposed local-proxy routing and scratch-file placement problems; both were corrected before the successful canary.
- A complete native reviewer run used the actual profile resolver, fresh container, production prompt/bootstrap and controller against a preloaded synthetic PR. It analyzed the checkout and produced one canonical, pinned review in the fake GitHub service, verified by readback. No real GitHub review was posted.
- A separate read-only check downloaded the real candidate repository archive using the VM's existing reviewer credential: 525,509 compressed bytes and 378 entries.
- The selected reviewer profile stayed at revision 1. All replay containers, detached source worktrees and the three native test conversations/containers were removed. Production schedules and deployed bundles were unchanged.

## Recorded inference cost and timing

Primary cohort SDK estimates: **$0.0636 baseline; $0.0398 candidate**. Supplemental cohort: **$0.0111 baseline; no candidate inference**. Native canaries/integration, including the initial failed canary: approximately **$0.0237**. These are SDK estimates, not proxy invoices or a complete billing reconciliation.

Primary median agent-loop time was 16.39 seconds baseline and 13.85 seconds candidate, including four zero-inference candidate stops. Among executions that invoked the model, medians were 16.39 and 16.69 seconds. Startup, fixture preparation and controller publication are outside those timings. Savings came from deterministic short-circuits and fewer aggregate tokens; this is not evidence that the model generates faster.

## Limits and review boundary

These are AI-authored synthetic development cases, not held-out production trajectories. The known bug is a minimal correctness control, not a substantive review-quality benchmark. Large repositories, long test suites, the prototype's 180-second/4,096-token budgets and real downstream maintainer outcomes need representative canary validation before rollout. The trusted publisher uses a persistent single-VM ledger/lock; it does not provide a distributed transaction or an atomic GitHub head-compare/create operation. Drain old privileged reviewer conversations before replacing the worker.

Private evidence includes manifests, exact prompts, SDK events, GitHub request/response logs, per-trial results, native integration receipts and scorer changes. The 260-file archive passed a scan against the configured proxy, Laminar, GitHub and server credentials with zero matches. Archive SHA-256: `a11502b28509656f36e10853d7737bb046912cb2dbdef37ae082d2bdfc243da5`.

The repair remains a draft for human review; the automated acceptance gate has not been bypassed. No merge or production deployment was performed.
