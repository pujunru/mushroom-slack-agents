# 🍄 Mushroom

Mushroom turns Slack conversations into agent work. It receives Slack events,
stores them durably, queues them, and hands them to a pool of general-purpose
agents that run [opencode](https://opencode.ai) and reply in the thread.

The design goal is that **no event is ever lost and no event is ever acted on
twice**, even when pods are restarted mid-job or Slack retries a delivery.

```
                        ┌──────────────┐
   Slack ──── HTTPS ────►   Traefik    │
                        └──────┬───────┘
                               │
                   ┌───────────┴───────────┐
                   ▼                       ▼
            ┌─────────────┐         ┌─────────────┐
            │  gateway    │   ...   │  gateway    │   stateless, N replicas
            │ (Slack Bolt)│         │ (Slack Bolt)│   verify → store → enqueue
            └──────┬──────┘         └──────┬──────┘
                   └───────────┬───────────┘
                               ▼
                    ┌─────────────────────┐
                    │     Postgres        │
                    │  ┌───────────────┐  │
                    │  │ slack_events  │  │  event store (idempotency)
                    │  │ jobs          │  │  durable job ledger
                    │  │ kv            │  │  key/value store
                    │  │ pgmq queues   │  │  the message queue
                    │  └───────────────┘  │
                    └──────────┬──────────┘
                               │  pgmq.read_with_poll
                   ┌───────────┴───────────┐
                   ▼                       ▼
            ┌─────────────┐         ┌─────────────┐
            │   agent     │   ...   │   agent     │   long-lived workers
            │ (opencode)  │         │ (opencode)  │   run → reply → ack
            └─────────────┘         └─────────────┘
```

## Why one Postgres for both the store and the queue

The queue is [pgmq](https://github.com/pgmq/pgmq), which implements a message
queue as SQL functions inside Postgres. That choice buys one property worth
more than a dedicated broker here: **the event row and its queue message
commit in the same transaction.** A job can never reference an event that was
rolled back, and an event can never be marked enqueued without a message
actually being on the queue. With a separate broker, that gap is a permanent
source of lost or phantom work.

It also means one stateful dependency to operate instead of two.

## How the delivery guarantees work

| Risk | Mitigation |
|---|---|
| Slack retries a delivery we were slow to ack | `slack_events.event_id` is Slack's own delivery id and the primary key, so a retry inserts nothing and enqueues nothing. Slack Bolt does **not** dedupe retries; this table is what does. |
| A worker dies mid-job | pgmq's visibility timeout. A message is hidden, not removed, and reappears when the lease lapses. Workers delete a message only after the job is recorded. |
| A job runs longer than the visibility timeout | The worker heartbeats the lease (`pgmq.set_vt`) at a third of the timeout while it works, so a slow job is never handed to a second worker. |
| One malformed job blocks the queue | pgmq's `read_ct` counts deliveries. Past `maxDeliveryAttempts` the message is moved to a dead-letter queue instead of retried. |
| An agent answers its own message | The router drops bot-authored events before they become jobs. Without this, an agent posting into a channel it watches loops forever. |
| A forged request | Every request is HMAC-verified against the signing secret, with a timestamp window to reject replays. |

## Quick start (local)

Requires Docker and a Slack app.

```bash
git clone https://github.com/your-org/mushroom && cd mushroom
cp .env.example .env          # fill in SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, MODEL_API_KEY
./scripts/dev.sh up
```

That starts Traefik, two gateway replicas, a worker, Postgres with pgmq, and a
dev-mode Vault. Then exercise the pipeline without touching Slack:

```bash
# Posts a correctly-signed fake app_mention.
SLACK_SIGNING_SECRET=<yours> ./scripts/send-test-event.py "summarise the readme"

curl -s localhost:8080/stats | python -m json.tool
./scripts/dev.sh logs
```

To receive real events, expose the gateway (`ngrok http 8080`) and set your
Slack app's **Event Subscriptions → Request URL** to
`https://<tunnel>/slack/events`. Slack sends a `url_verification` challenge
immediately; the gateway answers it automatically. Subscribe the bot to
`app_mention` and invite it to a channel.

## Deploying to Kubernetes

```bash
helm dependency update deploy/helm/mushroom

helm install mushroom deploy/helm/mushroom \
  --namespace mushroom --create-namespace \
  --set traefik.ingressRoute.host=mushroom.example.com \
  --set secrets.provider=plain \
  --set secrets.plain.slackSigningSecret=... \
  --set secrets.plain.slackBotToken=... \
  --set secrets.plain.modelApiKey=...
```

That is the evaluation path. For anything real, put the secrets in Vault and
let External Secrets Operator sync them:

```bash
export VAULT_ADDR=https://vault.example.com VAULT_TOKEN=<admin>
SLACK_SIGNING_SECRET=... SLACK_BOT_TOKEN=... MODEL_API_KEY=... \
  ./scripts/vault-bootstrap.sh

helm upgrade --install mushroom deploy/helm/mushroom \
  --namespace mushroom \
  --set secrets.provider=externalSecrets \
  --set secrets.externalSecrets.vault.server=$VAULT_ADDR
```

Pods never talk to Vault. ESO syncs a Kubernetes Secret and the pods read
environment variables from it, so the cluster holds no long-lived Vault
credential and the app needs no Vault client.

See [docs/deployment.md](docs/deployment.md) for Postgres options, scaling with
KEDA, and NetworkPolicy.

## Using it

Mention the bot in any channel it has been invited to:

```
@mushroom summarise what changed in the payments service this week
```

Route to a specific agent profile with a `!` directive:

```
@mushroom !review look at the open PRs on api-gateway
```

An unknown directive falls back to the general agent with the text intact,
rather than dropping the request.

## Repository layout

```
packages/mushroom-core/     shared library: config, db, models, pgmq client, router
services/gateway/           FastAPI Slack ingest
services/agent/             queue consumer + opencode runner
db/migrations/              SQL migrations, applied by a Helm hook
deploy/helm/mushroom/       the chart
deploy/local/               docker-compose dev stack
images/                     Dockerfiles and the agent's opencode config
scripts/                    dev helpers, migrations, Vault bootstrap
docs/                       architecture, deployment, Postgres, Slack setup
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e packages/mushroom-core -e services/gateway -e services/agent
pip install pytest pytest-asyncio httpx ruff

pytest                      # unit suite, no services needed
./scripts/dev.sh itest      # pgmq integration suite, needs the stack up
ruff check .
```

The unit suite fakes Postgres and opencode, so it runs anywhere. The pgmq
integration suite is the only thing that exercises the real SQL — see
[docs/verification.md](docs/verification.md) for what is and is not covered.

## Configuration

Every setting is an environment variable; see
[`.env.example`](.env.example) and `packages/mushroom-core/mushroom_core/config.py`.
The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | Shared Postgres DSN. |
| `SLACK_SIGNING_SECRET` | — | Required; requests are rejected without it. |
| `SLACK_BOT_TOKEN` | — | Needed to post replies. |
| `MODEL_API_KEY` | — | The model provider credential, passed to opencode. |
| `MODEL_API_KEY_ENV` | `OPENROUTER_API_KEY` | Variable name opencode reads it from; must match the provider. |
| `OPENCODE_MODEL` | `openrouter/anthropic/claude-sonnet-4.5` | `provider/model`; the prefix selects the backend. |
| `VISIBILITY_TIMEOUT_SECONDS` | `900` | Must exceed `JOB_TIMEOUT_SECONDS`. |
| `JOB_TIMEOUT_SECONDS` | `600` | Hard ceiling on one agent run. |
| `MAX_DELIVERY_ATTEMPTS` | `5` | Then dead-lettered. |

## Telling the agent what it is

`agent.systemPrompt` in the chart's values is written to `AGENTS.md`, which is
where opencode reads its instructions. It ships with a sensible default for
Slack — brief replies, Slack markdown, say-so-when-unsure — and changing it is
a `helm upgrade`, not an image rebuild:

```yaml
agent:
  systemPrompt: |
    You answer questions for the platform team.
    Prefer two or three sentences.
```

See [docs/models.md](docs/models.md#the-system-prompt).

## Choosing a model provider

OpenRouter by default, but the provider is configuration rather than code —
`agent.model` plus a matching `agent.modelApiKeyEnv` is the whole switch. See
[docs/models.md](docs/models.md) for direct-provider setups, model choices, and
OpenRouter's prepaid and rate-limit behaviour.

## License

MIT. See [LICENSE](LICENSE).
