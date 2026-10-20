# Slack app setup

## Create the app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App**
   → **From scratch**. Name it and pick a workspace.
2. **Basic Information** → copy the **Signing Secret** into
   `SLACK_SIGNING_SECRET`. Every request is verified against it, so the
   gateway rejects everything without it.

## Scopes

**OAuth & Permissions** → Bot Token Scopes. The minimum for mentions and
replies:

| Scope | Why |
|---|---|
| `app_mentions:read` | Receive `app_mention` events. |
| `chat:write` | Post replies. |
| `channels:history` | Read thread context in public channels. |

Add as needed: `groups:history` (private channels), `im:history` +
`im:write` (DMs), `files:read`, `reactions:write`.

Then **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-…`)
into `SLACK_BOT_TOKEN`.

## Event subscriptions

**Event Subscriptions** → enable, and set the **Request URL** to:

```
https://your-host/slack/events
```

Slack immediately POSTs a `url_verification` challenge. The gateway answers it
automatically, so the URL should verify as soon as it is reachable over HTTPS
with a valid certificate. If it does not, see the troubleshooting table below.

Under **Subscribe to bot events**, add at least `app_mention`. Optionally
`message.channels`, `message.im`, or `assistant_thread_started`.

> Subscribing to `message.channels` means every message in every channel the
> bot is in becomes an event. The gateway stores them all and the router drops
> the noise, but it is a lot of rows. Start with `app_mention`.

## Local development

Slack needs a public HTTPS URL, so tunnel to the local gateway:

```bash
./scripts/dev.sh up
ngrok http 8080
# Request URL: https://<subdomain>.ngrok.app/slack/events
```

Or skip Slack entirely and post a signed event yourself:

```bash
SLACK_SIGNING_SECRET=<yours> ./scripts/send-test-event.py "hello"

# Confirm deduplication: the same event id enqueues exactly once.
SLACK_SIGNING_SECRET=<yours> ./scripts/send-test-event.py --event-id EvFIXED "first"
SLACK_SIGNING_SECRET=<yours> ./scripts/send-test-event.py --event-id EvFIXED --retry 1 "retry"

# Confirm the auth gate: this must return 401.
SLACK_SIGNING_SECRET=<yours> ./scripts/send-test-event.py --bad-signature "nope"
```

## Using the bot

Invite it to a channel (`/invite @yourbot`), then mention it:

```
@yourbot summarise the open incidents
@yourbot !review check the latest PR on api-gateway
```

A `!name` prefix picks an agent profile. An unrecognised name falls back to
the general agent with the text unchanged.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Request URL will not verify | Not reachable over HTTPS, an invalid certificate, or the path is wrong — it must be `/slack/events`. |
| Every request 401s | `SLACK_SIGNING_SECRET` does not match the app's, or a proxy is modifying the request body. The signature covers the exact bytes. |
| Verification works, mentions do nothing | The bot is not subscribed to `app_mention`, or is not in the channel. Check `GET /events` for what actually arrived. |
| Events arrive but no job is created | Check the `status` and `reason` in `/events`. `bot_authored`, `ignored_subtype` and `empty_prompt` are all deliberate. |
| Jobs queue but never run | No worker, or the worker cannot reach Postgres. Check worker logs and `/stats`. |
| The bot replies to itself in a loop | Should be impossible — bot-authored events are dropped before routing. If you see it, the loop guard is the bug; file it. |
| Slack disabled the subscription | Too many failed deliveries. Slack retries then gives up; fix the gateway and re-enable in the app settings. |
