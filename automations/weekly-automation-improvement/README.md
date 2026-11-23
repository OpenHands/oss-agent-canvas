# Automation improvement discovery

The discovery workflow consolidates persisted Laminar findings into GitHub topic
issues. The repair workflow reads GitHub for scheduling. Both use native OpenHands
agents and share configuration, private evidence storage and a worker lock.

See [the high-level diagrams](HOW_IT_WORKS.md) for both workflows.

## Discover and prioritize

1. Read existing Flow-1 events and clusters from Laminar. Retain raw references and
   evidence with ambiguous attribution.
2. Use the native triage agent, Luna by default, to group findings by mechanism
   across automations and repositories and assign P0–P3.
3. Publish an actionable topic in the configured home repository with
   `automation-improvement`, `improvement-topic`, `improvement-ready` and one
   `priority:P0` through `priority:P3` label. The topic marker carries its stable
   identity. The weekly summary provides context.
4. Match the underlying mechanism to existing issues and PRs. Update a matching
   topic issue's private evidence index and post an idempotent recurrence comment.
   Preserve human priority and lifecycle labels. Matching human work and prior
   repair PRs receive recurrence evidence without opening competing issues.

The workflow writes `triage-report.json` with the publication results and budget.
Each topic issue points to a private, immutable evidence index on the authorized
worker. Retain `state_dir` across runs and worker replacements. The index retains
the original Signal records, inventory and previous evidence references. Public
briefs explain the problem, example, prevention and validation plan before the
opaque evidence reference.

## Configure and run

Copy [weekly.example.json](weekly.example.json) outside Git. Configure the Laminar
project and Signal, proxy and native automation endpoints, existing checkouts,
GitHub secret name, private state and owner-only credentials file. The worker
needs permission to create repository labels during first publication. The saved
agent profiles and worker credentials must match the intended repair scope.
`repair_scope=any_authorized` is the default. `customer_stack` limits repairs to
the home repository and explicit repository entries.

```bash
.venv/bin/python triage_workflow.py --config /private/config.json
.venv/bin/python triage_workflow.py --config /private/config.json --input /private/inventory.json
.venv/bin/python triage_workflow.py --config /private/config.json --dry-run
```

A dry run writes a private receipt and may spend triage inference. GitHub and the
canonical event state remain unchanged. Discovery errors stop publication.
Failed publications and recurrence comments remain pending for reconciliation.
