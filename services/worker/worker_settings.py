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

from typing import Any, Literal

from pydantic import BaseModel, Field

from core.openbao import OpenBaoClient


def get_queue_name(env: str, queue_type: str) -> str:
    """Generate a namespaced queue name.

    Pattern: ``OpenZync:{env}:queue:{queue_type}``

    Examples:
        ``OpenZync:dev:queue:high``
        ``OpenZync:prod:queue:low``

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
    return f"OpenZync:{env}:queue:{queue_type}"


class WorkerSettings(BaseModel):
    """Worker-specific configuration — populated from OpenBao or env vars.

    All durations are in **seconds** unless otherwise noted.

    Create via :meth:`from_openbao` for production, or instantiate directly
    for local development / testing.
    """

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        description=(
            "PostgreSQL connection string used by SQLAlchemy async engine. "
            "Must use the ``postgresql+asyncpg://`` scheme. "
            "Loaded from OpenBao — no default."
        ),
    )

    # ── Redis ────────────────────────────────────────────────────────────────
    REDIS_URL: str = Field(
        description=(
            "Redis connection string for the ARQ job queue. "
            "Loaded from OpenBao — no default."
        ),
    )

    # ── Environment ──────────────────────────────────────────────────────────
    ENV: str = Field(
        default="development",
        description=(
            "Environment name used in queue name prefix: "
            "``OpenZync:{env}:queue:{queue_name}``."
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
        default=9095,
        description=(
            "Port for the Prometheus metrics HTTP server. "
            "Separate from the health check port."
        ),
    )

    # ── FalkorDB (graph backend) ─────────────────────────────────────────────
    FALKORDB_URL: str = Field(
        default="redis://localhost:6379",
        description=(
            "FalkorDB connection URL (Redis RESP protocol). "
            "Defaults to localhost:6379."
        ),
    )
    FALKORDB_MAX_CONNECTIONS: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Max connections in the FalkorDB connection pool.",
    )
    FALKORDB_SOCKET_TIMEOUT: int = Field(
        default=10,
        ge=1,
        description="Socket timeout in seconds for FalkorDB connections.",
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
    # OpenBao factory
    # ═════════════════════════════════════════════════════════════════════════

    @classmethod
    async def from_openbao(cls, bao_client: OpenBaoClient) -> WorkerSettings:
        """Create a ``WorkerSettings`` by reading system config from OpenBao.

        Maps OpenBao snake_case keys to ``WorkerSettings`` field names.
        Queue names, timeouts, and ports that are deployment-specific are
        kept at their defaults (or can be overridden via env vars).

        Args:
            bao_client: An authenticated :class:`OpenBaoClient` instance.

        Returns:
            A fully-populated :class:`WorkerSettings` instance.
        """
        config = await bao_client.read_system_config()

        # Map OpenBao key names → WorkerSettings field names (lowercase)
        mapping: dict[str, str] = {
            "database_url": "DATABASE_URL",
            "redis_url": "REDIS_URL",
            "environment": "ENV",
            "log_level": "LOG_LEVEL",
            "falkordb_url": "FALKORDB_URL",
            "falkordb_max_connections": "FALKORDB_MAX_CONNECTIONS",
            "falkordb_socket_timeout": "FALKORDB_SOCKET_TIMEOUT",
            "max_workers": "MAX_WORKERS",
        }
        int_fields = {"MAX_WORKERS", "FALKORDB_MAX_CONNECTIONS", "FALKORDB_SOCKET_TIMEOUT"}

        kwargs: dict[str, Any] = {}
        for bao_key, field_name in mapping.items():
            value = config.get(bao_key)
            if value is not None:
                key = field_name.lower()
                kwargs[key] = int(value) if field_name in int_fields else value

        # Deployment-specific defaults (env-alterable but not stored in OpenBao)
        kwargs.setdefault("JOB_TIMEOUT_DEFAULT", 300)
        kwargs.setdefault("HIGH_QUEUE_NAME", "high")
        kwargs.setdefault("LOW_QUEUE_NAME", "low")

        return cls(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# Deferred singleton
# ═══════════════════════════════════════════════════════════════════════════════

_settings: WorkerSettings | None = None
"""Module-level singleton, populated by :func:`init_worker_settings_from_bao`."""


def get_worker_settings() -> WorkerSettings:
    """Return the initialised :class:`WorkerSettings` singleton.

    Returns:
        The module-level singleton instance.

    Raises:
        RuntimeError: If :func:`init_worker_settings_from_bao` has not
            been called yet.
    """
    if _settings is None:
        raise RuntimeError(
            "WorkerSettings not initialised. Call init_worker_settings_from_bao() "
            "before get_worker_settings().",
        )
    return _settings


async def init_worker_settings_from_bao(bao_client: OpenBaoClient) -> WorkerSettings:
    """Initialise the global :class:`WorkerSettings` singleton from OpenBao.

    Must be called once at worker startup, before any task is dispatched.

    Args:
        bao_client: An authenticated :class:`OpenBaoClient` instance.

    Returns:
        The newly-created :class:`WorkerSettings` instance (also stored
        as the module-level singleton).
    """
    global _settings  # noqa: PLW0603  — intentional module-level state

    ws = await WorkerSettings.from_openbao(bao_client)
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
# must be initialised via ``init_worker_settings_from_bao()`` before any usage.
settings = _SettingsProxy()  # type: ignore[assignment]
