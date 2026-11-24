# Connect discovery to the repository’s Software Factory

A Software Factory is the set of automations a team uses to collect findings, create and triage issues, implement changes, review PRs and validate outcomes in its repositories. A customer can keep the stages that already work for them.

The optional `factory_handoff.py` dispatcher connects discovery to two existing implementation routes, GitHub assignment and a labeled Linear ticket. It gives a GitHub improvement issue one implementation route and keeps the issue as the canonical record. It updates one dispatcher-owned status comment and the improvement status labels. An existing issue's description and human comments are preserved. Native repair skips issues labeled `improvement-external`.

## Enroll and run

Use `improvement-external` and `improvement-ready` for an externally owned investigation. When the implementation target, acceptance criteria and validation plan are decided, a trusted enroller must explicitly apply `improvement-factory` to authorize Factory implementation. Existing issues can be enrolled without an automation-specific body or a Laminar reference. The dispatcher verifies the latest `improvement-factory` label event, checks the issue is open and unassigned, and dispatches to the configured repository route. The GitHub credential must authenticate as `dispatcher_login` and have permission to assign, label, comment and request review on the configured repositories, including access to the GitHub Checks and commit status APIs. Provision the improvement labels using the same names as `tarball/backlog.py` before the first run.

Put these options in the weekly worker configuration. Enable the dispatcher after verifying its identity and permissions. The names below are examples.

```json
{
  "state_dir": "/path/to/persistent/improvement-state",
  "factory_handoff": {
    "enabled": true,
    "dispatcher_login": "factory-dispatcher",
    "trusted_enrollers": ["maintainer", "triage-bot"],
    "max_new_per_run": 3,
    "timeout_seconds": 172800,
    "linear_token_env": "LINEAR_API_KEY",
    "repositories": {
      "Example/project": {
        "mode": "assignment",
        "assignee": "all-hands-bot",
        "resolver_author": "factory-writer",
        "reviewer": "all-hands-bot",
        "required_checks": ["test"]
      },
      "Example/service": {
        "mode": "linear",
        "team_key": "OHE",
        "linear_label": "ohe-beta:implement",
        "linear_state": "Todo",
        "resolver_author": "factory-writer",
        "reviewer": "all-hands-bot",
        "required_checks": ["configured-e2e-check"],
        "path_scopes": ["e2e_tests/*", ".github/workflows/*e2e*.yml", ".github/workflows/*e2e*.yaml"]
      }
    }
  }
}
```

Run from the worker that owns `state_dir`, with `gh` authenticated to `github.com` and the Linear key supplied only when a Linear route is configured.

```sh
python3 tarball/factory_handoff.py --config /path/to/weekly.json --dry-run
python3 tarball/factory_handoff.py --config /path/to/weekly.json
```

Add `--issue https://github.com/OWNER/REPO/issues/N` to restrict a sweep to one issue in the configured repository allowlist. Dry runs inspect the issue and its enrollment without writing. Run only one scheduler against this configuration. Multiple processes on the same worker are serialized by the same `weekly.lock` used by native repair. Keep `factory-handoff.json` with the private worker state across restarts, and keep route identities and checks stable during an attempt.

New issues are dispatched in `priority:P0` through `priority:P3` order, then by creation time. Unlabeled priorities come last. The dispatcher preserves priority labels. A maintainer's `improvement-blocked` label pauses the handoff. `improvement-customer` selects the customer executor. A recorded attempt belongs to its executor across label edits and restarts.

## Configure the GitHub assignment route

Install the multi-repo OSS Canvas `github-issue-resolver` plugin version 0.2.0 or a compatible successor. For a dedicated installation, pass a nonempty repository list and the assignee configured on the dispatcher.

```json
{"factory_repositories": ["Example/project"], "factory_assignee": "all-hands-bot"}
```

The plugin reads the validated configuration from `AUTOMATION_CONFIG_PATH`. Set the installed GitHub event filter to match the same repositories and assignee. For this example use the following expression with the `github-events` source, event `*` and `dispatch_run` destination.

