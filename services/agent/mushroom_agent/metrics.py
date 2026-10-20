"""Prometheus metrics for the agent worker."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

JOBS_CLAIMED = Counter(
    "mushroom_agent_jobs_claimed_total",
    "Messages read off the queue.",
    ["agent"],
)

JOBS_COMPLETED = Counter(
    "mushroom_agent_jobs_completed_total",
    "Jobs that finished, by outcome.",
    ["agent", "status"],
)

JOBS_DEAD_LETTERED = Counter(
    "mushroom_agent_jobs_dead_lettered_total",
    "Jobs retired to the dead-letter queue after too many attempts.",
    ["agent"],
)

JOB_DURATION = Histogram(
    "mushroom_agent_job_duration_seconds",
    "Wall-clock duration of an agent run.",
    ["agent"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1200),
)

JOBS_IN_FLIGHT = Gauge(
    "mushroom_agent_jobs_in_flight",
    "Jobs currently being processed by this worker.",
    ["agent"],
)

QUEUE_DEPTH = Gauge(
    "mushroom_queue_depth",
    "Visible messages on the work queue, as last observed by this worker.",
)
