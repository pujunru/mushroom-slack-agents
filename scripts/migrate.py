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

from mushroom_core import db
from mushroom_core.logging import configure_logging, get_logger
from mushroom_core.queue import QueueClient

log = get_logger("migrate")

DEFAULT_MIGRATIONS_DIR = "/app/db/migrations"


async def run() -> int:
    configure_logging()
    migrations_dir = os.environ.get("MIGRATIONS_DIR", DEFAULT_MIGRATIONS_DIR)

    await db.create_pool()
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
