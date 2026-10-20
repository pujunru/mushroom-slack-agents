-- Enable pgmq and create the work queues.
--
-- pgmq implements a message queue as a set of SQL functions, giving
-- at-least-once delivery with a visibility timeout. Using it means the event
-- store and the broker are the same database, so an event and the job that
-- references it commit atomically.
--
-- pgmq can be present in two different ways, and this migration must work
-- with both:
--
--   1. As a Postgres *extension*, which needs the compiled library on the
--      server. That is what `ghcr.io/pgmq/pg17-pgmq` ships.
--   2. As *plain SQL*, loaded from pgmq's `pgmq.sql` into a stock `postgres`
--      image. pgmq has no C code, so this path is fully supported, but the
--      objects land in the `pgmq` schema with no `pg_extension` row at all.
--
-- The gate below therefore tests for the **schema**, not the extension. This
-- distinction matters: `CREATE EXTENSION IF NOT EXISTS pgmq` tests for the
-- extension, which does not exist on path 2, so it would proceed to run the
-- extension script and fail on objects that are already there.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'pgmq') THEN
        -- Nothing installed: create the extension. This requires the compiled
        -- library, i.e. a Postgres image that ships pgmq. If this raises,
        -- either use such an image or preload pgmq.sql; see docs/postgres.md.
        CREATE EXTENSION pgmq CASCADE;
    END IF;
    -- The pgmq schema already exists, via either install path. Nothing to do.
END
$$;

-- Queue creation is idempotent and race-free: pgmq.create uses IF NOT EXISTS
-- DDL, an ON CONFLICT DO NOTHING registry insert, and a transaction-scoped
-- advisory lock on the queue name. No existence check is needed or wanted.
SELECT pgmq.create('mushroom_jobs');
SELECT pgmq.create('mushroom_jobs_dlq');
