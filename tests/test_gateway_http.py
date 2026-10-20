"""HTTP-level tests for the gateway.

Postgres and pgmq are replaced with in-memory doubles so these run anywhere.
They cover the contract the gateway owes Slack: reject anything unsigned, echo
the handshake, ack fast, and never do the same work twice.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mushroom_core.models import SlackEnvelope
from mushroom_core.slack_signature import compute_signature

SECRET = "test-signing-secret"
pytestmark = pytest.mark.asyncio


class FakeQueue:
    """Records what would have been enqueued."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.next_msg_id = 1

    async def ensure_queues(self) -> None:  # pragma: no cover - startup only
        return None

    async def send_job(self, job, *, delay_seconds: int = 0, conn=None) -> int:
        msg_id = self.next_msg_id
        self.next_msg_id += 1
        self.sent.append(job.model_dump(mode="json"))
        return msg_id

    async def metrics(self, *, queue: str | None = None) -> dict[str, Any]:
        return {"queue_length": len(self.sent)}


class FakeStore:
    """Stands in for the event-store tables, preserving the PK semantics."""

    def __init__(self) -> None:
        self.events: dict[str, SlackEnvelope] = {}
        self.statuses: dict[str, str] = {}
        self.jobs: list[Any] = []


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """Build an app whose persistence layer is in-memory."""
    import contextlib

    from mushroom_gateway import app as app_module
    from mushroom_gateway import ingest as ingest_module

    store = FakeStore()
    queue = FakeQueue()

    @contextlib.asynccontextmanager
    async def fake_transaction():
        yield None

    async def fake_store_event(envelope, *, conn=None) -> bool:
        if envelope.event_id in store.events:
            return False
        store.events[envelope.event_id] = envelope
        store.statuses[envelope.event_id] = "received"
        return True

    async def fake_set_status(event_id, status, *, conn=None) -> None:
        store.statuses[event_id] = status.value

    async def fake_record_job(job, *, msg_id=None, conn=None) -> None:
        store.jobs.append((job, msg_id))

    monkeypatch.setattr(ingest_module, "transaction", fake_transaction)
    monkeypatch.setattr(ingest_module, "store_event", fake_store_event)
    monkeypatch.setattr(ingest_module, "set_event_status", fake_set_status)
    monkeypatch.setattr(ingest_module, "record_job", fake_record_job)

    # Build the app without the real lifespan, then attach the fake queue.
    test_app = FastAPI()
    test_app.include_router(app_module._health_router())
    test_app.include_router(app_module._slack_router())
    test_app.state.queue = queue

    monkeypatch.setattr(app_module.db, "healthcheck", lambda: _true())
    return test_app, store, queue


async def _true() -> bool:
    return True


def signed_headers(body: bytes, *, secret: str = SECRET, ts: str | None = None) -> dict[str, str]:
    timestamp = ts or str(int(time.time()))
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": compute_signature(secret, timestamp, body),
        "Content-Type": "application/json",
    }


async def post(test_app, body: bytes, headers: dict[str, str]):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/slack/events", content=body, headers=headers)


def event_body(event_id: str = "Ev1", text: str = "<@U0BOT> do the thing") -> bytes:
    import json

    return json.dumps(
        {
            "type": "event_callback",
            "event_id": event_id,
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "text": text,
                "channel": "C1",
                "user": "U1",
                "ts": "1700000000.1",
            },
        }
    ).encode()


# --- authentication --------------------------------------------------------


async def test_unsigned_request_is_rejected(harness) -> None:
    test_app, store, queue = harness
    body = event_body()
    resp = await post(test_app, body, {"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert store.events == {}
    assert queue.sent == []


async def test_wrong_secret_is_rejected(harness) -> None:
    test_app, _store, queue = harness
    body = event_body()
    resp = await post(test_app, body, signed_headers(body, secret="attacker"))
    assert resp.status_code == 401
    assert queue.sent == []


async def test_tampered_body_is_rejected(harness) -> None:
    """A signature for one body must not authorise another."""
    test_app, _store, queue = harness
    headers = signed_headers(event_body(text="harmless"))
    resp = await post(test_app, event_body(text="rm -rf /"), headers)
    assert resp.status_code == 401
    assert queue.sent == []


async def test_replayed_old_timestamp_is_rejected(harness) -> None:
    test_app, _store, _queue = harness
    body = event_body()
    old = str(int(time.time()) - 3600)
    resp = await post(test_app, body, signed_headers(body, ts=old))
    assert resp.status_code == 401


# --- handshake -------------------------------------------------------------


async def test_url_verification_echoes_the_challenge(harness) -> None:
    import json

    test_app, store, queue = harness
    body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    resp = await post(test_app, body, signed_headers(body))
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc123"}
    # The handshake is not an event and must not be stored or enqueued.
    assert store.events == {}
    assert queue.sent == []


# --- ingest ----------------------------------------------------------------


async def test_app_mention_is_stored_and_enqueued(harness) -> None:
    test_app, store, queue = harness
    body = event_body()
    resp = await post(test_app, body, signed_headers(body))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["enqueued"] is True
    assert payload["msg_id"] == 1

    assert "Ev1" in store.events
    assert store.statuses["Ev1"] == "enqueued"
    assert len(queue.sent) == 1
    assert queue.sent[0]["prompt"] == "do the thing"


async def test_retry_of_the_same_event_enqueues_once(harness) -> None:
    """Slack retries any delivery it thinks failed; work must not double up."""
    test_app, _store, queue = harness
    body = event_body(event_id="EvDup")

    first = await post(test_app, body, signed_headers(body))
    assert first.json()["enqueued"] is True

    retry_headers = signed_headers(body)
    retry_headers["X-Slack-Retry-Num"] = "1"
    retry_headers["X-Slack-Retry-Reason"] = "http_timeout"
    second = await post(test_app, body, retry_headers)

    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["enqueued"] is False
    # The critical assertion: still exactly one job.
    assert len(queue.sent) == 1


async def test_bot_message_is_stored_but_not_enqueued(harness) -> None:
    """The self-reply loop guard, enforced at the HTTP boundary."""
    import json

    test_app, store, queue = harness
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "EvBot",
            "event": {
                "type": "message",
                "text": "I am a bot",
                "bot_id": "B1",
                "channel": "C1",
                "ts": "1.0",
            },
        }
    ).encode()
    resp = await post(test_app, body, signed_headers(body))
    assert resp.status_code == 200
    assert resp.json()["enqueued"] is False
    assert resp.json()["reason"] == "bot_authored"
    # Stored for the audit trail, but marked ignored.
    assert store.statuses["EvBot"] == "ignored"
    assert queue.sent == []


async def test_malformed_json_is_a_client_error(harness) -> None:
    test_app, _store, _queue = harness
    body = b"{not json"
    resp = await post(test_app, body, signed_headers(body))
    assert resp.status_code == 400


async def test_ingest_failure_returns_500_so_slack_retries(harness, monkeypatch) -> None:
    test_app, _store, _queue = harness
    from mushroom_gateway import app as app_module

    async def boom(*a, **k):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(app_module, "ingest_event", boom)
    body = event_body(event_id="EvBoom")
    resp = await post(test_app, body, signed_headers(body))
    assert resp.status_code == 500


# --- ops endpoints ---------------------------------------------------------


async def test_healthz_does_not_require_postgres(harness) -> None:
    test_app, _, _ = harness
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_metrics_endpoint_exposes_prometheus_text(harness) -> None:
    test_app, _, _ = harness
    body = event_body(event_id="EvMetrics")
    await post(test_app, body, signed_headers(body))

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "mushroom_slack_events_received_total" in resp.text
