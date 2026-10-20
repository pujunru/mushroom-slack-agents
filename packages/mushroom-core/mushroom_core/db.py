"""Postgres pool management and the migration runner.

One asyncpg pool per process, created on startup and closed on shutdown. JSON
and JSONB are encoded/decoded as Python objects so callers never hand-roll
`json.dumps` at the call site.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from .config import DatabaseConfig, database_config
from .logging import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Make jsonb round-trip as dict/list instead of str."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def create_pool(cfg: DatabaseConfig | None = None) -> asyncpg.Pool:
    """Create the process-wide pool. Idempotent."""
    global _pool
    if _pool is not None:
        return _pool

    cfg = cfg or database_config()
    _pool = await asyncpg.create_pool(
        dsn=cfg.database_url,
        min_size=cfg.pool_min_size,
        max_size=cfg.pool_max_size,
        max_inactive_connection_lifetime=cfg.pool_max_inactive_connection_lifetime,
        command_timeout=cfg.command_timeout,
        init=_init_connection,
    )
    log.info("db.pool.created", min_size=cfg.pool_min_size, max_size=cfg.pool_max_size)
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Postgres pool not initialised; call create_pool() first")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db.pool.closed")


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    async with get_pool().acquire() as conn:
        yield conn


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    async with get_pool().acquire() as conn, conn.transaction():
        yield conn


async def healthcheck() -> bool:
    """Cheap liveness probe for the readiness endpoint."""
    try:
        async with acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1
    except Exception as exc:
        log.warning("db.healthcheck.failed", error=str(exc))
        return False


MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS mushroom_migrations (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


async def run_migrations(migrations_dir: str | pathlib.Path) -> list[str]:
    """Apply every unapplied .sql file in lexical order.

    Each file runs inside its own transaction and is recorded in
    `mushroom_migrations`. Advisory-locking the whole run means several
    replicas starting at once cannot apply the same file twice.
    """
    # These are blocking filesystem reads in an async function, which is
    # deliberate: migrations run once at startup before the service accepts
    # traffic, so there is no event loop to starve. The files are small and
    # local, and offloading them to a thread would add moving parts for no
    # measurable gain.
    directory = pathlib.Path(migrations_dir)
    if not directory.is_dir():  # noqa: ASYNC240
        raise FileNotFoundError(f"migrations directory not found: {directory}")

    files = sorted(p for p in directory.glob("*.sql") if p.is_file())  # noqa: ASYNC240
    applied: list[str] = []

    async with acquire() as conn:
        # Arbitrary but fixed key; any process running migrations uses it.
        await conn.execute("SELECT pg_advisory_lock($1)", 0x4D555348)
        try:
            await conn.execute(MIGRATIONS_TABLE)
            done = {
                r["filename"] for r in await conn.fetch("SELECT filename FROM mushroom_migrations")
            }
            for path in files:
                if path.name in done:
                    continue
                sql = path.read_text()
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO mushroom_migrations (filename) VALUES ($1)",
                        path.name,
                    )
                applied.append(path.name)
                log.info("db.migration.applied", filename=path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", 0x4D555348)

    if not applied:
        log.info("db.migrations.up_to_date", count=len(files))
    return applied
