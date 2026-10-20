# Postgres and pgmq

Mushroom needs one Postgres that serves as event store, KV store and message
queue. The only unusual requirement is pgmq.

## Getting pgmq

pgmq can be installed two ways, and the migration supports both.

### As an extension (simplest)

Use an image that ships it:

```
ghcr.io/pgmq/pg17-pgmq:v1.13.1
```

This is what the local compose stack uses. There is **no PGDG apt package**,
so a stock `postgres` image cannot `CREATE EXTENSION pgmq` without one of the
paths below.

### As plain SQL (works on any Postgres)

pgmq contains no C code, so its SQL file can be loaded into a stock image:

```bash
curl -fsSLO https://raw.githubusercontent.com/pgmq/pgmq/v1.13.1/pgmq-extension/sql/pgmq.sql
psql -v ON_ERROR_STOP=1 -f pgmq.sql "$DATABASE_URL"
```

Three things to know about this path:

- The objects land in the `pgmq` schema **with no `pg_extension` row**. `\dx`
  shows nothing; check with `\dn pgmq` or `SELECT * FROM pgmq.list_queues()`.
- It is **not re-runnable**. The file has bare `CREATE TYPE` statements, and
  Postgres has no `IF NOT EXISTS` for types, so a second load aborts. Treat it
  as once per database.
- Partitioned queues are unavailable, since those need `pg_partman`, which is
  itself compiled. Mushroom does not use them.

Use this path for a Postgres you do not control — a managed instance, or an
image without pgmq.

The chart's own bundled Postgres does **not** need it: it runs
`ghcr.io/pgmq/pg17-pgmq`, which ships pgmq as a compiled extension. An earlier
version of this chart depended on the Bitnami postgresql chart and loaded pgmq
with exactly the script above, run as an initdb hook. That was dropped for two
reasons: Bitnami retired their free Docker Hub catalog, so the pinned image
stopped resolving at all, and fetching pgmq at first boot made a cold start
depend on egress to GitHub.

### Why the migration gates on the schema

`db/migrations/0002_pgmq.sql` checks for the `pgmq` *schema*, not the
extension:

```sql
IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'pgmq') THEN
    CREATE EXTENSION pgmq CASCADE;
END IF;
```

`CREATE EXTENSION IF NOT EXISTS pgmq` would be wrong here: it tests for the
extension, which does not exist after a pure-SQL load, so it would run the
extension script and fail on duplicate objects. The schema check is true under
both install paths.

See [verification.md](verification.md) — this reasoning was derived from
pgmq's source, not confirmed by execution.

## The bundled Postgres starts as root, on purpose

`postgresql.podSecurityContext` is empty by default, which makes the chart set
`runAsUser: 0` on the Postgres pod. That looks wrong next to the hardened
settings on every other pod, so it is worth saying why.

The official Postgres entrypoint starts as root, takes ownership of its data
directory, and then re-executes itself as the `postgres` user via `gosu`. The
server process is therefore unprivileged either way; what root buys is the
ability to fix up a data directory the storage layer handed over as
`root:root 0755`. The image sets `USER postgres`, so without `runAsUser: 0`
the entrypoint never gets that chance and `initdb` fails with:

```
initdb: error: could not change permissions of directory
        "/var/lib/postgresql/data": Operation not permitted
```

The container keeps only `CHOWN`, `FOWNER` and `DAC_OVERRIDE` (to take
ownership) plus `SETUID`/`SETGID` (to drop privileges), with everything else
dropped.

**If your storage honours `fsGroup`, prefer pinning the uid instead:**

```yaml
postgresql:
  podSecurityContext:
    runAsUser: 999
    runAsGroup: 999
    runAsNonRoot: true
    fsGroup: 999
```

Most CSI drivers do honour it. `local-path-provisioner` on a plain kubeadm
node does not — verified with minimal probe pods: a volume mounted with
`fsGroup: 999` still presents as `root:root 0755` and the container can
neither `chmod` nor write it, and an init container's `chown` is discarded
between init and the main container. Hence the root-entrypoint default, which
works on both kinds of storage.

## Production options

The bundled chart dependency is for evaluation. For production, disable it and
point at something you operate:

```yaml
postgresql:
  enabled: false
externalDatabase:
  host: postgres.internal
  database: mushroom
  user: mushroom
secrets:
  provider: externalSecrets   # supply database_url from Vault
```

### CloudNativePG

Works well. Add pgmq via the pure-SQL path in a post-init job, or use a custom
image that includes the extension:

```yaml
spec:
  bootstrap:
    initdb:
      postInitApplicationSQL:
        - CREATE SCHEMA IF NOT EXISTS pgmq
```

then load `pgmq.sql` once as a Job.

### Managed Postgres

RDS, Cloud SQL and Azure Postgres do not offer pgmq as an extension, so use
the pure-SQL install. It needs no superuser beyond `CREATE SCHEMA` on the
target database.

## Sizing and tuning

The queue is append-and-delete heavy, so the main concern is autovacuum
keeping up with the queue table.

```sql
ALTER TABLE pgmq.q_mushroom_jobs SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_cost_delay = 0
);
```

Connection pooling matters more than raw size: every gateway and worker pod
holds its own asyncpg pool (`POOL_MIN_SIZE`..`POOL_MAX_SIZE`, default 2..10).
With 2 gateways and 10 workers that is up to 120 connections, so either raise
`max_connections` or put PgBouncer in front in transaction mode.

## Useful queries

```sql
-- Queue depth, and how much is leased right now.
SELECT queue_length, queue_visible_length, oldest_msg_age_sec
FROM pgmq.metrics('mushroom_jobs');

-- What is stuck?
SELECT job_id, agent, attempts, worker_id, started_at
FROM jobs WHERE status = 'running' ORDER BY started_at;

-- What gave up?
SELECT msg_id, message->'_dead_letter'->>'reason' AS reason, message->>'prompt'
FROM pgmq.q_mushroom_jobs_dlq ORDER BY msg_id DESC LIMIT 20;

-- Events that never became jobs, and why.
SELECT event_type, event_subtype, status, count(*)
FROM slack_events GROUP BY 1,2,3 ORDER BY 4 DESC;

-- Replay a dead-lettered job.
SELECT pgmq.send('mushroom_jobs', message - '_dead_letter')
FROM pgmq.q_mushroom_jobs_dlq WHERE msg_id = 42;
```
