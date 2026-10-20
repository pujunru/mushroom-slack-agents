"""Integration tests for the pgmq client.

These exercise the real SQL against a real Postgres with pgmq installed, so
they are the only check that the function signatures in `queue.py` are
correct. They skip when no database is reachable.

    docker compose -f deploy/local/docker-compose.yml up -d postgres
    MUSHROOM_TEST_DATABASE_URL=postgresql://mushroom:mushroom@localhost:5432/mushroom \
        pytest tests/test_queue_integration.py
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.asyncio

DSN = os.environ.get("MUSHROOM_TEST_DATABASE_URL")

pytest.importorskip("asyncpg", reason="asyncpg is required for queue integration tests")

if not DSN:
    pytest.skip(
        "set MUSHROOM_TEST_DATABASE_URL to run pgmq integration tests",
        allow_module_level=True,
    )


@pytest.fixture
async def queue_client():
    """A client bound to a throwaway queue, dropped on teardown."""
    import asyncpg
    from mushroom_core import db
    from mushroom_core.config import QueueConfig
    from mushroom_core.queue import QueueClient

    try:
        await db.create_pool(_config(DSN))
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres not reachable: {exc}")

    suffix = uuid.uuid4().hex[:8]
    cfg = QueueConfig(
        queue_name=f"test_jobs_{suffix}",
        dead_letter_queue_name=f"test_jobs_dlq_{suffix}",
        visibility_timeout_seconds=2,
        batch_size=10,
        poll_timeout_seconds=1,
    )
    client = QueueClient(cfg)
    async with db.acquire() as conn:
        has_pgmq = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'pgmq')"
        )
        if not has_pgmq:
            pytest.skip("pgmq is not installed in this database")
    await client.ensure_queues()

    yield client

    async with db.acquire() as conn:
        for name in (cfg.queue_name, cfg.dead_letter_queue_name):
            await conn.execute("SELECT pgmq.drop_queue($1)", name)
    await db.close_pool()


def _config(dsn: str):
    from mushroom_core.config import DatabaseConfig

    return DatabaseConfig(database_url=dsn)


async def test_send_and_read_round_trips_the_payload(queue_client) -> None:
    msg_id = await queue_client.send({"hello": "world", "n": 1})
    assert msg_id > 0

    messages = await queue_client.read(count=1)
    assert len(messages) == 1
    assert messages[0].msg_id == msg_id
    assert messages[0].message == {"hello": "world", "n": 1}
    assert messages[0].read_ct == 1


async def test_read_hides_the_message_from_a_second_reader(queue_client) -> None:
    """The visibility timeout is what stops two workers running one job."""
    await queue_client.send({"x": 1})
    first = await queue_client.read(count=10, visibility_timeout=30)
    assert len(first) == 1

    second = await queue_client.read(count=10)
    assert second == []


async def test_delete_acknowledges_permanently(queue_client) -> None:
    msg_id = await queue_client.send({"x": 1})
    messages = await queue_client.read(count=1, visibility_timeout=1)
    assert await queue_client.delete(messages[0].msg_id) is True

    # Even after the visibility timeout lapses it must not come back.
    import asyncio

    await asyncio.sleep(1.5)
    assert await queue_client.read(count=10) == []
    assert msg_id == messages[0].msg_id


async def test_message_reappears_after_visibility_timeout(queue_client) -> None:
    """A worker that dies without acking must not lose the job."""
    import asyncio

    await queue_client.send({"x": 1})
    first = await queue_client.read(count=1, visibility_timeout=1)
    assert len(first) == 1

    await asyncio.sleep(1.5)
    again = await queue_client.read(count=1, visibility_timeout=30)
    assert len(again) == 1
    assert again[0].msg_id == first[0].msg_id
    # read_ct is how the worker detects a poison message.
    assert again[0].read_ct == 2


async def test_read_with_poll_returns_promptly(queue_client) -> None:
    await queue_client.send({"x": 1})
    messages = await queue_client.read_with_poll(count=1, max_poll_seconds=2)
    assert len(messages) == 1


async def test_read_with_poll_returns_empty_when_idle(queue_client) -> None:
    messages = await queue_client.read_with_poll(count=1, max_poll_seconds=1)
    assert messages == []


async def test_set_visibility_timeout_extends_a_lease(queue_client) -> None:
    import asyncio

    await queue_client.send({"x": 1})
    messages = await queue_client.read(count=1, visibility_timeout=1)
    await queue_client.set_visibility_timeout(messages[0].msg_id, 30)

    await asyncio.sleep(1.5)
    # The extension held, so it is still invisible.
    assert await queue_client.read(count=10) == []


async def test_dead_letter_moves_the_message_to_the_dlq(queue_client) -> None:
    await queue_client.send({"poison": True})
    messages = await queue_client.read(count=1, visibility_timeout=30)
    await queue_client.dead_letter(messages[0], reason="too_many_attempts")

    # Gone from the work queue.
    assert await queue_client.read(count=10) == []
    # Present in the DLQ, with the diagnostic context attached.
    dlq = await queue_client.read(
        queue=queue_client.config.dead_letter_queue_name, count=10, visibility_timeout=30
    )
    assert len(dlq) == 1
    assert dlq[0].message["poison"] is True
    assert dlq[0].message["_dead_letter"]["reason"] == "too_many_attempts"


async def test_archive_retains_the_message(queue_client) -> None:
    from mushroom_core import db

    await queue_client.send({"keep": "me"})
    messages = await queue_client.read(count=1, visibility_timeout=30)
    assert await queue_client.archive(messages[0].msg_id) is True

    assert await queue_client.read(count=10) == []
    # pgmq.format_table_name lowercases the queue name, so the archive table
    # is pgmq.a_<lowercased>.
    table = f"a_{queue_client.config.queue_name.lower()}"
    async with db.acquire() as conn:
        # Table identifiers cannot be parameterised, and this name is built
        # from the test's own uuid-derived queue name, not from input.
        n = await conn.fetchval(f'SELECT count(*) FROM pgmq."{table}"')  # noqa: S608
    assert n == 1


async def test_metrics_reports_queue_depth(queue_client) -> None:
    await queue_client.send({"a": 1})
    await queue_client.send({"b": 2})
    assert await queue_client.depth() == 2

    m = await queue_client.metrics()
    assert m["queue_length"] == 2
    assert "newest_msg_age_sec" in m


async def test_send_job_and_round_trip_as_job(queue_client) -> None:
    from mushroom_core.models import Job, SlackContext

    job = Job(
        event_id="Ev1",
        agent="general",
        prompt="do the thing",
        slack=SlackContext(channel="C1", thread_ts="1.0"),
    )
    await queue_client.send_job(job)

    messages = await queue_client.read(count=1, visibility_timeout=30)
    restored = messages[0].as_job()
    assert restored.job_id == job.job_id
    assert restored.prompt == "do the thing"
    assert restored.slack.channel == "C1"
    # attempt is populated from pgmq's read_ct.
    assert restored.attempt == 1


async def test_transactional_send_rolls_back_with_its_caller(queue_client) -> None:
    """The reason store and broker share a database: atomic enqueue."""
    from mushroom_core import db

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with db.transaction() as conn:
            await queue_client.send({"should": "vanish"}, conn=conn)
            raise Boom

    assert await queue_client.read(count=10) == []


async def test_message_record_exposes_pgmq_bookkeeping(queue_client) -> None:
    """pgmq 1.13 added last_read_at and headers to message_record."""
    await queue_client.send({"x": 1})
    messages = await queue_client.read(count=1, visibility_timeout=30)
    msg = messages[0]
    assert msg.read_ct == 1
    # Populated by the read that just returned it.
    assert msg.last_read_at is not None
    assert msg.enqueued_at is not None
    assert msg.vt is not None


async def test_metrics_exposes_all_documented_columns(queue_client) -> None:
    await queue_client.send({"a": 1})
    m = await queue_client.metrics()
    for column in (
        "queue_name",
        "queue_length",
        "newest_msg_age_sec",
        "oldest_msg_age_sec",
        "total_messages",
        "scrape_time",
        "queue_visible_length",
        "default_partition_length",
    ):
        assert column in m, f"missing metrics column: {column}"


async def test_depth_counts_only_visible_messages(queue_client) -> None:
    """Depth is a backlog signal, so leased messages must not count."""
    await queue_client.send({"a": 1})
    await queue_client.send({"b": 2})
    assert await queue_client.depth() == 2

    # Lease one; it is no longer available to a reader.
    await queue_client.read(count=1, visibility_timeout=60)
    assert await queue_client.depth() == 1


async def test_send_with_delay_hides_the_message_initially(queue_client) -> None:
    """Confirms the ::integer delay cast resolves to the seconds overload."""
    import asyncio

    await queue_client.send({"later": True}, delay_seconds=2)
    assert await queue_client.read(count=10) == []

    await asyncio.sleep(2.5)
    messages = await queue_client.read(count=10, visibility_timeout=30)
    assert len(messages) == 1
    assert messages[0].message == {"later": True}
