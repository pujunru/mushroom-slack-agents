"""FastAPI application for the Slack gateway.

The service is stateless: every request is verified from the signing secret
and every durable effect lands in the shared Postgres, so any replica can
serve any request and Traefik can load-balance without affinity.

Slack's request path is handled directly rather than through Bolt's listener
dispatch. Bolt would verify the signature and answer `url_verification` for
us, but the gateway's entire job is "verify, persist, enqueue, ack" — one
explicit handler makes the 3-second budget and the idempotency guarantee
auditable in one place, and avoids a second framework in the hot path. The
Slack SDK is still used for its typed web client when replying.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from mushroom_core import db
from mushroom_core.config import (
    observability_config,
    slack_config,
)
from mushroom_core.logging import configure_logging, get_logger
from mushroom_core.models import SlackEnvelope
from mushroom_core.queue import QueueClient
from mushroom_core.slack_signature import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    retry_info,
    verify_request,
)
from mushroom_core.store import job_counts_by_status, recent_events
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .ingest import ingest_event
from .metrics import EVENTS_REJECTED, INGEST_LATENCY

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the pool and make sure the queues exist before serving."""
    configure_logging()
    await db.create_pool()
    queue = QueueClient()
    await queue.ensure_queues()
    app.state.queue = queue
    log.info("gateway.started", service=observability_config().service_name)
    try:
        yield
    finally:
        await db.close_pool()
        log.info("gateway.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Mushroom Slack Gateway",
        version="0.1.0",
        description="Verifies, stores and enqueues Slack events.",
        lifespan=lifespan,
    )
    app.include_router(_health_router())
    app.include_router(_slack_router())
    return app


# ---------------------------------------------------------------------------
# Health and introspection
# ---------------------------------------------------------------------------


def _health_router() -> APIRouter:
    router = APIRouter(tags=["ops"])

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up. Deliberately does not touch Postgres,
        so a database blip does not cause Kubernetes to restart every pod."""
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz() -> Response:
        """Readiness: we can actually serve, i.e. Postgres is reachable."""
        healthy = await db.healthcheck()
        if not healthy:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @router.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @router.get("/stats")
    async def stats(request: Request) -> dict[str, object]:
        """Queue depth and job counts. Handy during local development."""
        queue: QueueClient = request.app.state.queue
        return {
            "queue": await queue.metrics(),
            "jobs_by_status": await job_counts_by_status(),
        }

    @router.get("/events")
    async def events(limit: int = 25) -> dict[str, object]:
        return {"events": await recent_events(min(limit, 200))}

    return router


# ---------------------------------------------------------------------------
# Slack ingest
# ---------------------------------------------------------------------------


def _slack_router() -> APIRouter:
    router = APIRouter(tags=["slack"])
    cfg = slack_config()

    @router.post("/slack/events")
    async def slack_events(request: Request) -> Response:
        start = time.perf_counter()

        # The signature covers the exact bytes Slack sent, so the body must be
        # read raw and verified before any parsing.
        body = await request.body()
        headers = dict(request.headers)

        try:
            verify_request(
                signing_secret=cfg.slack_signing_secret.get_secret_value(),
                timestamp=headers.get(TIMESTAMP_HEADER.lower()) or headers.get(TIMESTAMP_HEADER),
                signature=headers.get(SIGNATURE_HEADER.lower()) or headers.get(SIGNATURE_HEADER),
                body=body,
                max_age_seconds=cfg.signature_max_age_seconds,
            )
        except SignatureError as exc:
            EVENTS_REJECTED.labels(reason="bad_signature").inc()
            log.warning("slack.request.rejected", error=str(exc))
            # 401, not 4xx-with-detail: an unauthenticated caller learns
            # nothing about why.
            return PlainTextResponse("unauthorized", status_code=401)

        try:
            payload = await request.json()
        except Exception:
            EVENTS_REJECTED.labels(reason="malformed_json").inc()
            return PlainTextResponse("bad request", status_code=400)

        # Slack's endpoint handshake. Must echo the challenge verbatim.
        if payload.get("type") == "url_verification":
            log.info("slack.url_verification")
            return JSONResponse({"challenge": payload.get("challenge", "")})

        retry_num, retry_reason = retry_info(headers)
        if retry_num:
            log.info("slack.retry", retry_num=retry_num, retry_reason=retry_reason)

        if payload.get("type") != "event_callback":
            # Acked so Slack stops resending something we do not consume.
            log.info("slack.unhandled_envelope", envelope_type=payload.get("type"))
            return JSONResponse({"ok": True, "ignored": "envelope_type"})

        try:
            envelope = SlackEnvelope.model_validate(payload)
        except Exception:
            EVENTS_REJECTED.labels(reason="invalid_envelope").inc()
            log.exception("slack.envelope.invalid")
            return PlainTextResponse("bad request", status_code=400)

        queue: QueueClient = request.app.state.queue
        try:
            result = await ingest_event(envelope, queue)
        except Exception:
            # 500 makes Slack retry the delivery, which is what we want: the
            # event is not yet durably stored.
            EVENTS_REJECTED.labels(reason="ingest_error").inc()
            log.exception("slack.ingest.error", event_id=envelope.event_id)
            return PlainTextResponse("internal error", status_code=500)
        finally:
            INGEST_LATENCY.observe(time.perf_counter() - start)

        return JSONResponse({"ok": True, **result.as_dict()})

    return router


app = create_app()
