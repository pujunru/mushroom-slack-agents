"""Async client over pgmq.

pgmq exposes a queue as a set of SQL functions in the `pgmq` schema, so the
whole client is parameterised SQL over the same asyncpg pool the rest of the
app uses. One Postgres is both the store and the broker: an event and the job
that references it can be written in a single transaction, so a job can never
reference an event that was rolled back.

Delivery is at-least-once. A read makes a message invisible for
`visibility_timeout` seconds rather than removing it; the consumer deletes it
only after the work succeeds. A crashed worker's message therefore reappears,
which is why the visibility timeout must exceed the worst-case job runtime and
why handlers must tolerate a repeat.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from .config import QueueConfig, queue_config
from .db import acquire, get_pool
from .logging import get_logger
from .models import Job, QueueMessage

log = get_logger(__name__)


class QueueClient:
    """Thin wrapper over the `pgmq` SQL functions."""

    def __init__(self, cfg: QueueConfig | None = None) -> None:
        self._cfg = cfg or queue_config()

    @property
    def config(self) -> QueueConfig:
        return self._cfg

    # -- setup --------------------------------------------------------------

    async def ensure_queues(self) -> None:
        """Create the work queue and its dead-letter queue if absent.

        `pgmq.create` is idempotent: its table and index DDL is all
        `IF NOT EXISTS`, its registry insert is `ON CONFLICT DO NOTHING`, and
        it holds a transaction-scoped advisory lock on the queue name. Calling
        it unconditionally is therefore both safe and race-free, and strictly
        better than checking first — a check-then-create has a window where two
        replicas both see the queue missing.
        """
        async with acquire() as conn:
            for name in (self._cfg.queue_name, self._cfg.dead_letter_queue_name):
                await conn.execute("SELECT pgmq.create($1)", name)
        log.info(
            "queue.ensured",
            queues=[self._cfg.queue_name, self._cfg.dead_letter_queue_name],
        )

    # -- producing ----------------------------------------------------------

    async def send(
        self,
        payload: dict[str, Any],
        *,
        queue: str | None = None,
        delay_seconds: int = 0,
        conn: asyncpg.Connection | None = None,
    ) -> int:
        """Enqueue one message, returning its pgmq `msg_id`.

        Pass `conn` to enqueue inside a caller's transaction, so the message
        becomes visible only if that transaction commits.
        """
        target = queue or self._cfg.queue_name
        # The ::integer cast is required, not cosmetic: two 3-arg overloads
        # match by name (delay INTEGER and delay TIMESTAMPTZ), so an untyped
        # parameter fails to resolve. INTEGER delay is seconds from now.
        sql = "SELECT pgmq.send(queue_name => $1, msg => $2::jsonb, delay => $3::integer)"

        async def _run(c: asyncpg.Connection) -> int:
            return await c.fetchval(sql, target, payload, delay_seconds)

        if conn is not None:
            msg_id = await _run(conn)
        else:
            async with acquire() as c:
                msg_id = await _run(c)
        log.debug("queue.sent", queue=target, msg_id=msg_id)
        return int(msg_id)

    async def send_job(
        self,
        job: Job,
        *,
        delay_seconds: int = 0,
        conn: asyncpg.Connection | None = None,
    ) -> int:
        return await self.send(job.model_dump(mode="json"), delay_seconds=delay_seconds, conn=conn)

    # -- consuming ----------------------------------------------------------

    async def read(
        self,
        *,
        queue: str | None = None,
        count: int | None = None,
        visibility_timeout: int | None = None,
    ) -> list[QueueMessage]:
        """Read up to `count` messages, hiding them for the visibility timeout."""
        target = queue or self._cfg.queue_name
        rows = await self._read_rows(
            target,
            count or self._cfg.batch_size,
            visibility_timeout or self._cfg.visibility_timeout_seconds,
        )
        return [QueueMessage.model_validate(dict(r)) for r in rows]

    async def _read_rows(self, queue: str, count: int, vt: int) -> list[asyncpg.Record]:
        async with acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM pgmq.read(queue_name => $1, vt => $2, qty => $3)",
                queue,
                vt,
                count,
            )

    async def read_with_poll(
        self,
        *,
        queue: str | None = None,
        count: int | None = None,
        visibility_timeout: int | None = None,
        max_poll_seconds: int | None = None,
        poll_interval_ms: int | None = None,
    ) -> list[QueueMessage]:
        """Long-poll for messages.

        pgmq holds the connection open and returns as soon as a message
        appears, which keeps idle workers from hammering the database with
        empty selects.
        """
        target = queue or self._cfg.queue_name
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM pgmq.read_with_poll(
                    queue_name          => $1,
                    vt                  => $2,
                    qty                 => $3,
                    max_poll_seconds    => $4,
                    poll_interval_ms    => $5
                )
                """,
                target,
                visibility_timeout or self._cfg.visibility_timeout_seconds,
                count or self._cfg.batch_size,
                (
                    max_poll_seconds
                    if max_poll_seconds is not None
                    else self._cfg.poll_timeout_seconds
                ),
                (poll_interval_ms if poll_interval_ms is not None else self._cfg.poll_interval_ms),
            )
        return [QueueMessage.model_validate(dict(r)) for r in rows]

    # -- acknowledging ------------------------------------------------------

    async def delete(self, msg_id: int, *, queue: str | None = None) -> bool:
        """Remove a message for good. This is the success ack."""
        target = queue or self._cfg.queue_name
        # Named `msg_id` selects the single-message overload, which returns a
        # boolean. The `msg_ids` array overload returns SETOF BIGINT instead,
        # so the argument name decides the result shape here.
        async with acquire() as conn:
            ok = await conn.fetchval(
                "SELECT pgmq.delete(queue_name => $1, msg_id => $2::bigint)",
                target,
                msg_id,
            )
        return bool(ok)

    async def archive(self, msg_id: int, *, queue: str | None = None) -> bool:
        """Move a message to the queue's archive table, retaining it."""
        target = queue or self._cfg.queue_name
        async with acquire() as conn:
            ok = await conn.fetchval(
                "SELECT pgmq.archive(queue_name => $1, msg_id => $2::bigint)",
                target,
                msg_id,
            )
        return bool(ok)

    async def set_visibility_timeout(
        self, msg_id: int, seconds: int, *, queue: str | None = None
    ) -> None:
        """Extend or shorten a message's invisibility.

        A long-running handler calls this as a heartbeat to keep its lease
        from expiring mid-job.
        """
        target = queue or self._cfg.queue_name
        async with acquire() as conn:
            # The third parameter is named `vt` in every overload; the
            # INTEGER form is an offset in seconds from now.
            await conn.execute(
                # `SELECT * FROM`, not `SELECT pgmq.set_vt(...)`. set_vt
                # returns SETOF pgmq.message_record, and asyncpg refuses a
                # composite type in text mode:
                #   cannot decode type "pgmq"."message_record"
                # Selecting from it expands the record into ordinary scalar
                # columns, which decode fine. The row itself is discarded —
                # this call is made for its effect on the lease.
                "SELECT * FROM pgmq.set_vt(queue_name => $1, "
                "msg_id => $2::bigint, vt => $3::integer)",
                target,
                msg_id,
                seconds,
            )

    async def dead_letter(self, message: QueueMessage, *, reason: str) -> None:
        """Retire a poison message: copy it to the DLQ, then drop the original.

        Both steps run in one transaction so a failure cannot lose the message
        or leave it in both places.
        """
        payload = dict(message.message)
        payload["_dead_letter"] = {
            "reason": reason,
            "read_ct": message.read_ct,
            "original_msg_id": message.msg_id,
        }
        async with get_pool().acquire() as conn, conn.transaction():
            await conn.fetchval(
                "SELECT pgmq.send(queue_name => $1, msg => $2::jsonb)",
                self._cfg.dead_letter_queue_name,
                payload,
            )
            await conn.fetchval(
                "SELECT pgmq.delete(queue_name => $1, msg_id => $2::bigint)",
                self._cfg.queue_name,
                message.msg_id,
            )
        log.warning(
            "queue.dead_lettered",
            msg_id=message.msg_id,
            read_ct=message.read_ct,
            reason=reason,
        )

    # -- introspection ------------------------------------------------------

    async def metrics(self, *, queue: str | None = None) -> dict[str, Any]:
        """Queue depth and age, for the /stats endpoint and KEDA scaling.

        Returns pgmq's `metrics_result`: queue_name, queue_length,
        newest_msg_age_sec, oldest_msg_age_sec, total_messages, scrape_time,
        queue_visible_length and default_partition_length. Note that
        `queue_length` counts every message including invisible ones, while
        `queue_visible_length` counts only those available to a reader — the
        latter is the better scaling signal.
        """
        target = queue or self._cfg.queue_name
        async with acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM pgmq.metrics($1)", target)
        return dict(row) if row else {}

    async def depth(self, *, queue: str | None = None) -> int:
        """Messages currently available to a reader.

        Prefers `queue_visible_length` over `queue_length`, so work already
        leased by a running worker is not counted as pending backlog. Falls
        back to `queue_length` on a server predating that column.
        """
        m = await self.metrics(queue=queue)
        visible = m.get("queue_visible_length")
        if visible is None:
            visible = m.get("queue_length", 0)
        return int(visible)


_client: QueueClient | None = None


def get_queue_client() -> QueueClient:
    global _client
    if _client is None:
        _client = QueueClient()
    return _client
