# Architecture

## The path an event takes

1. **Slack POSTs** to `/slack/events` through Traefik.
2. **The gateway verifies** the HMAC signature over the raw body, with a
   300-second timestamp window to reject replays. A failure is a 401 with no
   detail.
3. **It stores the delivery verbatim** in `slack_events`, keyed by Slack's own
   `event_id`. A duplicate insert is a no-op, which is the whole idempotency
   mechanism.
4. **The router decides** whether the event becomes work, and which agent
   profile takes it. Bot-authored events, noise subtypes and empty prompts are
   stored and marked `ignored`.
5. **The job is enqueued** to pgmq *in the same transaction* as the event
   insert, then the gateway acks. Nothing slow happens before the ack.
6. **A worker long-polls** `pgmq.read_with_poll`, which hides the message for
   the visibility timeout rather than removing it.
7. **The worker runs opencode**, heartbeating the message's lease while it
   works.
8. **It records the result**, deletes the message, and posts the output into
   the originating Slack thread.

## Why the gateway is stateless

Everything durable lives in Postgres, and every request carries its own proof
of authenticity. So a gateway pod holds nothing another pod needs: Traefik can
round-robin without session affinity, a replica can be killed mid-rollout, and
scaling is just `replicas`.

The one constraint this creates is the 3-second Slack ack budget. The handler
does exactly one insert and one enqueue, and nothing that can block on a third
party — no Slack API calls, no model calls, no agent work. That is why the
agent lives in a separate process entirely.

## Why the agent is a long-lived worker, not a Job per message

A Job per message gives stronger isolation but pays a container cold start on
every Slack mention, which is the latency the user actually feels. Long-lived
workers keep a warm process, and the queue's visibility timeout already
provides the crash-safety that a Job's restart policy would.

Concurrency is replicas, not threads: each worker processes one job at a time.
That keeps a pod's CPU, memory and lease behaviour predictable, and makes
`queue_visible_length / replicas` a meaningful scaling signal.

## Scaling the workers

CPU is a poor signal for these pods — an agent spends most of a job waiting on
a model, so a backlogged worker looks idle. Scale on queue depth instead, with
KEDA:

```yaml
keda:
  enabled: true
  maxReplicaCount: 20
  queueDepthTarget: "5"
```

The scaler queries `queue_visible_length`, which excludes messages already
leased by a running worker, so in-flight work is not counted twice as backlog.

## The data model

### `slack_events` — the event store

Holds the complete request body as `jsonb`. Two reasons it is verbatim rather
than parsed into columns: a replay needs no call back to Slack, and a new
event type needs no migration. The parsed columns that do exist (`channel`,
`event_type`, `thread_ts`) are there for indexing, not as the source of truth.

Workers re-read the event from this table rather than trusting the queue
payload, so a payload can never drift from what Slack actually sent.

### `jobs` — the durable ledger

The queue holds "what to do next"; this table holds history. A message
archived out of pgmq still has a row here, with its worker, attempt count,
duration and output. This is what you query when someone asks why the bot did
not answer.

### `kv` — the key-value store

Namespaced, with optional TTL. Reads filter on `expires_at`, so an expired row
reads as absent before `kv_purge_expired()` reclaims it.

### pgmq queues

`mushroom_jobs` and `mushroom_jobs_dlq`. pgmq keeps the queue in
`pgmq.q_mushroom_jobs` and its archive in `pgmq.a_mushroom_jobs`.

## Failure modes and what happens

| Failure | Behaviour |
|---|---|
| Postgres unreachable at ingest | Gateway returns 500; Slack retries the delivery. Readiness fails so the pod leaves the Service. |
| Postgres unreachable mid-job | The job errors without acking; the message is redelivered. |
| Worker OOM-killed mid-job | No ack, so the lease lapses and another worker picks it up. |
| opencode missing from the image | The job fails fast with a clear error rather than retrying — a deployment fault, not a bad job. |
| opencode hangs | Killed at `JOB_TIMEOUT_SECONDS`; its whole process group is terminated. |
| Model API returns an error | The job is marked failed and acked. The same prompt would fail again, so retrying wastes money. |
| Slack reply fails | Logged and ignored. The work is done and recorded; a dropped notification is not worth re-running an expensive agent. |
| A job that always crashes | Dead-lettered after `MAX_DELIVERY_ATTEMPTS`, so it cannot block the queue. |

## Security posture

- Requests are HMAC-verified; the signing secret never leaves the gateway.
- Prompts are passed to opencode as a single argv element, never through a
  shell, so Slack message text cannot be interpreted as shell syntax.
- Containers run as uid 10001, non-root, with a read-only root filesystem, all
  capabilities dropped and `RuntimeDefault` seccomp. Writable paths are
  explicit `emptyDir` mounts.
- Secrets reach pods only as environment variables from a Kubernetes Secret
  that ESO syncs from Vault. Pods hold no Vault credential.
- Only `/slack/events` is routed publicly. `/metrics`, `/stats` and `/events`
  are cluster-internal.
- An optional default-deny NetworkPolicy restricts egress to Postgres, DNS and
  outbound HTTPS.

A deliberate caveat: agents execute model-directed work with
`permissions: autoaccept`, because a human cannot approve each step of an
unattended run. Treat an agent pod as having the capabilities of whatever you
mount into it, and scope its credentials accordingly.
