# mushroom-core

Shared library for the Mushroom services. Holds the pieces that both the
Slack gateway and the agent workers need:

- `config` — environment-driven settings (Pydantic Settings).
- `db` — asyncpg pool management and migration runner.
- `models` — the `SlackEvent` / `Job` domain models.
- `queue` — a thin async client over [pgmq](https://github.com/pgmq/pgmq).
- `store` — the event store and the Postgres-backed KV table.
- `logging` — structlog configuration shared by all entrypoints.
