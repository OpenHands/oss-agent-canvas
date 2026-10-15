## OSS-agent-canvas

The repo contains the automations of the oss-agent-canvas.

See https://oss-agent-canvas.ngrok.dev

## Layout

- `automations/` — git-synced automations, one directory per slug, each with an
  `automation.yaml` and the `tarball/` that gets uploaded on every run.
- `plugins/` — plugin-backed automations. Each directory is a self-contained
  plugin package: a `.plugin/plugin.json` manifest whose `automation` block
  carries the entrypoint, agent profile requirements, config schema, and
  suggested trigger, plus the scripts the entrypoint runs. See
  `plugins/issue-triage/README.md`.
