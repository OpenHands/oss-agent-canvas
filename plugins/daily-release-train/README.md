# daily-release-train

Draft plugin-spec automation for a daily 03:00 AM ET (America/New_York)
release train across the four OpenHands repos that make up an OpenHands
(OpenHands/OpenHands, Agent Canvas) release, in dependency order:

1. **OpenHands/software-agent-sdk** — no release-please. `prepare-release.yml`
   (workflow_dispatch) opens a `rel-X.Y.Z` PR; merging it makes
   `create-release.yml` tag `vX.Y.Z` and dispatch the PyPI / npm / image /
   binary publishers, which then auto-open `bump-sdk-X.Y.Z` PRs in
   `automation` and `OpenHands`.
2. **OpenHands/automation** — release-please standing release PR; merging it
   tags and publishes `openhands-automation` to PyPI.
3. **OpenHands/extensions** — release-please standing release PR; merging it
   publishes `openhands-extensions` (PyPI) and `@openhands/extensions` (npm).
4. **OpenHands/OpenHands** — release-please standing release PR (created as a
   draft). Its `config/defaults.json` pins `versions.agentServer`,
   `versions.automation`, and `versions.agentCanvas`, guarded by
   `sdk-version-sync.yml`, so the pending SDK/automation bump PRs are merged
   before the release PR.

After the train, the agent posts a single summary to Slack: what version of
each library shipped (with release links), and for any library that could not
be released, the blockers (failing checks, conflicts, failed publish
workflows, missing registry artifacts, or `needs-*-release` dependencies).

## Status

Draft. The plugin is inert until a plugin-spec automation is created from it,
and the config defaults to `mode: dry_run`: it inspects everything, performs
no GitHub mutations, and posts the summary bannered as a dry run. A human
must review this plugin and flip `mode` to `live` before any real release is
attempted.

Slack destination: there is no channel literally named `#agent-canvas`; the
Agent Canvas channel is `#proj-agent-canvas` (`C06QT0AGY4W`, OpenHands
workspace), the same channel `release-ready.yml` uses. Override via
`slack_channel_id` / `slack_channel_name` config.
