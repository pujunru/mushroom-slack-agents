"""Environment-driven configuration.

Every setting is read from the process environment. In Kubernetes those
variables are projected from a Kubernetes Secret that External Secrets
Operator syncs out of Vault; locally they come from `.env`. No secret is ever
read from a file baked into an image.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class DatabaseConfig(BaseConfig):
    """Connection settings for the shared Postgres instance."""

    database_url: str = Field(
        default="postgresql://mushroom:mushroom@localhost:5432/mushroom",
        description="libpq-style DSN. asyncpg accepts the postgresql:// form.",
    )
    pool_min_size: int = Field(default=2, ge=0)
    pool_max_size: int = Field(default=10, ge=1)
    # Keep this comfortably under any proxy/LB idle timeout in front of PG.
    pool_max_inactive_connection_lifetime: float = Field(default=300.0, gt=0)
    command_timeout: float = Field(default=30.0, gt=0)

    @field_validator("database_url")
    @classmethod
    def _strip_asyncpg_prefix(cls, value: str) -> str:
        """Accept SQLAlchemy-style DSNs so one env var works for both worlds."""
        return value.replace("postgresql+asyncpg://", "postgresql://", 1)


class QueueConfig(BaseConfig):
    """pgmq queue names and consumer tuning."""

    queue_name: str = Field(default="mushroom_jobs")
    dead_letter_queue_name: str = Field(default="mushroom_jobs_dlq")
    # How long a message stays invisible to other consumers after a read.
    # Must exceed the worst-case job runtime, or two workers will run the
    # same job concurrently.
    visibility_timeout_seconds: int = Field(default=900, ge=1)
    # Messages read per poll. Workers process a batch serially.
    batch_size: int = Field(default=1, ge=1, le=100)
    # Long-poll duration; pgmq holds the connection open rather than
    # returning an empty result immediately.
    poll_timeout_seconds: int = Field(default=5, ge=0)
    poll_interval_ms: int = Field(default=250, ge=1)
    # read_ct above this is treated as a poison message and archived to DLQ.
    max_delivery_attempts: int = Field(default=5, ge=1)


class SlackConfig(BaseConfig):
    """Slack app credentials and ingest behaviour."""

    slack_signing_secret: SecretStr = Field(default=SecretStr(""))
    slack_bot_token: SecretStr = Field(default=SecretStr(""))
    slack_app_token: SecretStr = Field(default=SecretStr(""))
    # Reject requests whose timestamp is older than this, per Slack's
    # replay-attack guidance.
    signature_max_age_seconds: int = Field(default=300, ge=1)
    # Slack expects a 2xx within 3s. We ack immediately after the enqueue.
    enqueue_timeout_seconds: float = Field(default=2.0, gt=0)


class AgentConfig(BaseConfig):
    """Agent worker and opencode runner settings."""

    agent_name: str = Field(default="general")
    # Hard ceiling on a single opencode invocation.
    job_timeout_seconds: int = Field(default=600, ge=1)
    workspace_root: str = Field(default="/workspace")
    opencode_binary: str = Field(default="opencode")
    # Which model opencode invokes, in its `provider/model` form. The provider
    # prefix selects the backend, so changing provider is a values change
    # rather than a code change.
    opencode_model: str = Field(
        default="openrouter/anthropic/claude-sonnet-4.5",
        description="opencode model string, e.g. openrouter/<vendor>/<model>",
    )
    # The credential opencode needs, and the environment variable it expects
    # to find it in. Keeping the *name* configurable is what makes this
    # provider-agnostic: opencode reads a provider-specific variable
    # (OPENROUTER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) from the
    # process serving model requests, so the worker must set whichever one
    # matches the configured model.
    model_api_key: SecretStr = Field(default=SecretStr(""))
    model_api_key_env: str = Field(
        default="OPENROUTER_API_KEY",
        description="Env var name opencode reads the credential from",
    )
    # Post the agent's result back into the originating Slack thread.
    reply_to_slack: bool = Field(default=True)


class ObservabilityConfig(BaseConfig):
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json", pattern="^(json|console)$")
    metrics_port: int = Field(default=9090, ge=1, le=65535)
    service_name: str = Field(default="mushroom")


@lru_cache
def database_config() -> DatabaseConfig:
    return DatabaseConfig()


@lru_cache
def queue_config() -> QueueConfig:
    return QueueConfig()


@lru_cache
def slack_config() -> SlackConfig:
    return SlackConfig()


@lru_cache
def agent_config() -> AgentConfig:
    return AgentConfig()


@lru_cache
def observability_config() -> ObservabilityConfig:
    return ObservabilityConfig()