```text
repository.full_name == 'Example/project' && ((action == 'assigned' && contains(keys(@), 'issue') && assignee.login == 'all-hands-bot') || starts_with(ref || '', 'refs/heads/openhands/issue-'))
```

Provide a resolver-scoped `GITHUB_TOKEN` with access to this public repository, permission to push a branch and open a draft PR, read issues and timelines, check collaborator membership and post issue status. The token authenticates as `resolver_author`. Set the resolver's `FACTORY_DISPATCHER_GITHUB_LOGIN` secret to the dispatcher account. Use a resolver worker and agent sandbox scoped to its admitted repositories. The runtime needs its normal automation KV store and agent server. It passes the GitHub secret into the coding conversation.

The resolver checks the latest assignment and its dispatcher's issue comment. It records a stable conversation ID before starting work and reads back uncertain starts. The draft uses `openhands/issue-N`. Before activating the dispatcher, check the **installed** resolver's version, entrypoint, repository filter, assignee and writer identity. Verify the reviewer accepts an explicit request on a draft in that repository. Route Factory assignments to one installation. Drain or disable older copies that still accept all assignments to the same account. An empty repository list uses the resolver's built-in repositories and standard behavior.

## Configure the Linear route

Use the updated OHE ticket-to-PR poller for the configured team and label. Verify its installed version handles paginated results, the canonical GitHub issue field and the Factory branch. Complete its initial baseline run before dispatching new tickets. Set the timeout above the poll interval plus expected coding and review time. The checked-in OHE poller runs daily.

The dispatcher needs a Linear token for the chosen team, with permission to read states, labels, dependencies and comments and to create issues. The ticket ID is stable for a canonical GitHub issue and team. Its fixed description contains `Repo: owner/repo` and `GitHub issue: https://github.com/owner/repo/issues/N`. Only enable a route when that team is authorized to see the canonical GitHub reference. The poller checks blocking relations and creates a draft from `openhands/factory-issue-N` in the issue repository. Set `linear_resolver_user_id` to the Linear writer's UUID to recognize its structured blocked and unrepaired comments. The issue shows a generic blocker while a dependency remains open, the ticket is canceled or the trigger label is absent.

Each reviewer installation has a repository allowlist. For a route restricted to E2E changes, every changed path must fall within its configured E2E scope. Keep the route's `path_scopes` consistent with the installed reviewer, including both names of a renamed file. Configure a separate GitHub reviewer principal. Its event trigger or cron reconciliation must pick up requests on the target repository.

## Follow results and recover

The issue's dispatcher-owned comment records the implementation route, stage, next actor and verified PR, including its current head and draft state. GitHub labels expose active work and terminal outcomes. On a green run every configured check has reported success for that head. A missing, skipped or neutral result waits for evidence. The reviewer request is explicit. A formal approval covers the exact commit. The maintainer evaluates the issue's behavior evidence and decides whether to promote or merge. A new head needs fresh checks and review. A material change request or an inconclusive verdict from the shared reviewer appears on the original issue.

Both bundled implementation adapters create one PR per issue. The dispatcher checks the resolver branch and same-repository closing PR references on every sweep. If it finds competing PRs it links them and leaves reconciliation to a maintainer. Use the customer executor contract for a planned multi-PR repair. A resolver that finishes without a patch can report `unrepaired`.

A failed write or readback returns `readback_or_write_failed` and a nonzero CLI exit. Rerun with the same persistent state to reconcile the canonical comment, labels, assignment, ticket and reviewer request. A timed-out reviewer request blocks the issue. Inspect the current PR before requesting review manually. When a hold is resolved, the next sweep reads the current checks, review and PR state. Keep work with the recorded owner until the associated conversation, ticket and open PRs have been reconciled. A configuration change during a recorded attempt requires the operator to restore the recorded route or perform an explicit ownership transfer.
