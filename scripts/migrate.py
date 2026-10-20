#!/usr/bin/env python
"""Apply database migrations, then exit.

Run as a Helm pre-install/pre-upgrade hook, and usable by hand for local
development. The runner advisory-locks the whole set, so several replicas
starting at once cannot apply the same file twice.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import asyncpg
from mushroom_core import db
from mushroom_core.logging import configure_logging, get_logger
from mushroom_core.queue import QueueClient

log = get_logger("migrate")

DEFAULT_MIGRATIONS_DIR = "/app/db/migrations"
# A fresh Postgres has to run initdb before it accepts connections, and this
# job starts as a Helm pre-install hook — so on a first install the database
# is usually not up yet. Without this wait the job fails immediately and burns
# its backoffLimit in seconds.
DEFAULT_WAIT_SECONDS = 300


async def wait_for_database(timeout_seconds: int) -> None:
    """Block until Postgres accepts a connection, or give up.

    Retries only the errors that mean "not ready yet" — a refused connection,
    a database still starting up, or an unresolvable host (the Service may not
    have endpoints yet). Anything else, such as bad credentials, fails fast
    rather than retrying for five minutes against a wrong password.
    """
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    delay = 1.0
    while True:
        attempt += 1
        try:
            await db.create_pool()
            if await db.healthcheck():
                log.info("migrate.database_ready", attempts=attempt)
                return
            raise ConnectionError("healthcheck returned false")
        except (OSError, ConnectionError, asyncpg.CannotConnectNowError) as exc:
            await db.close_pool()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.error(
                    "migrate.database_unreachable",
                    attempts=attempt,
                    waited_seconds=timeout_seconds,
                    error=str(exc),
                )
                raise
            log.info(
                "migrate.waiting_for_database",
                attempt=attempt,
                error=str(exc)[:200],
                retry_in_seconds=round(delay, 1),
            )
            await asyncio.sleep(min(delay, max(remaining, 0.1)))
            # Back off, but keep checking often enough to start promptly.
            delay = min(delay * 1.5, 10.0)


async def run() -> int:
    configure_logging()
    migrations_dir = os.environ.get("MIGRATIONS_DIR", DEFAULT_MIGRATIONS_DIR)
    wait_seconds = int(os.environ.get("DB_WAIT_SECONDS", DEFAULT_WAIT_SECONDS))

    try:
        await wait_for_database(wait_seconds)
    except Exception:
        log.exception("migrate.failed_waiting_for_database")
        return 1

    try:
        applied = await db.run_migrations(migrations_dir)
        if applied:
            log.info("migrate.applied", files=applied, count=len(applied))
        else:
            log.info("migrate.no_changes")

        # The queue DDL is idempotent, so this also repairs a queue that was
        # dropped out from under the app.
        await QueueClient().ensure_queues()
        log.info("migrate.complete")
    except Exception:
        log.exception("migrate.failed")
        return 1
    finally:
        await db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
