-- Per-thread conversation history.
--
-- `slack_events` already stores every delivery verbatim, but it is an audit
-- log of *inbound HTTP*: one row per Slack event, keyed by Slack's delivery
-- id, including events that never became anything. It is the wrong shape for
-- "what was said in this thread, in order" — and it cannot hold the bot's own
-- replies at all, because those are authored by us and never arrive as an
-- event we act on.
--
-- This table is the conversation itself: one row per turn, user and assistant
-- alike, ordered within a thread. It is what gets replayed to the agent so a
-- follow-up like "now double that" has the 42 to work from.
CREATE TABLE IF NOT EXISTS thread_messages (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel     text NOT NULL,
    -- The thread root's ts. A thread's first message carries its own ts here,
    -- so the root and its replies share one key.
    thread_ts   text NOT NULL,
    -- 'user' for a human, 'assistant' for the bot. Matches the vocabulary of
    -- the model APIs this is replayed into, so no translation is needed.
    role        text NOT NULL,
    -- Slack's message ts. Unique per message within a channel, so it both
    -- orders the conversation and makes replay idempotent.
    message_ts  text NOT NULL,
    slack_user  text,
    text        text NOT NULL,
    -- The job that produced an assistant turn, when there was one. Null for
    -- user turns and for anything posted outside the job pipeline.
    job_id      uuid REFERENCES jobs (job_id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT thread_messages_role_check CHECK (role IN ('user', 'assistant')),
    -- Slack can redeliver, and a reply can be retried: the same message must
    -- not appear twice in the history.
    CONSTRAINT thread_messages_unique_message UNIQUE (channel, message_ts)
);

-- The read path is always "the last N turns of one thread, oldest first".
CREATE INDEX IF NOT EXISTS thread_messages_thread_idx
    ON thread_messages (channel, thread_ts, message_ts);

-- Used to answer "have we worked in this thread before?" without scanning.
CREATE INDEX IF NOT EXISTS thread_messages_assistant_idx
    ON thread_messages (channel, thread_ts)
    WHERE role = 'assistant';
