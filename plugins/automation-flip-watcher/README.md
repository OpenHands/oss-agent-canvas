# Automation Flip Watcher

Fast-path uptime alerts for the OSS automation VM. The weekly Automation &
Conversation Health Review stays the high-level overview of the various smaller
issues; this watcher covers the other half — when an automation flips from
consistently passing to consistently failing, Slack hears about it within the
hour instead of at the next Friday review.

## Layout

```
.plugin/plugin.json   # plugin manifest + embedded `automation` block
.claude-plugin        # symlink to .plugin, for other plugin hosts
.codex-plugin         # symlink to .plugin
scripts/worker.py     # entrypoint (runner=script), deterministic — no LLM
scripts/config.json   # defaults the deployed automation overrides
```

## What it alerts on

| Kind | Trigger |
|---|---|
| `flip` | An automation with a healthy baseline reaches `fail_threshold` consecutive failed runs |
| `observed` | First watch of an automation that is already failing (no silent baseline wait) |
| `silent` | The newest run reports COMPLETED but its conversation ended error/stuck |
| `stale` | An enabled automation in scope produced no run for > 2× its cron cadence (min 2h) |
| `recovery` | An alerted automation reaches `healthy_streak` consecutive passing runs again |
| `realert` | Still failing after `realert_hours`; posted as a threaded reply to the original alert |
| `heartbeat` | Daily one-line uptime summary (`<healthy>/<watched> healthy`) at `heartbeat_hour_utc` |

Each alert names the automation, the failing-run count and start time, the last
error (single line, truncated), and links the conversation. Re-alerts within
24h thread on the original message instead of posting duplicates.

## Data flow

1. Read the automation service's SQLite database read-only
   (`file:...?mode=ro`) — `automations` for scope, `automation_runs` for the
   last `watch_window_hours` (default 48h). Non-terminal statuses
   (PENDING/RUNNING) and SKIPPED runs are not outcomes.
2. Compare trailing streaks against `fail_threshold`/`healthy_streak`; the
   state machine in the KV store (`state` key) tracks per-automation status,
   streaks, in-alert flag, and the Slack ts of the open alert thread.
3. Post to `alert_channel` with the `slack_token_secret` (default
   `SLACK_BOT_TOKEN`, the All Hands AI workspace token for `C0BGK8NRYUX`).

## Guardrails

- Read-only against the VM: the database opens in read-only mode and
  conversation `base_state.json` files are only read. The only writes are this
  automation's KV state and Slack messages.
- Never modifies, enables, disables, or dispatches any automation — the weekly
  review remains the improvement recommender; a human (or a follow-up automation)
  picks up the fix from the thread.
- Never watches itself (by automation id and by name).

## Relationship to the weekly review

`automations/weekly-automation-conversation-health-review/` keeps its mandate
(deep Security/Resources/Errors/Skills lens with fingerprint dedupe). The
watcher is the fast path: it does not cluster errors, measure cost, or audit
secrets — it answers one question per hour: "is everything still running?"