-- Core schema: the Slack event store, the job ledger and the KV table.
-- Everything lives in the default schema so a plain DATABASE_URL just works.

-- ---------------------------------------------------------------------------
-- slack_events: every delivery Slack makes, stored verbatim.
--
-- `event_id` is Slack's own per-delivery identifier and doubles as our
-- idempotency key: Slack retries a delivery when we fail to ack in 3s, and the
-- primary key turns that retry into a no-op insert.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS slack_events (
    event_id        text PRIMARY KEY,
    team_id         text,
    api_app_id      text,
    event_type      text NOT NULL,
    event_subtype   text,
    channel         text,
    slack_user      text,
    thread_ts       text,
    -- The complete request body, so a replay needs no call back to Slack.
    payload         jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'received',
    -- Slack's own event timestamp (unix seconds), when provided.
    event_time      bigint,
    received_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT slack_events_status_check CHECK (
        status IN ('received', 'enqueued', 'processing', 'done', 'failed', 'ignored')
    )
);

CREATE INDEX IF NOT EXISTS slack_events_received_at_idx
    ON slack_events (received_at DESC);
CREATE INDEX IF NOT EXISTS slack_events_status_idx
    ON slack_events (status) WHERE status IN ('received', 'enqueued', 'processing');
CREATE INDEX IF NOT EXISTS slack_events_channel_idx
    ON slack_events (channel, received_at DESC);
CREATE INDEX IF NOT EXISTS slack_events_type_idx
    ON slack_events (event_type, received_at DESC);

-- ---------------------------------------------------------------------------
-- jobs: one row per unit of agent work, tracking its queue life and result.
--
-- The queue holds the authoritative "what to do next"; this table is the
-- durable history, so a message archived out of pgmq still has a record.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    job_id          uuid PRIMARY KEY,
    event_id        text NOT NULL REFERENCES slack_events (event_id) ON DELETE CASCADE,
    agent           text NOT NULL DEFAULT 'general',
    status          text NOT NULL DEFAULT 'pending',
    -- pgmq message id, once enqueued. Null if the enqueue failed.
    msg_id          bigint,
    prompt          text NOT NULL DEFAULT '',
    params          jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts        integer NOT NULL DEFAULT 0,
    output          text,
    error           text,
    exit_code       integer,
    duration_seconds double precision,
    -- Which worker pod claimed it, for debugging a stuck job.
    worker_id       text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_status_check CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'dead_lettered')
    )
);

CREATE INDEX IF NOT EXISTS jobs_event_id_idx ON jobs (event_id);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_agent_idx ON jobs (agent, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_msg_id_idx ON jobs (msg_id) WHERE msg_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- kv: the general-purpose key-value store.
--
-- Namespaced so unrelated users of the store cannot collide, with optional
-- TTL. Reads must filter on `expires_at`; `kv_purge_expired()` reclaims rows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kv (
    namespace   text NOT NULL,
    key         text NOT NULL,
    value       jsonb NOT NULL,
    expires_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, key)
);

CREATE INDEX IF NOT EXISTS kv_expires_at_idx
    ON kv (expires_at) WHERE expires_at IS NOT NULL;

CREATE OR REPLACE FUNCTION kv_purge_expired() RETURNS integer AS $$
DECLARE
    deleted integer;
BEGIN
    DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at <= now();
    GET DIAGNOSTICS deleted = ROW_COUNT;
    RETURN deleted;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Keep updated_at honest without every caller remembering to set it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS slack_events_set_updated_at ON slack_events;
CREATE TRIGGER slack_events_set_updated_at
    BEFORE UPDATE ON slack_events
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS jobs_set_updated_at ON jobs;
CREATE TRIGGER jobs_set_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS kv_set_updated_at ON kv;
CREATE TRIGGER kv_set_updated_at
    BEFORE UPDATE ON kv
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
