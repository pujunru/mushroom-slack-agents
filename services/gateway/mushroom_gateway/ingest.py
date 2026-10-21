"""The ingest path: verify, store, enqueue, ack.

This is the whole point of the gateway, so the ordering is deliberate.

Slack expects a 2xx within 3 seconds and retries the delivery otherwise. The
handler therefore does the minimum durable work — one insert and one enqueue,
in a single transaction — and acks. No agent work, no Slack API calls, nothing
that can block on a third party.

Two properties fall out of doing it this way:

- **Idempotent.** `slack_events.event_id` is Slack's own delivery id and the
  primary key, so a retry inserts nothing and enqueues nothing. Bolt does not
  dedupe retries itself, so this table is the only thing standing between a
  retry storm and duplicated agent runs.
- **Atomic.** The event row and its queue message commit together. A job can
  never point at an event that was rolled back, and an event can never be
  marked enqueued without a message actually being on the queue.
"""

from __future__ import annotations

import asyncpg
from mushroom_core.db import transaction
from mushroom_core.logging import get_logger
from mushroom_core.models import EventStatus, SlackEnvelope
from mushroom_core.queue import QueueClient
from mushroom_core.router import RoutingDecision, route
from mushroom_core.store import (
    record_job,
    record_thread_message,
    set_event_status,
    store_event,
    thread_is_known,
)

from .metrics import (
    EVENTS_DUPLICATE,
    EVENTS_IGNORED,
    EVENTS_RECEIVED,
    JOBS_ENQUEUED,
)

log = get_logger(__name__)


class IngestResult:
    """What happened to one delivery, for the response body and metrics."""

    __slots__ = ("duplicate", "enqueued", "job_id", "msg_id", "reason")

    def __init__(
        self,
        *,
        duplicate: bool = False,
        enqueued: bool = False,
        reason: str = "",
        job_id: str | None = None,
        msg_id: int | None = None,
    ) -> None:
        self.duplicate = duplicate
        self.enqueued = enqueued
        self.reason = reason
        self.job_id = job_id
        self.msg_id = msg_id

    def as_dict(self) -> dict[str, object]:
        return {
            "duplicate": self.duplicate,
            "enqueued": self.enqueued,
            "reason": self.reason,
            "job_id": self.job_id,
            "msg_id": self.msg_id,
        }


async def ingest_event(
    envelope: SlackEnvelope,
    queue: QueueClient,
    *,
    allowed_agents: frozenset[str] | None = None,
) -> IngestResult:
    """Store a delivery and enqueue the job it implies.

    Safe to call twice with the same envelope: the second call reports
    `duplicate` and does nothing else.
    """
    EVENTS_RECEIVED.labels(event_type=envelope.event_type).inc()

    # A plain `message` only counts when it continues a thread we have
    # already worked in; see router.route. The lookup needs the database, so
    # it happens here rather than inside the pure routing function. A failure
    # is treated as "unknown thread", which errs toward staying quiet.
    in_known_thread = False
    if envelope.event_type == "message" and envelope.channel and envelope.thread_ts:
        try:
            in_known_thread = await thread_is_known(envelope.channel, envelope.thread_ts)
        except Exception:
            log.warning(
                "ingest.thread_lookup_failed",
                event_id=envelope.event_id,
                channel=envelope.channel,
            )

    decision: RoutingDecision = route(
        envelope, allowed_agents=allowed_agents, in_known_thread=in_known_thread
    )

    try:
        async with transaction() as conn:
            is_new = await store_event(envelope, conn=conn)
            if not is_new:
                # A Slack retry, or a duplicate delivery. The first attempt
                # already created whatever work was needed.
                EVENTS_DUPLICATE.inc()
                log.info(
                    "ingest.duplicate",
                    event_id=envelope.event_id,
                    event_type=envelope.event_type,
                )
                return IngestResult(duplicate=True, reason="duplicate")

            # Record the turn before deciding what to do with it. A message
            # that creates no job is still part of the conversation, and a
            # transcript with gaps is worse than none — the agent would see
            # replies to things it cannot read.
            await _record_turn(envelope, conn=conn)

            if not decision.should_enqueue or decision.job is None:
                await set_event_status(envelope.event_id, EventStatus.IGNORED, conn=conn)
                EVENTS_IGNORED.labels(reason=decision.reason.split(":")[0]).inc()
                log.info(
                    "ingest.ignored",
                    event_id=envelope.event_id,
                    event_type=envelope.event_type,
                    reason=decision.reason,
                )
                return IngestResult(reason=decision.reason)

            job = decision.job
            # Enqueue inside the transaction: if the commit fails, the message
            # never becomes visible and the event row never exists.
            msg_id = await queue.send_job(job, conn=conn)
            await record_job(job, msg_id=msg_id, conn=conn)
            await set_event_status(envelope.event_id, EventStatus.ENQUEUED, conn=conn)
    except asyncpg.PostgresError:
        # Let the caller turn this into a 5xx so Slack retries the delivery.
        log.exception("ingest.failed", event_id=envelope.event_id)
        raise

    JOBS_ENQUEUED.labels(agent=job.agent).inc()
    log.info(
        "ingest.enqueued",
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        job_id=str(job.job_id),
        agent=job.agent,
        msg_id=msg_id,
    )
    return IngestResult(enqueued=True, reason="ok", job_id=str(job.job_id), msg_id=msg_id)


async def _record_turn(envelope: SlackEnvelope, *, conn: asyncpg.Connection) -> None:
    """Append this message to its thread's history.

    Best-effort: history is what makes a follow-up coherent, but failing to
    record it must not stop the event being ingested and answered.
    """
    channel = envelope.channel
    thread_ts = envelope.thread_ts
    message_ts = envelope.event.get("ts")
    text = envelope.text
    if not (channel and thread_ts and message_ts and text):
        return
    try:
        await record_thread_message(
            channel=channel,
            thread_ts=thread_ts,
            # Our own messages arrive as events too; they are the assistant
            # side of the transcript.
            role="assistant" if envelope.is_bot_event else "user",
            message_ts=str(message_ts),
            text=text,
            slack_user=envelope.user,
            conn=conn,
        )
    except Exception:
        log.warning("ingest.history_record_failed", event_id=envelope.event_id)
