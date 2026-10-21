"""Persistence for Slack events, jobs and the KV table."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from .db import acquire
from .logging import get_logger
from .models import EventStatus, Job, JobResult, JobStatus, SlackEnvelope

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Slack event store
# ---------------------------------------------------------------------------


async def store_event(
    envelope: SlackEnvelope,
    *,
    conn: asyncpg.Connection | None = None,
) -> bool:
    """Persist a Slack delivery.

    Returns True when this is the first time we have seen `event_id`, and
    False when it is a duplicate. Slack retries any delivery it believes we
    failed, so callers use the return value to skip re-enqueueing work.
    """

    async def _run(c: asyncpg.Connection) -> bool:
        row = await c.fetchrow(
            """
            INSERT INTO slack_events (
                event_id, team_id, api_app_id, event_type, event_subtype,
                channel, slack_user, thread_ts, payload, event_time, status
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            envelope.event_id,
            envelope.team_id,
            envelope.api_app_id,
            envelope.event_type,
            envelope.event_subtype,
            envelope.channel,
            envelope.user,
            envelope.thread_ts,
            envelope.model_dump(mode="json"),
            envelope.event_time,
            EventStatus.RECEIVED.value,
        )
        return row is not None

    if conn is not None:
        return await _run(conn)
    async with acquire() as c:
        return await _run(c)


async def set_event_status(
    event_id: str,
    status: EventStatus,
    *,
    conn: asyncpg.Connection | None = None,
) -> None:
    async def _run(c: asyncpg.Connection) -> None:
        await c.execute(
            "UPDATE slack_events SET status = $2 WHERE event_id = $1",
            event_id,
            status.value,
        )

    if conn is not None:
        await _run(conn)
    else:
        async with acquire() as c:
            await _run(c)


async def get_event(event_id: str) -> SlackEnvelope | None:
    """Re-read a stored delivery. Workers use this instead of trusting the
    queue payload, so the payload can never drift from what Slack sent."""
    async with acquire() as c:
        row = await c.fetchrow("SELECT payload FROM slack_events WHERE event_id = $1", event_id)
    if row is None:
        return None
    return SlackEnvelope.model_validate(row["payload"])


async def thread_is_known(
    channel: str,
    thread_ts: str,
    *,
    conn: asyncpg.Connection | None = None,
) -> bool:
    """Has the bot already produced work in this thread?

    True when some earlier event in the same channel and thread was turned
    into a job. That is what distinguishes "a follow-up to something we
    answered" from "ordinary chatter in a channel we happen to be in".

    Matching on `thread_ts` alone is not enough: a thread's root message
    carries its own `ts` as the thread id, so the root is found by either
    column.
    """

    async def _run(c: asyncpg.Connection) -> bool:
        return bool(
            await c.fetchval(
                """
                SELECT EXISTS (
                    -- The bot has spoken here, so this is our conversation.
                    SELECT 1 FROM thread_messages
                    WHERE channel = $1 AND thread_ts = $2 AND role = 'assistant'
                )
                OR EXISTS (
                    -- Or we produced work here before thread_messages existed.
                    SELECT 1
                    FROM slack_events e
                    JOIN jobs j ON j.event_id = e.event_id
                    WHERE e.channel = $1
                      AND (
                            e.thread_ts = $2
                            OR e.payload->'event'->>'ts' = $2
                          )
                )
                """,
                channel,
                thread_ts,
            )
        )

    if conn is not None:
        return await _run(conn)
    async with acquire() as c:
        return await _run(c)


