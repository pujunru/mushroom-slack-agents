"""structlog setup shared by every entrypoint.

Services log JSON by default so a cluster log collector can parse them; set
`LOG_FORMAT=console` for readable local output.
"""

from __future__ import annotations

import logging
import sys

import structlog

from .config import observability_config


def configure_logging() -> None:
    cfg = observability_config()
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    # Slack's SDK and asyncpg are chatty at DEBUG; keep them a notch quieter.
    for noisy in ("slack_bolt", "slack_sdk", "asyncpg", "httpx"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if cfg.log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
