# What is verified, and what is not

This project was built without a container runtime or a Postgres instance
available on the authoring machine. That has a direct consequence worth
stating plainly rather than burying: **the Python logic is tested; the SQL is
reviewed but not executed.**

## Verified by running it

The suite (`pytest`) passes — 92 tests — and covers the logic that carries
the guarantees:

| Area | What is proven |
|---|---|
| Slack signature verification | HMAC matches an independent inline reimplementation of Slack's documented v0 scheme. Tampered bodies, wrong secrets, stale and future timestamps, and missing headers are all rejected. A signature valid for one body does not authorise another. |
| Gateway HTTP contract | Unsigned requests get 401. The `url_verification` challenge is echoed and not stored. A replayed delivery with Slack's retry headers enqueues **exactly once**. Bot-authored events are stored but never queued. Ingest failure returns 500 so Slack retries. |
| Routing policy | Mention stripping, `!agent` directives, unknown-agent fallback, ignored subtypes, empty prompts, and thread-vs-message reply targeting. |
| Worker delivery semantics | A successful job is acked once and recorded. A crash mid-job does **not** ack, so the message is redelivered. Delivery count past the limit is dead-lettered, and the attempt at the limit still runs. A long job extends its lease. SIGTERM defers queued messages. |
| opencode output parsing | Known event shapes are extracted; unknown JSON is retained without crashing; non-JSON output is preserved as text rather than lost. |
| Test-event sender | Produces signatures the gateway's verifier accepts, and its `--bad-signature` mode is correctly rejected. |
| **The whole pipeline** | `tests/test_end_to_end.py` fakes only Postgres and opencode, driving the real signature check, router, ingest transaction, queue model, worker loop and reply path. It proves one Slack event yields exactly one agent run and one threaded reply even when Slack retries, that an agent failure reaches the user, and that an unsigned request never reaches the queue. |
| Heartbeat arithmetic | The lease refresh interval genuinely survives two consecutive failed extensions. |
| Config invariant | A visibility timeout at or below the job timeout is warned about at worker startup. |

Run it:

```bash
pytest -q
```

## Reviewed but not executed

### The pgmq SQL

Every function signature, argument name, type and return shape in
`mushroom_core/queue.py` was taken from reading pgmq's `pgmq.sql` at tag
`v1.13.1`, not from memory or from an older version's docs. Specifically
checked and corrected during development:

- `pgmq.send` returns `SETOF BIGINT`, and its `delay` argument **requires an
  explicit `::integer` cast** — two 3-arg overloads match by name (`delay
  INTEGER` and `delay TIMESTAMPTZ`), so an untyped parameter fails to resolve.
- `pgmq.set_vt`'s third parameter is named `vt`, not `vt_offset`.
- `pgmq.delete`/`archive` have two overloads whose **return shape differs**
  (`BOOLEAN` for `msg_id`, `SETOF BIGINT` for `msg_ids`). The client uses the
  named `msg_id` form to get a reliable boolean.
- `pgmq.metrics` returns eight columns. `queue_visible_length` excludes leased
  messages and is the better scaling signal, so `depth()` prefers it.
- `message_record` gained `last_read_at` and `headers` in the 1.13 line; both
  are optional on the model so an older server still decodes.
- `pgmq.create` is idempotent and advisory-locked, so the migration calls it
  unconditionally. An earlier check-then-create had a race.
- Queue tables are `pgmq.q_<name>` / `pgmq.a_<name>` with the name
  **lowercased**.

What is *not* proven: that these calls execute successfully against a live
server. The integration suite exists for exactly this and skips without a
database.

```bash
docker compose -f deploy/local/docker-compose.yml up -d postgres
MUSHROOM_TEST_DATABASE_URL=postgresql://mushroom:mushroom@localhost:5432/mushroom \
  pytest tests/test_queue_integration.py -v
```

That suite asserts the round trip, the visibility timeout, lease extension,
dead-lettering, the archive table, all eight metrics columns, the delayed-send
cast, and that a transactional enqueue rolls back with its caller. **Run it
first.** If a signature is wrong, it fails there rather than in production.

### The dual pgmq install path

`db/migrations/0002_pgmq.sql` gates on the `pgmq` *schema* rather than the
extension, because pgmq can be present two ways:

1. As an extension, from an image that ships it (`ghcr.io/pgmq/pg17-pgmq`).
2. As plain SQL loaded into a stock `postgres` image — supported because pgmq
   has no C code.

The reasoning is that `CREATE EXTENSION IF NOT EXISTS pgmq` tests for the
*extension*, which does not exist on path 2, so it would proceed to run the
extension script and fail on already-present objects. That follows from the
`pgmq.sql` file containing no `CREATE EXTENSION` and from pgmq's own
installation notes — but **it was not confirmed by executing it.** Verify on a
throwaway database before relying on the pure-SQL path:

```bash
docker run -d --name pgtest -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:17
curl -fsSLO https://raw.githubusercontent.com/pgmq/pgmq/v1.13.1/pgmq-extension/sql/pgmq.sql
psql -v ON_ERROR_STOP=1 -f pgmq.sql postgres://postgres:postgres@localhost:55432/postgres
# then apply db/migrations/*.sql twice and confirm both runs succeed
docker rm -f pgtest
```

### The Helm chart

Templates were checked for balanced delimiters and block/end pairs, and
`values.yaml`/`Chart.yaml` parse as YAML — but `helm` was not available, so
the chart was never rendered. Before deploying:

```bash
helm dependency update deploy/helm/mushroom
helm lint deploy/helm/mushroom
helm template mushroom deploy/helm/mushroom --set secrets.plain.slackSigningSecret=x \
  --set secrets.plain.slackBotToken=y | kubectl apply --dry-run=client -f -
```

### The container images

Neither Dockerfile was built. The pinned `@opencode/cli@2.1.1` version and the
`--standalone` flag come from opencode v2's documentation; note that
`ghcr.io/anomalyco/opencode:latest` still serves the v1 line, which is why the
agent image installs a pinned npm package rather than using that base.

## Version pins and why

Three upstream artifacts are stale in a way that silently produces a
plausible-but-wrong result, so each is pinned deliberately:

| Pin | Reason |
|---|---|
| pgmq `v1.13.1` | The project README's quickstart still references v1.10.0. |
| `@opencode/cli@2.1.1` | `ghcr.io/anomalyco/opencode:latest` still serves v1 (1.19.3). |
| `external-secrets.io/v1` | `v1beta1` stopped being served in ESO v0.17.0, so older manifests will not apply. |
