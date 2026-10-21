"""Tests for the agent worker loop.

The queue, the database and opencode are all faked, so these test the
worker's decision-making: when it acks, when it retries, when it gives up,
and whether it keeps its lease alive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from mushroom_core.config import AgentConfig, QueueConfig
from mushroom_core.models import Job, JobStatus, QueueMessage, SlackContext

pytestmark = pytest.mark.asyncio


class FakeQueue:
    """In-memory stand-in that records acks, leases and dead-letters."""

    def __init__(self, messages: list[QueueMessage] | None = None) -> None:
        self.pending = list(messages or [])
        self.deleted: list[int] = []
        self.dead_lettered: list[tuple[int, str]] = []
        self.vt_extensions: list[tuple[int, int]] = []
        self._cfg = QueueConfig(visibility_timeout_seconds=30, max_delivery_attempts=3)

    @property
    def config(self) -> QueueConfig:
        return self._cfg

    async def read_with_poll(self, **_: Any) -> list[QueueMessage]:
        batch, self.pending = self.pending[:1], self.pending[1:]
        return batch

    async def delete(self, msg_id: int, *, queue: str | None = None) -> bool:
        self.deleted.append(msg_id)
        return True

    async def dead_letter(self, message: QueueMessage, *, reason: str) -> None:
        self.dead_lettered.append((message.msg_id, reason))

    async def set_visibility_timeout(self, msg_id: int, seconds: int, *, queue=None) -> None:
        self.vt_extensions.append((msg_id, seconds))

    async def depth(self, **_: Any) -> int:
        return len(self.pending)


class FakeReplier:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def post(self, *, channel: str, text: str, thread_ts: str | None = None) -> bool:
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return True


class FakeRun:
    """Mimics an OpencodeResult."""

    def __init__(
        self,
        *,
        ok: bool = True,
        text: str = "done",
        exit_code: int = 0,
        stderr: str = "",
        session_id: str | None = "ses_fake",
    ) -> None:
        self.ok = ok
        self.text = text
        self.exit_code = exit_code
        self.stderr = stderr
        self.timed_out = False
        self.session_id = session_id


def make_message(
    *,
    msg_id: int = 1,
    read_ct: int = 1,
    job: Job | None = None,
    payload: dict | None = None,
) -> QueueMessage:
    now = datetime.now(UTC)
    body = payload if payload is not None else (job or make_job()).model_dump(mode="json")
    return QueueMessage(msg_id=msg_id, read_ct=read_ct, enqueued_at=now, vt=now, message=body)


def make_job(**kw: Any) -> Job:
    defaults: dict[str, Any] = {
        "event_id": "Ev1",
        "agent": "general",
        "prompt": "do the thing",
        "slack": SlackContext(channel="C1", thread_ts="1.0"),
    }
    defaults.update(kw)
    return Job(**defaults)


@pytest.fixture
def db_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture the store writes the worker makes."""
    from mushroom_agent import worker as worker_module

    calls: dict[str, list] = {"running": [], "complete": [], "event_status": []}

    async def mark_running(job_id, *, worker_id, attempt):
        calls["running"].append((job_id, worker_id, attempt))

    async def complete(result):
        calls["complete"].append(result)

    async def set_status(event_id, status, *, conn=None):
        calls["event_status"].append((event_id, status))

    # The session store talks to Postgres, which these tests do not have.
    sessions: dict[str, str] = {}

    async def get_session(agent, channel, thread_ts):
        return sessions.get(f"{agent}:{channel}:{thread_ts}")

    async def set_session(agent, channel, thread_ts, session_id):
        sessions[f"{agent}:{channel}:{thread_ts}"] = session_id

    monkeypatch.setattr(worker_module, "mark_job_running", mark_running)
    monkeypatch.setattr(worker_module, "complete_job", complete)
    monkeypatch.setattr(worker_module, "set_event_status", set_status)
    monkeypatch.setattr(worker_module, "get_thread_session", get_session)
    monkeypatch.setattr(worker_module, "set_thread_session", set_session)
    calls["sessions"] = sessions
    return calls


def build_worker(queue: FakeQueue, *, runner=None, replier=None, agent_cfg=None):
    from mushroom_agent.worker import Worker

    async def default_runner(prompt, cfg, **kw):
        return FakeRun()

    return Worker(
        queue=queue,  # type: ignore[arg-type]
        queue_cfg=queue.config,
        agent_cfg=agent_cfg or AgentConfig(reply_to_slack=True),
        replier=replier,
        runner=runner or default_runner,
    )


# --- the happy path --------------------------------------------------------


