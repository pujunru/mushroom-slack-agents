"""The agent worker loop.

A worker is a long-lived pod that pulls jobs off pgmq and runs them. The loop
is deliberately simple and single-flight per worker: concurrency comes from
running more replicas, which keeps each pod's CPU, memory and queue lease
behaviour predictable, and lets Kubernetes do the scaling.

The important behaviour is what happens around failure:

- **At-least-once, acked on success.** A message is deleted only after the job
  is recorded. A worker killed mid-job never acks, so its message reappears
  once the visibility timeout lapses.
- **Lease heartbeating.** Long runs push the visibility timeout forward while
  they work, so a slow job is not handed to a second worker. Without this a
  job slower than the timeout would run repeatedly and forever.
- **Poison-message retirement.** pgmq's `read_ct` counts deliveries. Past
  `max_delivery_attempts` the message is moved to the dead-letter queue
  instead of being retried, so one malformed job cannot block the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import uuid

from mushroom_core import db
from mushroom_core.config import (
    AgentConfig,
    QueueConfig,
    agent_config,
    observability_config,
    queue_config,
    slack_config,
)
from mushroom_core.logging import configure_logging, get_logger
from mushroom_core.models import EventStatus, Job, JobResult, JobStatus, QueueMessage
from mushroom_core.queue import QueueClient
from mushroom_core.store import complete_job, mark_job_running, set_event_status

from .metrics import (
    JOB_DURATION,
    JOBS_CLAIMED,
    JOBS_COMPLETED,
    JOBS_DEAD_LETTERED,
    JOBS_IN_FLIGHT,
    QUEUE_DEPTH,
)
from .opencode import OpencodeError, run_opencode
from .slack_client import SlackReplier

log = get_logger(__name__)


class Worker:
    """Consumes jobs from pgmq and runs them through opencode."""

    def __init__(
        self,
        *,
        queue: QueueClient | None = None,
        agent_cfg: AgentConfig | None = None,
        queue_cfg: QueueConfig | None = None,
        replier: SlackReplier | None = None,
        runner=run_opencode,
    ) -> None:
        self._queue_cfg = queue_cfg or queue_config()
        self._queue = queue or QueueClient(self._queue_cfg)
        self._agent_cfg = agent_cfg or agent_config()
        self._replier = replier
        self._runner = runner
        # Identifies this pod in the jobs table, for debugging a stuck job.
        self.worker_id = os.environ.get("HOSTNAME") or f"worker-{uuid.uuid4().hex[:8]}"
        self._stopping = asyncio.Event()
        self._warn_on_timeout_misconfiguration()

    def _warn_on_timeout_misconfiguration(self) -> None:
        """Check the one configuration invariant that silently corrupts work.

        The visibility timeout must exceed the job timeout. If it does not,
        the only thing keeping a long job from being redelivered is the
        heartbeat task, so a single missed extension hands the same job to a
        second worker and the agent runs twice. The heartbeat is an
        optimisation; the timeout is the correctness backstop.

        This warns rather than raising: refusing to start would turn a
        suboptimal setting into an outage, and the heartbeat does still
        protect the common case.
        """
        vt = self._queue_cfg.visibility_timeout_seconds
        job_timeout = self._agent_cfg.job_timeout_seconds
        if vt <= job_timeout:
            log.warning(
                "worker.config.visibility_timeout_too_low",
                visibility_timeout_seconds=vt,
                job_timeout_seconds=job_timeout,
                detail=(
                    "visibility timeout must exceed the job timeout, or a job "
                    "slower than the timeout can be delivered to a second "
                    "worker and run twice"
                ),
            )

    # -- lifecycle ----------------------------------------------------------

    def request_stop(self) -> None:
        """Finish the current job, then exit. Wired to SIGTERM."""
        if not self._stopping.is_set():
            log.info("worker.stopping", worker_id=self.worker_id)
            self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    async def run_forever(self) -> None:
        log.info(
            "worker.started",
            worker_id=self.worker_id,
            agent=self._agent_cfg.agent_name,
            queue=self._queue_cfg.queue_name,
        )
        while not self.stopping:
            try:
                processed = await self.poll_once()
            except Exception:
                log.exception("worker.poll.error", worker_id=self.worker_id)
                # Back off so a persistent failure does not spin hot.
                await self._sleep(5.0)
                continue
            if not processed:
                # read_with_poll already waited, so no extra sleep is needed.
                continue
        log.info("worker.stopped", worker_id=self.worker_id)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # -- the loop body ------------------------------------------------------

    async def poll_once(self) -> int:
        """Read and process one batch. Returns how many messages ran."""
        messages = await self._queue.read_with_poll()
        if not messages:
            with contextlib.suppress(Exception):
                QUEUE_DEPTH.set(await self._queue.depth())
            return 0

        for message in messages:
            if self.stopping:
                # Leave it on the queue; the lease lapses and another worker
                # picks it up rather than it being half-run during shutdown.
                log.info("worker.deferring_message", msg_id=message.msg_id)
                break
            await self.handle_message(message)
        return len(messages)

    async def handle_message(self, message: QueueMessage) -> None:
        """Process one queue message end to end."""
        try:
            job = message.as_job()
        except Exception:
            log.exception("worker.payload.invalid", msg_id=message.msg_id)
            await self._queue.dead_letter(message, reason="invalid_payload")
            JOBS_DEAD_LETTERED.labels(agent="unknown").inc()
            return

        # read_ct is pgmq's delivery counter. Past the limit, stop retrying.
        if message.read_ct > self._queue_cfg.max_delivery_attempts:
            log.warning(
                "worker.poison_message",
                msg_id=message.msg_id,
                read_ct=message.read_ct,
                job_id=str(job.job_id),
            )
            await self._queue.dead_letter(message, reason="max_attempts_exceeded")
            await self._fail_job(
                job,
                error=f"exceeded {self._queue_cfg.max_delivery_attempts} delivery attempts",
                status=JobStatus.DEAD_LETTERED,
            )
            JOBS_DEAD_LETTERED.labels(agent=job.agent).inc()
            return

        JOBS_CLAIMED.labels(agent=job.agent).inc()
        JOBS_IN_FLIGHT.labels(agent=job.agent).inc()
        started = time.perf_counter()

        # Keep the lease alive for as long as the job runs.
        heartbeat = asyncio.create_task(self._heartbeat(message.msg_id))
        try:
            await mark_job_running(job.job_id, worker_id=self.worker_id, attempt=message.read_ct)
            await set_event_status(job.event_id, EventStatus.PROCESSING)
            result = await self._execute(job)
        except Exception as exc:
            # Do not ack: the message reappears and is retried.
            log.exception("worker.job.error", job_id=str(job.job_id))
            await self._fail_job(job, error=str(exc))
            JOBS_COMPLETED.labels(agent=job.agent, status="error").inc()
            return
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            JOBS_IN_FLIGHT.labels(agent=job.agent).dec()
            JOB_DURATION.labels(agent=job.agent).observe(time.perf_counter() - started)

        # The job finished and is recorded, so the message can be acked.
        await self._queue.delete(message.msg_id)
        await complete_job(result)
        await set_event_status(
            job.event_id,
            EventStatus.DONE if result.status is JobStatus.SUCCEEDED else EventStatus.FAILED,
        )
        JOBS_COMPLETED.labels(agent=job.agent, status=result.status.value).inc()
        log.info(
            "worker.job.finished",
            job_id=str(job.job_id),
            status=result.status.value,
            duration_seconds=round(result.duration_seconds, 2),
        )

        await self._reply(job, result)

    async def _execute(self, job: Job) -> JobResult:
        """Run the agent for one job."""
        started = time.perf_counter()
        try:
            run = await self._runner(
                job.prompt,
                self._agent_cfg,
                timeout_seconds=self._agent_cfg.job_timeout_seconds,
            )
        except OpencodeError as exc:
            # A missing binary is a deployment fault, not a bad job.
            return JobResult(
                job_id=job.job_id,
                event_id=job.event_id,
                status=JobStatus.FAILED,
                error=str(exc),
                duration_seconds=time.perf_counter() - started,
            )

        return JobResult(
            job_id=job.job_id,
            event_id=job.event_id,
            status=JobStatus.SUCCEEDED if run.ok else JobStatus.FAILED,
            output=run.text,
            error=None if run.ok else (run.stderr[-4000:] or "agent run failed"),
            exit_code=run.exit_code,
            duration_seconds=time.perf_counter() - started,
        )

    async def _heartbeat(self, msg_id: int) -> None:
        """Periodically extend the message's visibility lease.

        Refreshes at a quarter of the timeout, which genuinely tolerates two
        consecutive failures: with a 900s timeout, beats land at 225s, 450s
        and 675s, so even after missing two the next attempt still precedes
        expiry. A third of the timeout would leave the third attempt landing
        exactly at expiry, tolerating only one failure.
        """
        interval = max(self._queue_cfg.visibility_timeout_seconds / 4, 5)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._queue.set_visibility_timeout(
                    msg_id, self._queue_cfg.visibility_timeout_seconds
                )
                log.debug("worker.lease.extended", msg_id=msg_id)
            except Exception as exc:
                log.warning("worker.lease.extend_failed", msg_id=msg_id, error=str(exc))

    async def _fail_job(
        self,
        job: Job,
        *,
        error: str,
        status: JobStatus = JobStatus.FAILED,
    ) -> None:
        result = JobResult(job_id=job.job_id, event_id=job.event_id, status=status, error=error)
        with contextlib.suppress(Exception):
            await complete_job(result)
            await set_event_status(job.event_id, EventStatus.FAILED)

    async def _reply(self, job: Job, result: JobResult) -> None:
        """Post the outcome back to the originating thread."""
        if not self._agent_cfg.reply_to_slack or not job.slack.channel:
            return
        try:
            replier = self._replier or SlackReplier(
                slack_config().slack_bot_token.get_secret_value()
            )
        except Exception as exc:
            # The job is done and recorded by this point. Failing to build the
            # Slack client is a notification problem, not a reason to crash
            # the worker loop and redeliver expensive agent work.
            log.warning("slack.replier.unavailable", error=str(exc))
            return
        if result.status is JobStatus.SUCCEEDED:
            body = result.output
        else:
            detail = result.error or "unknown error"
            body = f":warning: The agent could not finish this request.\n```{detail}```"
        await replier.post(channel=job.slack.channel, text=body, thread_ts=job.slack.thread_ts)


async def main() -> None:
    configure_logging()
    await db.create_pool()
    queue = QueueClient()
    await queue.ensure_queues()

    worker = Worker(queue=queue)

    # Kubernetes sends SIGTERM and then waits for terminationGracePeriod, so
    # draining here is what prevents a job being cut in half on a rollout.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)

    _start_metrics_server()
    try:
        await worker.run_forever()
    finally:
        await db.close_pool()


def _start_metrics_server() -> None:
    from prometheus_client import start_http_server

    port = observability_config().metrics_port
    try:
        start_http_server(port)
        log.info("worker.metrics.listening", port=port)
    except OSError as exc:
        log.warning("worker.metrics.failed", port=port, error=str(exc))


if __name__ == "__main__":
    asyncio.run(main())
