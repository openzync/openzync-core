"""Worker-specific configuration — read directly from ``OZ_*`` env vars.

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

from typing import Any, Literal

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.queue_names import get_queue_name

# The queue-name builder lives in ``core.queue_names`` — the API enqueues
# into the same queues the worker consumes, so it must not import from the
# worker package.  It is re-exported here because it is part of this
# module's public surface.


class WorkerSettings(BaseSettings):
    """Worker-specific configuration — read directly from ``OZ_*`` env vars.

    All durations are in **seconds** unless otherwise noted.

    Instantiate directly (env vars) or via :func:`get_worker_settings`
    after :func:`init_worker_settings` has run.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        description=(
            "PostgreSQL connection string used by SQLAlchemy async engine. "
            "Must use the ``postgresql+asyncpg://`` scheme. "
            "Loaded from the environment (``OZ_DATABASE_URL``) — no default."
        ),
        validation_alias="OZ_DATABASE_URL",
    )
    DB_POOL_SIZE: int = Field(
        default=4,
        ge=1,
        le=100,
        description=(
            "SQLAlchemy pool size per worker process.  Sizing rule: "
            "replicas x (pool_size + max_overflow) < Postgres max_connections.  "
            "Must match core Settings — 4 + 1 keeps 15 Helm replicas at 75."
        ),
        validation_alias="OZ_DB_POOL_SIZE",
    )
    DB_MAX_OVERFLOW: int = Field(
        default=1,
        ge=0,
        le=100,
        description=(
            "Extra connections a worker process may open above "
            "DB_POOL_SIZE before tasks queue."
        ),
        validation_alias="OZ_DB_MAX_OVERFLOW",
    )

    # ── Redis ────────────────────────────────────────────────────────────────
    REDIS_URL: str = Field(
        description=(
            "Redis connection string for the ARQ job queue. "
            "Loaded from the environment (``OZ_REDIS_URL``) — no default."
        ),
        validation_alias="OZ_REDIS_URL",
    )

    # ── Environment ──────────────────────────────────────────────────────────
    ENV: str = Field(
        default="development",
        description=(
            "Environment name used in queue name prefix: "
            "``OpenZync:{env}:queue:{queue_name}``."
        ),
        validation_alias="OZ_ENVIRONMENT",
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
        validation_alias="OZ_MAX_WORKERS",
    )
    LOW_MAX_WORKERS: int = Field(
        default=2,
        ge=2,
        le=32,
        description=(
            "Concurrency floor for the low-priority queue worker. "
            "The low queue holds 12 batch tasks — a floor below 2 starves it "
            "whenever MAX_WORKERS is small. Floor is contractually 2."
        ),
        validation_alias="OZ_LOW_MAX_WORKERS",
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
        default=14_400,
        description=(
            "Seconds to keep FAILED job results in Redis (4h — down from 24h; "
            "DB 0 shares the 128MB noeviction budget with live queue keys). "
            "Wired in worker.on_job_end, which re-expires failed results to "
            "this TTL (ARQ itself keeps one TTL for both outcomes)."
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
        validation_alias="OZ_LOG_LEVEL",
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
        default=9095,
        description=(
            "Port for the Prometheus metrics HTTP server. "
            "Separate from the health check port."
        ),
    )

    # ── FalkorDB (graph backend) ─────────────────────────────────────────────
    FALKORDB_URL: str | None = Field(
        default=None,
        description=(
            "FalkorDB connection URL (Redis RESP protocol).  "
            "When set at system level, orgs share a single FalkorDB instance.  "
            "When None (default), per-org falkordb_url from org_config may be used."
        ),
        validation_alias="OZ_FALKORDB_URL",
    )
    FALKORDB_MAX_CONNECTIONS: int = Field(
        default=10,
        ge=1,
        le=100,
        description=(
            "Max connections in the FalkorDB connection pool.  Must match "
            "core Settings (single canonical value — never hardcode a pool "
            "size at a call site; 15 max Helm replicas x 10 = 150 worst-case)."
        ),
        validation_alias="OZ_FALKORDB_MAX_CONNECTIONS",
    )
    FALKORDB_SOCKET_TIMEOUT: int = Field(
        default=10,
        ge=1,
        description="Socket timeout in seconds for FalkorDB connections.",
        validation_alias="OZ_FALKORDB_SOCKET_TIMEOUT",
    )

    # ── SurrealDB (graph backend — optional system-level) ────────────────────
    SURREALDB_URL: str | None = Field(
        default=None,
        description=(
            "System-level SurrealDB WebSocket connection URL "
            "(e.g. ws://surrealdb:8000/rpc).  "
            "When set, all orgs use this server and namespace is auto-derived "
            "from org identity.  When None (default), per-org surrealdb_* "
            "fields from org_config are used."
        ),
        validation_alias="OZ_SURREALDB_URL",
    )

    # ── Community Detection ──────────────────────────────────────────────────
    AUTO_RUN_COMMUNITY_DETECTION: bool = Field(
        default=False,
        description=(
            "If True: enqueues summarise_community after each link_entities_to_episode "
            "completion (with per-org Redis dedup \u2013 max once per hour per org). "
            "If False (default): nightly cron at 02:00 UTC."
        ),
    )

    # ── Derived properties ───────────────────────────────────────────────────

    @property
    def high_queue_full(self) -> str:
        """Fully qualified high-priority queue name (e.g. ``OpenZync:prod:queue:high``)."""
        return get_queue_name(self.ENV, self.HIGH_QUEUE_NAME)

    @property
    def low_queue_full(self) -> str:
        """Fully qualified low-priority queue name (e.g. ``OpenZync:prod:queue:low``)."""
        return get_queue_name(self.ENV, self.LOW_QUEUE_NAME)

    # ═════════════════════════════════════════════════════════════════════════
    # Env factory
    # ═════════════════════════════════════════════════════════════════════════

    @classmethod
    def from_env(cls) -> WorkerSettings:
        """Create a ``WorkerSettings`` by reading ``OZ_*`` env vars.

        Returns:
            A fully-populated :class:`WorkerSettings` instance.
        """
        return cls()  # type: ignore[call-arg]


# ═══════════════════════════════════════════════════════════════════════════════
# Deferred singleton
# ═══════════════════════════════════════════════════════════════════════════════

_settings: WorkerSettings | None = None
"""Module-level singleton, populated by :func:`init_worker_settings`."""


def get_worker_settings() -> WorkerSettings:
    """Return the initialised :class:`WorkerSettings` singleton.

    Returns:
        The module-level singleton instance.

    Raises:
        RuntimeError: If :func:`init_worker_settings` has not
            been called yet.
    """
    if _settings is None:
        raise RuntimeError(
            "WorkerSettings not initialised. Call init_worker_settings() "
            "before get_worker_settings().",
        )
    return _settings


def init_worker_settings() -> WorkerSettings:
    """Initialise the global :class:`WorkerSettings` singleton from env.

    Must be called once at worker startup, before any task is dispatched.

    Returns:
        The newly-created :class:`WorkerSettings` instance (also stored
        as the module-level singleton).
    """
    global _settings  # noqa: PLW0603  — intentional module-level state

    ws = WorkerSettings.from_env()
    _settings = ws
    return ws


class _SettingsProxy:
    """Transparent proxy for backward-compatible ``settings.*`` access.

    Existing code that imports ``settings`` from this module continues
    to work.  Attribute access is forwarded to :func:`get_worker_settings`.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_worker_settings(), name)


# Module-level singleton — import this, never instantiate WorkerSettings directly.
# NOTE: ``settings`` is a proxy, not a ``WorkerSettings`` instance.  It resolves
# attribute access at runtime via ``get_worker_settings()``, so the singleton
# must be initialised via ``init_worker_settings()`` before any usage.
settings = _SettingsProxy()  # type: ignore[assignment]
