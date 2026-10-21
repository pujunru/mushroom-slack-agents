"""End-to-end test of the whole pipeline.

Only Postgres and opencode are faked. The signature check, router, ingest
transaction, queue message model, worker loop and reply path are all the real
code, so this is the test that catches a break *between* components — the kind
the per-module tests cannot see.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mushroom_core.config import AgentConfig, QueueConfig
from mushroom_core.models import JobStatus, QueueMessage

pytestmark = pytest.mark.asyncio

SECRET = "test-signing-secret"


class Pipeline:
    """Shared fake state standing in for Postgres and pgmq."""

    def __init__(self) -> None:
        self.events: dict[str, Any] = {}
        self.status: dict[str, str] = {}
        self.jobs: dict[str, Any] = {}
        self.queue: list[QueueMessage] = []
        self.results: list[Any] = []
        self.replies: list[tuple[str, str, str | None]] = []
        self.acked: list[int] = []
        self.turns: list[dict] = []
        self.sessions: dict[str, str] = {}
        self._next_msg_id = 1

    # -- gateway side ---------------------------------------------------
    async def send_job(self, job, *, delay_seconds: int = 0, conn=None) -> int:
        msg_id = self._next_msg_id
        self._next_msg_id += 1
        now = datetime.now(UTC)
        self.queue.append(
            QueueMessage(
                msg_id=msg_id,
                read_ct=1,
                enqueued_at=now,
                vt=now,
                message=job.model_dump(mode="json"),
            )
        )
        return msg_id

    async def metrics(self, **_: Any) -> dict[str, Any]:
        return {"queue_length": len(self.queue)}

    # -- worker side ----------------------------------------------------
    @property
    def config(self) -> QueueConfig:
        return QueueConfig(visibility_timeout_seconds=900, max_delivery_attempts=5)

    async def read_with_poll(self, **_: Any) -> list[QueueMessage]:
        return [self.queue.pop(0)] if self.queue else []

    async def delete(self, msg_id: int, **_: Any) -> bool:
        self.acked.append(msg_id)
        return True

    async def dead_letter(self, message: QueueMessage, *, reason: str) -> None:
        return None

    async def set_visibility_timeout(self, msg_id: int, seconds: int, **_: Any) -> None:
        return None

    async def depth(self, **_: Any) -> int:
        return len(self.queue)


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> Pipeline:
    """Wire the fakes into both services and return the shared state."""
    import mushroom_agent.worker as worker_module
    import mushroom_gateway.ingest as ingest_module

    state = Pipeline()

    @contextlib.asynccontextmanager
    async def fake_transaction():
        yield None

    async def store_event(envelope, *, conn=None) -> bool:
        if envelope.event_id in state.events:
            return False
        state.events[envelope.event_id] = envelope
        state.status[envelope.event_id] = "received"
        return True

    async def set_event_status(event_id, status, *, conn=None) -> None:
        state.status[event_id] = status.value

    async def record_job(job, *, msg_id=None, conn=None) -> None:
        state.jobs[str(job.job_id)] = (job, msg_id)

    async def mark_running(job_id, *, worker_id, attempt) -> None:
        return None

    async def complete_job(result) -> None:
        state.results.append(result)

    monkeypatch.setattr(ingest_module, "transaction", fake_transaction)
    monkeypatch.setattr(ingest_module, "store_event", store_event)
    monkeypatch.setattr(ingest_module, "set_event_status", set_event_status)
    monkeypatch.setattr(ingest_module, "record_job", record_job)
    sessions: dict[str, str] = {}

    async def get_session(agent, channel, thread_ts):
        return sessions.get(f"{agent}:{channel}:{thread_ts}")

    async def set_session(agent, channel, thread_ts, session_id):
        sessions[f"{agent}:{channel}:{thread_ts}"] = session_id

    monkeypatch.setattr(worker_module, "mark_job_running", mark_running)
    monkeypatch.setattr(worker_module, "set_event_status", set_event_status)
    monkeypatch.setattr(worker_module, "complete_job", complete_job)
    monkeypatch.setattr(worker_module, "get_thread_session", get_session)
    monkeypatch.setattr(worker_module, "set_thread_session", set_session)
    state.sessions = sessions

    return state


def build_gateway(pipeline: Pipeline) -> FastAPI:
    import mushroom_gateway.app as app_module

    app = FastAPI()
    app.include_router(app_module._health_router())
    app.include_router(app_module._slack_router())
    app.state.queue = pipeline
    return app


def build_worker(pipeline: Pipeline, *, output: str = "All done.", ok: bool = True):
    from mushroom_agent.worker import Worker

    class Replier:
        enabled = True

        async def post(self, *, channel: str, text: str, thread_ts: str | None = None) -> bool:
            pipeline.replies.append((channel, text, thread_ts))
            return True

    class Run:
        def __init__(self) -> None:
            self.ok = ok
            self.text = output
            self.exit_code = 0 if ok else 1
            self.stderr = "" if ok else "the agent failed"
            self.timed_out = False
            self.session_id = "ses_e2e"

    async def runner(prompt: str, cfg, **_: Any):
        assert prompt, "the worker must pass a non-empty prompt to the agent"
        return Run()

    return Worker(
        queue=pipeline,  # type: ignore[arg-type]
        queue_cfg=pipeline.config,
        agent_cfg=AgentConfig(job_timeout_seconds=600, reply_to_slack=True),
        replier=Replier(),
        runner=runner,
    )


def slack_request(text: str, *, event_id: str = "EvE2E") -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": event_id,
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "text": text,
                "channel": "C9",
                "user": "U7",
                "ts": "1700000000.000100",
            },
        }
    ).encode()
    timestamp = str(int(time.time()))
    signature = (
        "v0="
        + hmac.new(SECRET.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    )
    return body, {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    }


async def test_mention_flows_through_to_a_threaded_reply(pipeline: Pipeline) -> None:
    app = build_gateway(pipeline)
    body, headers = slack_request("<@U0BOT> summarise payments deploys")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/slack/events", content=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["enqueued"] is True
    assert len(pipeline.queue) == 1

    worker = build_worker(pipeline, output="3 deploys this week.")
    processed = await worker.poll_once()

    assert processed == 1
    # Acked only after the result was recorded.
    assert pipeline.acked == [1]
    assert pipeline.results[0].status is JobStatus.SUCCEEDED
    # The mention was stripped before reaching the agent.
    job, _ = next(iter(pipeline.jobs.values()))
    assert job.prompt == "summarise payments deploys"
    # The reply landed in the originating thread.
    channel, text, thread_ts = pipeline.replies[0]
    assert (channel, thread_ts) == ("C9", "1700000000.000100")
    assert "3 deploys" in text
    assert pipeline.status["EvE2E"] == "done"


async def test_slack_retry_does_not_duplicate_agent_work(pipeline: Pipeline) -> None:
    """The guarantee that matters most: one Slack event, one agent run."""
    app = build_gateway(pipeline)
    body, headers = slack_request("<@U0BOT> do it once", event_id="EvRetry")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        first = await client.post("/slack/events", content=body, headers=headers)
        retry_headers = dict(headers)
        retry_headers["X-Slack-Retry-Num"] = "1"
        retry_headers["X-Slack-Retry-Reason"] = "http_timeout"
        second = await client.post("/slack/events", content=body, headers=retry_headers)

    assert first.json()["enqueued"] is True
    assert second.json()["duplicate"] is True
    assert len(pipeline.queue) == 1

    worker = build_worker(pipeline)
    await worker.poll_once()
    assert await worker.poll_once() == 0

    # Exactly one agent run and one reply.
    assert len(pipeline.results) == 1
    assert len(pipeline.replies) == 1


async def test_agent_failure_reaches_the_user(pipeline: Pipeline) -> None:
    app = build_gateway(pipeline)
    body, headers = slack_request("<@U0BOT> break please", event_id="EvFail")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/slack/events", content=body, headers=headers)

    worker = build_worker(pipeline, ok=False)
    await worker.poll_once()

    assert pipeline.results[0].status is JobStatus.FAILED
    assert pipeline.status["EvFail"] == "failed"
    # The user is told, rather than left waiting silently.
    assert "could not finish" in pipeline.replies[0][1]


async def test_unsigned_request_never_reaches_the_queue(pipeline: Pipeline) -> None:
    app = build_gateway(pipeline)
    body, _ = slack_request("<@U0BOT> sneak this in")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            "/slack/events", content=body, headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 401
    assert pipeline.queue == []
    assert pipeline.events == {}