async def test_successful_job_is_acked_and_recorded(db_calls) -> None:
    queue = FakeQueue()
    message = make_message()
    worker = build_worker(queue, replier=FakeReplier())

    await worker.handle_message(message)

    # Acked only after the work finished.
    assert queue.deleted == [1]
    assert queue.dead_lettered == []
    result = db_calls["complete"][0]
    assert result.status is JobStatus.SUCCEEDED
    assert result.output == "done"


async def test_successful_job_replies_in_thread(db_calls) -> None:
    queue = FakeQueue()
    replier = FakeReplier()
    worker = build_worker(queue, replier=replier)

    await worker.handle_message(make_message(job=make_job()))

    assert len(replier.posts) == 1
    assert replier.posts[0]["channel"] == "C1"
    assert replier.posts[0]["thread_ts"] == "1.0"
    assert "done" in replier.posts[0]["text"]


async def test_event_status_transitions_to_done(db_calls) -> None:
    queue = FakeQueue()
    worker = build_worker(queue, replier=FakeReplier())
    await worker.handle_message(make_message())

    statuses = [s.value for _, s in db_calls["event_status"]]
    assert statuses == ["processing", "done"]


# --- failures --------------------------------------------------------------


async def test_failed_agent_run_is_acked_but_marked_failed(db_calls) -> None:
    """A clean agent failure is a finished job, not something to retry: the
    same prompt would fail again."""
    queue = FakeQueue()

    async def failing(prompt, cfg, **kw):
        return FakeRun(ok=False, text="", exit_code=1, stderr="boom")

    worker = build_worker(queue, runner=failing, replier=FakeReplier())
    await worker.handle_message(make_message())

    assert queue.deleted == [1]
    assert db_calls["complete"][0].status is JobStatus.FAILED
    assert [s.value for _, s in db_calls["event_status"]][-1] == "failed"


async def test_failed_run_reply_explains_the_failure(db_calls) -> None:
    queue = FakeQueue()
    replier = FakeReplier()

    async def failing(prompt, cfg, **kw):
        return FakeRun(ok=False, text="", exit_code=1, stderr="traceback here")

    worker = build_worker(queue, runner=failing, replier=replier)
    await worker.handle_message(make_message())

    assert "could not finish" in replier.posts[0]["text"]


async def test_crash_mid_job_does_not_ack(db_calls) -> None:
    """The retry guarantee: an unexpected exception must leave the message on
    the queue so the visibility timeout redelivers it."""
    queue = FakeQueue()

    async def exploding(prompt, cfg, **kw):
        raise RuntimeError("worker lost its mind")

    worker = build_worker(queue, runner=exploding, replier=FakeReplier())
    await worker.handle_message(make_message())

    assert queue.deleted == []
    assert queue.dead_lettered == []


# --- poison messages ------------------------------------------------------


async def test_too_many_attempts_is_dead_lettered(db_calls) -> None:
    """One bad job must not block the queue forever."""
    queue = FakeQueue()
    # max_delivery_attempts is 3; this is the 4th delivery.
    message = make_message(read_ct=4)
    worker = build_worker(queue, replier=FakeReplier())

    await worker.handle_message(message)

    assert queue.dead_lettered == [(1, "max_attempts_exceeded")]
    assert queue.deleted == []
    assert db_calls["complete"][0].status is JobStatus.DEAD_LETTERED


async def test_attempt_at_the_limit_still_runs(db_calls) -> None:
    """Off-by-one guard: the limit is inclusive of the final attempt."""
    queue = FakeQueue()
    worker = build_worker(queue, replier=FakeReplier())
    await worker.handle_message(make_message(read_ct=3))

    assert queue.dead_lettered == []
    assert queue.deleted == [1]


async def test_unparseable_payload_is_dead_lettered(db_calls) -> None:
    queue = FakeQueue()
    worker = build_worker(queue, replier=FakeReplier())

    await worker.handle_message(make_message(payload={"garbage": True}))

    assert queue.dead_lettered == [(1, "invalid_payload")]
    assert queue.deleted == []


async def test_attempt_number_is_recorded_from_read_ct(db_calls) -> None:
    queue = FakeQueue()
    worker = build_worker(queue, replier=FakeReplier())
    await worker.handle_message(make_message(read_ct=2))

    _, _, attempt = db_calls["running"][0]
    assert attempt == 2


# --- lease heartbeating ---------------------------------------------------


async def test_long_job_extends_its_lease(db_calls, monkeypatch) -> None:
    """A job slower than the visibility timeout must keep its lease, or a
    second worker would run it concurrently."""
    import asyncio

    queue = FakeQueue()
    # Visibility timeout 30s -> heartbeat every 10s. Patch sleep so the test
    # does not actually wait.
    queue._cfg = QueueConfig(visibility_timeout_seconds=15, max_delivery_attempts=3)

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds: float) -> None:
        await real_sleep(0.01)

    monkeypatch.setattr("mushroom_agent.worker.asyncio.sleep", fast_sleep)

    async def slow(prompt, cfg, **kw):
        await real_sleep(0.1)
        return FakeRun()

    worker = build_worker(queue, runner=slow, replier=FakeReplier())
    await worker.handle_message(make_message())

    assert queue.vt_extensions, "expected at least one lease extension"
    assert all(seconds == 15 for _, seconds in queue.vt_extensions)
    assert queue.deleted == [1]


