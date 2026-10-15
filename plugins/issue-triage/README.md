# Issue Triage Automation Plugin

Plugin-backed automation for triaging OpenHands GitHub issues. It is the
plugin-package form of the `issue-triage-multi-repo-openhands-software-agent-sdk`
automation: the same worker, the same agent profile, and the same trigger, but
the automation metadata lives in the plugin manifest instead of in a
`automation.yaml` + uploaded tarball pair.

## Layout

```
.plugin/plugin.json   # plugin manifest + embedded `automation` block
.claude-plugin        # symlink to .plugin, for other plugin hosts
.codex-plugin         # symlink to .plugin
scripts/worker.py     # entrypoint (runner=script)
scripts/github_client.py
scripts/agent_conversation.py
```

## Automation metadata

The `automation` key in `.plugin/plugin.json` is the plugin-spec manifest the
automation service reads:

```json
"automation": {
  "schema_version": "openhands.automation.plugin.v1",
  "entrypoint": {
    "runner": "script",
    "command": "python3 worker.py",
    "working_dir": "scripts"
  },
  "runtime": { "agent_profile": { "required": true, "default_name": "factory-triage", "..." : "..." } },
  "suggested_trigger": { "type": "event", "source": "github-events", "on": "*" }
}
```

| Field | Value | Why |
|---|---|---|
| `entrypoint.runner` | `script` | The worker drives the SDK itself, so it needs a real entrypoint rather than the stock conversation runner. |
| `entrypoint.working_dir` | `scripts` | The worker imports its siblings (`github_client`, `agent_conversation`), which sit next to it. |
| `runtime.agent_profile.required` | `true` | The worker reads `AUTOMATION_AGENT_PROFILE_ID` and dispatches into that profile, so the automation cannot run without one. |
| `runtime.agent_profile.default_name` | `factory-triage` | Resolved to a profile id at creation; the profile owns the model. |
| `runtime.agent_profile.allowed_tools` | `terminal`, `file_editor`, `task_tracker`, `browser_tool_set` | The profile must enable all and only these. They are exactly what `factory-triage` enables. |
| `runtime.agent_profile.allowed_secret_refs` | `GITHUB_TOKEN_ISSUE_TRIAGE` | The profile must expose all and only this secret; the worker loads it by name. |
| `suggested_trigger` | `github-events` issue events | Used when the automation is created without an explicit trigger. |
| `config_schema` | `repos`, `github_token_secret`, `base_branch` | Install-time knobs, validated and defaulted by the service before the run. |

`config_schema` carries the defaults the worker used to keep in a checked-in
`config.json`, so the manifest is the single source of truth. At run time the
service writes the validated config to `AUTOMATION_CONFIG_PATH`; the worker
reads that first and falls back to the manifest's defaults when the scripts are
run directly.

## Creating the automation

```bash
curl -X POST "${OPENHANDS_HOST}/api/automation/v1/plugin-spec" \
  -H "Authorization: Bearer ${OPENHANDS_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Issue Triage (multi-repo): extensions, OpenHands, software-agent-sdk, automation, enterprise",
    "plugin": {"source": "github:OpenHands/oss-agent-canvas", "repo_path": "plugins/issue-triage", "ref": "main"},
    "config": {},
    "observability_associations": {
      "github.repository": "repository.full_name",
      "github.issue_number": "issue.number",
      "github.issue_url": "issue.html_url",
      "github.action": "action",
      "github.sender": "sender.login"
    },
    "timeout": 600
  }'
```

`config: {}` is enough: every property has a default, and the manifest's
`suggested_trigger` supplies the trigger.

## Running the scripts directly

```bash
AUTOMATION_PLUGIN_ROOT=plugins/issue-triage python3 scripts/worker.py
```

Without `AUTOMATION_CONFIG_PATH` the worker falls back to the manifest defaults.
It still needs `AGENT_SERVER_URL` and `SESSION_API_KEY` to reach an agent server.

## Relationship to the existing automation

`automations/issue-triage-multi-repo-openhands-software-agent-sdk/` remains the
live, git-synced automation. This plugin package is the same behaviour expressed
as a plugin spec; it is not wired into that sync path, which only manages
`automations/`.
