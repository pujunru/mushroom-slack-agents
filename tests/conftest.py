"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the settings the tests rely on, and clear the config caches.

    Config objects are `lru_cache`d for the process, so a test that changes
    the environment must invalidate them or it will read another test's view.
    """
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    from mushroom_core import config

    for factory in (
        config.database_config,
        config.queue_config,
        config.slack_config,
        config.agent_config,
        config.observability_config,
    ):
        factory.cache_clear()
    yield
    for factory in (
        config.database_config,
        config.queue_config,
        config.slack_config,
        config.agent_config,
        config.observability_config,
    ):
        factory.cache_clear()
