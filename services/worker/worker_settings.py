"""Worker-specific configuration — loaded from pydantic-settings.

All worker configuration lives here rather than in ``core.config`` because the
worker process is independently deployable and should not depend on API-specific
settings.  Import the ``settings`` singleton rather than instantiating
:class:`WorkerSettings` directly.

Usage:
    from services.worker.worker_settings import settings

    arq_redis = await create_pool(RedisSettings.from_dsn(str(settings.REDIS_URL)))
    queue = settings.high_queue_full
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic.networks import RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_queue_name(env: str, queue_type: str) -> str:
    """Generate a namespaced queue name.

    Pattern: ``OpenZep:{env}:queue:{queue_type}``

    Examples:
        ``OpenZep:dev:queue:high``
        ``OpenZep:prod:queue:low``

    Args:
        env: Environment name (e.g. ``dev``, ``staging``, ``prod``).
        queue_type: Queue type — one of ``"high"`` (real-time ingestion) or
            ``"low"`` (scheduled batch / background maintenance).

    Returns:
        Fully qualified queue name string.

    Raises:
        ValueError: If ``queue_type`` is not ``"high"`` or ``"low"``.
    """
    if queue_type not in ("high", "low"):
        raise ValueError(f"queue_type must be 'high' or 'low', got {queue_type!r}")
    return f"OpenZep:{env}:queue:{queue_type}"


class WorkerSettings(BaseSettings):
    """Worker-specific configuration — loaded from environment variables.

    All durations are in **seconds** unless otherwise noted.

    Settings are read from environment variables (no ``MG_`` prefix — the worker
    process has its own env-file contract).  Create the module-level ``settings``
    singleton — do not instantiate this class directly.
    """

    model_config = SettingsConfigDict(
        env_prefix="MG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ── Redis ────────────────────────────────────────────────────────────────
    REDIS_URL: RedisDsn = Field(
        default="redis://localhost:6379",
        description="Redis connection string for the ARQ job queue.",
    )

    # ── Environment ──────────────────────────────────────────────────────────
    ENV: str = Field(
        default="development",
        description=(
            "Environment name used in queue name prefix: "
            "``OpenZep:{env}:queue:{queue_name}``."
        ),
    )

    # ── Concurrency ──────────────────────────────────────────────────────────
    MAX_WORKERS: int = Field(
        default=4,
        ge=1,
        le=32,
        description=(
            "Maximum number of concurrent tasks a worker processes. "
            "Set to CPU count for CPU-bound work, higher for I/O-bound tasks "
            "(LLM API calls, DB queries, embedding)."
        ),
    )

    # ── Job defaults ─────────────────────────────────────────────────────────
    JOB_TIMEOUT_DEFAULT: int = Field(
        default=300,
        description=(
            "Default job timeout in seconds. "
            "Individual tasks may override this (see 02-task-definitions.md)."
        ),
    )
    JOB_KEEP_RESULT_FOR: int = Field(
        default=3_600,
        description="Seconds to keep successful job results in Redis.",
    )
    JOB_KEEP_RESULT_FOR_FAILURE: int = Field(
        default=86_400,
        description=(
            "Seconds to keep failed job results in Redis. "
            "Longer than success TTL so failed jobs can be debugged."
        ),
    )

    # ── Queue names ──────────────────────────────────────────────────────────
    HIGH_QUEUE_NAME: str = Field(
        default="high",
        description="Queue name for real-time / priority tasks.",
    )
    LOW_QUEUE_NAME: str = Field(
        default="low",
        description="Queue name for batch / scheduled tasks.",
    )

    # ── Polling ──────────────────────────────────────────────────────────────
    POLL_DELAY: float = Field(
        default=0.5,
        description="Seconds between polls when the queue is empty.",
    )

    # ── Health check ─────────────────────────────────────────────────────────
    HEALTH_CHECK_INTERVAL: int = Field(
        default=30,
        description="Interval in seconds between Redis health pings.",
    )
    HEALTH_PORT: int = Field(
        default=8081,
        description="Port for the health check HTTP server (liveness / readiness).",
    )

    # ── Logging ──────────────────────────────────────────────────────────────
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )
    STRUCTLOG_FORMAT: Literal["json", "console"] = Field(
        default="json",
        description=(
            "Log format.  ``json`` for production (Loki ingestion), "
            "``console`` for human-readable development output."
        ),
    )

    # ── Structured Extraction ─────────────────────────────────────────────────
    STRUCTURED_EXTRACTION_MAX_TOKENS: int = Field(
        default=2000,
        ge=256,
        le=16384,
        description="Max tokens for structured extraction LLM calls.",
    )

    # ── Prometheus ───────────────────────────────────────────────────────────
    PROMETHEUS_PORT: int = Field(
        default=9090,
        description=(
            "Port for the Prometheus metrics HTTP server. "
            "Separate from the health check port."
        ),
    )

    # ── Derived properties ───────────────────────────────────────────────────

    @property
    def high_queue_full(self) -> str:
        """Fully qualified high-priority queue name (e.g. ``OpenZep:prod:queue:high``)."""
        return get_queue_name(self.ENV, self.HIGH_QUEUE_NAME)

    @property
    def low_queue_full(self) -> str:
        """Fully qualified low-priority queue name (e.g. ``OpenZep:prod:queue:low``)."""
        return get_queue_name(self.ENV, self.LOW_QUEUE_NAME)


# Module-level singleton — import this, never instantiate WorkerSettings directly.
settings = WorkerSettings()
