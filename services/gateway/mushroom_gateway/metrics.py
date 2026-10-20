"""Prometheus metrics for the gateway.

Exposed on the main port at /metrics so Traefik needs no extra route and a
ServiceMonitor can scrape the same Service.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

EVENTS_RECEIVED = Counter(
    "mushroom_slack_events_received_total",
    "Slack deliveries accepted after signature verification.",
    ["event_type"],
)

EVENTS_REJECTED = Counter(
    "mushroom_slack_events_rejected_total",
    "Slack deliveries rejected before processing.",
    ["reason"],
)

EVENTS_DUPLICATE = Counter(
    "mushroom_slack_events_duplicate_total",
    "Deliveries already present in the event store, i.e. Slack retries.",
)

JOBS_ENQUEUED = Counter(
    "mushroom_jobs_enqueued_total",
    "Jobs written to the queue.",
    ["agent"],
)

EVENTS_IGNORED = Counter(
    "mushroom_slack_events_ignored_total",
    "Deliveries stored but deliberately not turned into a job.",
    ["reason"],
)

INGEST_LATENCY = Histogram(
    "mushroom_ingest_duration_seconds",
    "Time from request receipt to Slack ack.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0),
)
