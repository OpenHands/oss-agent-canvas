# Linear Assistant (webhook)

The webhook-driven form of `plugins/linear-assistant`. Same behaviour, same
config, same Linear bot (`all-hands-bot-internal`), but Linear pushes each
@-mention or assignment to the automation service instead of the script polling
the bot's notifications every five minutes.

Use this where the automation service is publicly reachable (e.g. Cloud). The
OSS VM sits behind a Google OAuth gate that Linear cannot pass, so it keeps the
polling plugin.

## How a request flows

```
Linear ──webhook──▶ /api/automation/v1/events/<org_id>/linear ──▶ one run of scripts/main.py
```

1. Linear sends an `AppUserNotification` webhook for the bot.
2. The automation service verifies the HMAC signature and matches `action`
   (`issueMention`, `issueCommentMention`, `issueAssignedToYou`).
3. The run reads the notification id from the payload, re-reads the notification
   from Linear's API (so the request text never comes from the webhook body),
   starts a conversation over fresh clones of the candidate repos, and waits for
   it to finish.
4. It replies in the comment thread and opens a draft PR for every branch that
   actually has commits on GitHub.

There is no state store: one run handles one request, and the automation
service drops redelivered webhooks by `Linear-Delivery` id.

## Differences from the polling plugin

| | polling | webhook |
|---|---|---|
| trigger | cron `*/5 * * * *` | event, source `linear` |
| latency | up to 5 minutes | seconds |
| idle cost | a run every 5 minutes | none |
| concurrency cap | `max_concurrent_tasks` via KV | the automation service's own run limits |
| finishing a task | a later tick | the same run waits (`timeout` must cover `max_active_age_seconds`) |
| lost delivery | picked up by the next poll | lost; mention the bot again |

## Setup

Secrets (names are configurable): `LINEAR_BOT_READ_AND_MENTION_TOKEN` with
`read` and `comments:create`, and `GITHUB_TOKEN`.

1. Create the webhook on the automation service. `event_id_header` is what lets
   it drop Linear redeliveries; keep the returned `webhook_secret`:

   ```bash
   curl -X POST "${OPENHANDS_HOST}/api/automation/v1/webhooks" \
     -H "Authorization: Bearer ${OPENHANDS_API_KEY}" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "Linear",
       "source": "linear",
       "event_key_expr": "action",
       "signature_header": "Linear-Signature",
       "signature_scheme": "hmac_sha256_hex",
       "event_id_header": "Linear-Delivery"
     }'
   ```

2. In Linear (Settings > API > the OAuth application), set the webhook URL to
   the returned `webhook_url`, paste the signing secret, and subscribe to
   app-user notifications.

3. Create the automation. The timeout must be at least `max_active_age_seconds`,
   and staging caps it at 1800 s, so lower the default (3 h) to fit. A task that
   needs longer is abandoned with a reply asking the user to mention the bot again:

   ```bash
   curl -X POST "${OPENHANDS_HOST}/api/automation/v1/plugin-spec" \
     -H "Authorization: Bearer ${OPENHANDS_API_KEY}" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "Linear Assistant (webhook)",
       "plugin": {"source": "github:OpenHands/oss-agent-canvas", "repo_path": "plugins/linear-assistant-webhook", "ref": "main"},
       "config": {"openhands_url": "'"${OPENHANDS_HOST}"'", "max_active_age_seconds": 1500},
       "timeout": 1700
     }'
   ```

Do not run this alongside the polling plugin against the same bot: each
mention would be answered twice.