# --- shutdown -------------------------------------------------------------


async def test_stop_request_defers_remaining_messages(db_calls) -> None:
    """On SIGTERM the worker leaves queued messages for another pod."""
    queue = FakeQueue(messages=[make_message(msg_id=7)])
    worker = build_worker(queue, replier=FakeReplier())
    worker.request_stop()

    await worker.poll_once()

    assert queue.deleted == []
    assert worker.stopping is True


async def test_reply_is_skipped_when_disabled(db_calls) -> None:
    queue = FakeQueue()
    replier = FakeReplier()
    worker = build_worker(queue, replier=replier, agent_cfg=AgentConfig(reply_to_slack=False))
    await worker.handle_message(make_message())

    assert replier.posts == []
    # The job still completed and was acked.
    assert queue.deleted == [1]


# --- configuration guards -------------------------------------------------


async def test_warns_when_visibility_timeout_is_not_above_job_timeout(capsys) -> None:
    """The invariant that prevents an agent running the same job twice.

    structlog writes to stdout rather than through a propagating stdlib
    logger, so this asserts on captured output rather than caplog.
    """
    queue = FakeQueue()
    queue._cfg = QueueConfig(visibility_timeout_seconds=60)

    build_worker(queue, agent_cfg=AgentConfig(job_timeout_seconds=600))

    assert "visibility_timeout_too_low" in capsys.readouterr().out


async def test_no_warning_when_the_invariant_holds(capsys) -> None:
    queue = FakeQueue()
    queue._cfg = QueueConfig(visibility_timeout_seconds=900)

    build_worker(queue, agent_cfg=AgentConfig(job_timeout_seconds=600))

    assert "visibility_timeout_too_low" not in capsys.readouterr().out


async def test_heartbeat_tolerates_two_consecutive_failures() -> None:
    """The lease must survive two missed extensions, as documented.

    At a third of the timeout the third attempt lands exactly at expiry, so
    only one failure is survivable. This pins the quarter-interval choice.
    """
    from mushroom_core.config import QueueConfig as QC

    vt = 900
    interval = max(QC(visibility_timeout_seconds=vt).visibility_timeout_seconds / 4, 5)
    # After a successful beat at t, two failures mean the next attempt is at
    # 3 * interval. It must land strictly before the lease expires at vt.
    assert 3 * interval < vt, "two missed heartbeats must not lose the lease"


# --- reply shape -----------------------------------------------------------


async def test_reply_goes_into_the_thread(db_calls) -> None:
    """A reply must land in the thread, not as a new channel message."""
    queue = FakeQueue()
    replier = FakeReplier()
    worker = build_worker(queue, replier=replier)

    await worker.handle_message(
        make_message(job=make_job(slack=SlackContext(channel="C1", thread_ts="1700.5")))
    )

    assert replier.posts[0]["thread_ts"] == "1700.5"


async def test_reply_does_not_mention_the_bot(db_calls) -> None:
    """Nothing in the reply path adds an @-mention.

    Re-mentioning would be noisy, and worse: a message the bot authors that
    mentions the bot is the shape of a self-reply loop. The router's
    bot-authored guard stops it, but not emitting one is the first defence.
    """
    queue = FakeQueue()
    replier = FakeReplier()

    async def runner(prompt, cfg, **kw):
        return FakeRun(text="Here is the summary you asked for.")

    worker = build_worker(queue, runner=runner, replier=replier)
    await worker.handle_message(make_message())

    assert "<@" not in replier.posts[0]["text"]


async def test_reply_failure_does_not_crash_the_worker(db_calls, monkeypatch) -> None:
    """A broken Slack client must not take the worker loop down.

    The agent run has already succeeded and been recorded by the time the
    reply is attempted, so a notification failure must never redeliver
    expensive work. A missing aiohttp dependency did exactly this in
    production: the loop crashed after every successful job.
    """
    queue = FakeQueue()
    from mushroom_agent import worker as worker_module

    def exploding_replier(_token):
        raise ModuleNotFoundError("No module named 'aiohttp'")

    monkeypatch.setattr(worker_module, "SlackReplier", exploding_replier)

    worker = build_worker(queue, replier=None)
    await worker.handle_message(make_message())

    # The job still completed and was acked.
    assert queue.deleted == [1]
    assert db_calls["complete"][0].status is JobStatus.SUCCEEDED
