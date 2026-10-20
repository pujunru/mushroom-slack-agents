"""Domain models shared across the pipeline.

The pipeline moves data through three shapes:

1. `SlackEnvelope` — what Slack POSTs to the gateway, stored verbatim.
2. `Job` — the unit of work the gateway puts on the queue.
3. `JobResult` — what a worker records when it finishes.

The queue payload is deliberately small: it carries the `event_id` and enough
routing context to pick an agent, not the whole Slack body. Workers re-read
the full event from the event store, so a payload can never drift from the
stored truth.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EventStatus(enum.StrEnum):
    """Lifecycle of a stored Slack event."""

    RECEIVED = "received"
    ENQUEUED = "enqueued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    # Recognised but intentionally not turned into a job (e.g. bot's own
    # message, unsubscribed subtype).
    IGNORED = "ignored"


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Exceeded max_delivery_attempts and was moved to the DLQ.
    DEAD_LETTERED = "dead_lettered"


class SlackEnvelope(BaseModel):
    """A raw Slack Events API delivery, as received over HTTP.

    Slack wraps the interesting part in `event`; the outer fields identify the
    workspace and the delivery. We keep the whole thing so a replay needs no
    call back to Slack.
    """

    model_config = ConfigDict(extra="allow")

    # Slack's own delivery id. Unique per event, and our idempotency key.
    event_id: str
    event_time: int | None = None
    team_id: str | None = None
    api_app_id: str | None = None
    type: str = "event_callback"
    event: dict[str, Any] = Field(default_factory=dict)
    authorizations: list[dict[str, Any]] | None = None

    @property
    def event_type(self) -> str:
        """The inner event type, e.g. `app_mention`, `message`."""
        return str(self.event.get("type", "unknown"))

    @property
    def event_subtype(self) -> str | None:
        subtype = self.event.get("subtype")
        return str(subtype) if subtype else None

    @property
    def channel(self) -> str | None:
        channel = self.event.get("channel")
        return str(channel) if channel else None

    @property
    def user(self) -> str | None:
        user = self.event.get("user") or self.event.get("bot_id")
        return str(user) if user else None

    @property
    def text(self) -> str:
        return str(self.event.get("text", ""))

    @property
    def thread_ts(self) -> str | None:
        """Where a reply belongs: the thread root, else the message itself."""
        ts = self.event.get("thread_ts") or self.event.get("ts")
        return str(ts) if ts else None

    @property
    def is_bot_event(self) -> bool:
        """True when we authored the event, so we must not act on it.

        Without this check an agent that posts into a channel it also watches
        will answer itself in an unbounded loop.
        """
        return bool(self.event.get("bot_id")) or self.event.get("subtype") == "bot_message"


class SlackContext(BaseModel):
    """Routing and reply coordinates carried on the job payload."""

    team_id: str | None = None
    channel: str | None = None
    user: str | None = None
    thread_ts: str | None = None
    event_type: str = "unknown"


class Job(BaseModel):
    """A unit of agent work. This is the JSON that lives on the queue."""

    job_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    # The Slack event that caused this job. Foreign key into slack_events.
    event_id: str
    # Which agent profile should handle it, resolved by the gateway's router.
    agent: str = "general"
    # Free-form instruction assembled from the Slack text.
    prompt: str = ""
    slack: SlackContext = Field(default_factory=SlackContext)
    # Arbitrary extras for an agent profile; kept out of the typed fields so
    # adding a profile needs no schema change.
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    # Incremented by the worker from pgmq's read_ct, for observability only.
    attempt: int = 0


class JobResult(BaseModel):
    """Outcome of one agent run."""

    job_id: uuid.UUID
    event_id: str
    status: JobStatus
    # Text the worker posts back to Slack, when replying is enabled.
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    duration_seconds: float = 0.0
    finished_at: datetime = Field(default_factory=_utcnow)


class QueueMessage(BaseModel):
    """A pgmq row, with the queue bookkeeping that wraps the payload.

    Mirrors pgmq's `message_record` composite type. `last_read_at` and
    `headers` were added in the 1.13 line, so both are optional: a client
    reading from an older server simply will not receive them.
    """

    msg_id: int
    # pgmq's delivery counter. Drives poison-message detection.
    read_ct: int
    enqueued_at: datetime
    last_read_at: datetime | None = None
    vt: datetime
    message: dict[str, Any]
    headers: dict[str, Any] | None = None

    def as_job(self) -> Job:
        job = Job.model_validate(self.message)
        job.attempt = self.read_ct
        return job