async def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    async with acquire() as c:
        rows = await c.fetch(
            """
            SELECT event_id, event_type, event_subtype, channel, slack_user,
                   status, received_at
            FROM slack_events
            ORDER BY received_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Job ledger
# ---------------------------------------------------------------------------


async def record_job(
    job: Job,
    *,
    msg_id: int | None = None,
    conn: asyncpg.Connection | None = None,
) -> None:
    """Insert the durable record for a job that is about to be queued."""

    async def _run(c: asyncpg.Connection) -> None:
        await c.execute(
            """
            INSERT INTO jobs (job_id, event_id, agent, status, msg_id, prompt, params)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (job_id) DO UPDATE SET msg_id = EXCLUDED.msg_id
            """,
            job.job_id,
            job.event_id,
            job.agent,
            JobStatus.PENDING.value,
            msg_id,
            job.prompt,
            job.params,
        )

    if conn is not None:
        await _run(conn)
    else:
        async with acquire() as c:
            await _run(c)


async def mark_job_running(job_id: uuid.UUID, *, worker_id: str, attempt: int) -> None:
    async with acquire() as c:
        await c.execute(
            """
            UPDATE jobs
            SET status = $2, worker_id = $3, attempts = $4, started_at = now()
            WHERE job_id = $1
            """,
            job_id,
            JobStatus.RUNNING.value,
            worker_id,
            attempt,
        )


async def complete_job(result: JobResult) -> None:
    async with acquire() as c:
        await c.execute(
            """
            UPDATE jobs
            SET status = $2,
                output = $3,
                error = $4,
                exit_code = $5,
                duration_seconds = $6,
                finished_at = $7
            WHERE job_id = $1
            """,
            result.job_id,
            result.status.value,
            result.output,
            result.error,
            result.exit_code,
            result.duration_seconds,
            result.finished_at,
        )


async def get_job(job_id: uuid.UUID) -> dict[str, Any] | None:
    async with acquire() as c:
        row = await c.fetchrow("SELECT * FROM jobs WHERE job_id = $1", job_id)
    return dict(row) if row else None


async def job_counts_by_status() -> dict[str, int]:
    """Backing data for the gateway's /stats endpoint."""
    async with acquire() as c:
        rows = await c.fetch("SELECT status, count(*) AS n FROM jobs GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# Thread history
# ---------------------------------------------------------------------------


async def record_thread_message(
    *,
    channel: str,
    thread_ts: str,
    role: str,
    message_ts: str,
    text: str,
    slack_user: str | None = None,
    job_id: uuid.UUID | None = None,
    conn: asyncpg.Connection | None = None,
) -> None:
    """Append one turn to a thread's history.

    Both sides of the conversation go here: a user's message when it is
    ingested, and the bot's reply when it is posted. Replaying only one side
    would give the agent a transcript with every other line missing.

    Idempotent on (channel, message_ts), because Slack retries deliveries and
    a reply can be re-posted — neither should duplicate a turn.
    """

    async def _run(c: asyncpg.Connection) -> None:
        await c.execute(
            """
            INSERT INTO thread_messages
                (channel, thread_ts, role, message_ts, slack_user, text, job_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (channel, message_ts) DO NOTHING
            """,
            channel,
            thread_ts,
            role,
            message_ts,
            slack_user,
            text,
            job_id,
        )

    if conn is not None:
        await _run(conn)
    else:
        async with acquire() as c:
            await _run(c)


async def thread_history(
    channel: str,
    thread_ts: str,
    *,
    limit: int = 20,
    before_ts: str | None = None,
) -> list[dict[str, Any]]:
    """The last `limit` turns of a thread, oldest first.

    `before_ts` excludes the triggering message itself, so a prompt is not
    duplicated as both history and instruction.

    The ordering is deliberate: the query takes the *most recent* `limit`
    turns, then reverses them. Taking the oldest would mean a long thread
    replays its opening forever and never shows the agent what was just said.
    """
    async with acquire() as c:
        rows = await c.fetch(
            """
            SELECT role, text, slack_user, message_ts
            FROM (
                SELECT role, text, slack_user, message_ts
                FROM thread_messages
                WHERE channel = $1
                  AND thread_ts = $2
                  AND ($3::text IS NULL OR message_ts < $3)
                ORDER BY message_ts DESC
                LIMIT $4
            ) recent
            ORDER BY message_ts ASC
            """,
            channel,
            thread_ts,
            before_ts,
            limit,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Thread -> opencode session
# ---------------------------------------------------------------------------

# Namespace in the KV table. Sessions are scoped per agent profile, because a
# different agent is a different conversation even in the same thread.
SESSION_NAMESPACE = "opencode_session"

# How long an idle thread keeps its session. opencode's own state is on the
# worker's disk, so a session id outliving the pod that created it is a
# dangling reference; a week is long enough for a conversation to continue
# across a weekend and short enough to self-clean.
SESSION_TTL_SECONDS = 7 * 24 * 3600


def _session_key(agent: str, channel: str, thread_ts: str) -> str:
    return f"{agent}:{channel}:{thread_ts}"


async def get_thread_session(agent: str, channel: str, thread_ts: str) -> str | None:
    """The opencode session this thread has been using, if any."""
    value = await kv_get(SESSION_NAMESPACE, _session_key(agent, channel, thread_ts))
    if isinstance(value, dict):
        sid = value.get("session_id")
        return sid if isinstance(sid, str) and sid else None
    return None


async def set_thread_session(agent: str, channel: str, thread_ts: str, session_id: str) -> None:
    """Remember which opencode session belongs to this thread.

    Writing it on every turn refreshes the TTL, so an active conversation
    never expires mid-flow.
    """
    await kv_set(
        SESSION_NAMESPACE,
        _session_key(agent, channel, thread_ts),
        {"session_id": session_id},
        ttl_seconds=SESSION_TTL_SECONDS,
    )


# ---------------------------------------------------------------------------
# KV store
# ---------------------------------------------------------------------------


async def kv_set(
    namespace: str,
    key: str,
    value: Any,
    *,
    ttl_seconds: int | None = None,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds) if ttl_seconds else None
    async with acquire() as c:
        await c.execute(
            """
            INSERT INTO kv (namespace, key, value, expires_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (namespace, key)
            DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at
            """,
            namespace,
            key,
            value,
            expires_at,
        )


async def kv_get(namespace: str, key: str, default: Any = None) -> Any:
    """Read a key, treating an expired row as absent."""
    async with acquire() as c:
        row = await c.fetchrow(
            """
            SELECT value FROM kv
            WHERE namespace = $1 AND key = $2
              AND (expires_at IS NULL OR expires_at > now())
            """,
            namespace,
            key,
        )
    return row["value"] if row else default


async def kv_delete(namespace: str, key: str) -> bool:
    async with acquire() as c:
        result = await c.execute("DELETE FROM kv WHERE namespace = $1 AND key = $2", namespace, key)
    return result.endswith(" 1")


async def kv_list(namespace: str, *, prefix: str = "", limit: int = 100) -> list[dict[str, Any]]:
    async with acquire() as c:
        rows = await c.fetch(
            """
            SELECT key, value, expires_at, updated_at FROM kv
            WHERE namespace = $1 AND key LIKE $2 || '%'
              AND (expires_at IS NULL OR expires_at > now())
            ORDER BY key
            LIMIT $3
            """,
            namespace,
            prefix,
            limit,
        )
    return [dict(r) for r in rows]


async def kv_purge_expired() -> int:
    async with acquire() as c:
        return await c.fetchval("SELECT kv_purge_expired()") or 0
