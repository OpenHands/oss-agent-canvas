# Automation improvement — discover and prioritize

Turn persisted Laminar findings into a prioritized topic queue and readable weekly
issue. This is the first of two workflows. The stacked repair workflow consumes
the private handoff and owns fixes, PRs and independent validation.

See [the high-level diagrams](HOW_IT_WORKS.md) for the full system.

## Workflow

1. Read existing Flow-1 events/clusters directly from self-hosted Laminar. Do not
   rerun Flow-1. Preserve ambiguous findings, raw-data references and backlog state.
2. Prepare compact evidence and inspect existing issues/PRs.
3. Run the existing native OpenHands triage agent (default Luna) to consolidate
   mechanisms and rank topics P0–P3.
4. Add idempotent recurrence comments to matching existing work and exclude it
   before allocating repair slots. Preserve existing descriptions and private data.
5. Save the ordered queue and evidence/configuration references in an owner-only
   `repair-request.json`. Do not dispatch repair agents in this workflow.

Topic-specific repair issues are still created by repair agents in workflow two;
triage creates the same weekly issue and recurrence comments as before.

## Configure and run

Copy [weekly.example.json](weekly.example.json) outside Git. Configure the Laminar
project/Signal, proxy and native automation endpoints, canonical repository
locations, GitHub secret name, private state and owner-only credentials file.
Existing runtime/setup requirements and agent-profile settings are unchanged.
`repair_scope=any_authorized` remains the default; choose `customer_stack` to limit
future repair destinations to the home repository and explicit repository entries.
GitHub credentials/profile/MCP permissions must match the intended write scope.

```bash
.venv/bin/python triage_workflow.py --config /private/config.json
.venv/bin/python triage_workflow.py --config /private/config.json --input /private/inventory.json
.venv/bin/python triage_workflow.py --config /private/config.json --dry-run
```

The normal result names the private `repair_request` path. `triage-report.json`
contains the preparation outcome. A dry-run publishes no issues/comments, writes
no dispatch ledger and produces no repair handoff. Failed discovery does not
produce a repair handoff. Pending recurrence comments stay pending.

The handoff retains queue order, source revisions, configuration, original evidence
and the budget snapshot. Shared code covers Laminar/GitHub access, evidence indexes,
privacy, scope, validation, locking and accounting. The second workflow uses the
same state and lock; splitting execution does not grant a new budget.

The weekly cron is unchanged. The stacked second PR restores `weekly.py` as the
thin entrypoint that chains both workflows under one lock and budget.
