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
