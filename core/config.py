"""Application configuration — populated exclusively from OpenBao at startup.

System-level configuration (database URLs, secrets, tunables) is stored in
OpenBao KV under the ``system/`` namespace and loaded at application startup
via :func:`init_settings`.  The :class:`Settings` singleton is not available
until :func:`init_settings` has been called.

There is **no** module-level ``Settings()`` instantiation at import time.
Access the singleton through :func:`get_settings` or the backward-compatible
``from core.config import settings`` pattern.

There is **no** env-var fallback.  If OpenBao is unreachable at startup, the
process fails fast with :class:`OpenBaoConnectionError`.

Usage::

    from core.config import BootstrapSettings, get_settings, init_settings
    from core.openbao import OpenBaoClient

    bootstrap = BootstrapSettings()
    async with OpenBaoClient(
        bootstrap.OPENBAO_ADDR,
        bootstrap.OPENBAO_ROLE_ID,
        bootstrap.OPENBAO_SECRET_ID,
    ) as bao:
        settings = await init_settings(bao)

    # At runtime:
    db_url = get_settings().DATABASE_URL
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Re-export init_settings so callers can do:
#   from core.config import init_settings, BootstrapSettings
# This import is safe at module level because ``core.openbao_settings`` only
# imports ``core.config`` inside function bodies (no circular dependency).
from core.openbao_settings import init_settings  # noqa: F401  — re-export

# ═══════════════════════════════════════════════════════════════════════════════
# Bootstrap settings (reaching OpenBao)
# ═══════════════════════════════════════════════════════════════════════════════


class BootstrapSettings(BaseSettings):
    """Minimal settings needed to reach OpenBao for the first time.

    These values are read from **actual environment variables only** — there
    is no ``.env`` file fallback.  They are never stored in OpenBao itself;
    they bootstrap the connection.

    In production, inject these via Docker environment variables, Kubernetes
    Secrets, or your infrastructure's secrets manager.

    Environment variables:
        OZ_OPENBAO_ADDR: OpenBao server URL (default ``http://localhost:8200``).
        OZ_OPENBAO_ROLE_ID: AppRole RoleID for authentication (required).
        OZ_OPENBAO_SECRET_ID: AppRole SecretID for authentication (required).
    """

    OPENBAO_ADDR: str = Field(
        default="http://localhost:8200",
        description="OpenBao server URL.",
        validation_alias="OZ_OPENBAO_ADDR",
    )
    OPENBAO_ROLE_ID: str = Field(
        description="AppRole RoleID for OpenBao authentication.",
        validation_alias="OZ_OPENBAO_ROLE_ID",
    )
    OPENBAO_SECRET_ID: str = Field(
        description="AppRole SecretID for OpenBao authentication.",
        validation_alias="OZ_OPENBAO_SECRET_ID",
    )
    OPENBAO_WORKER_ROLE_ID: str | None = Field(
        default=None,
        description="Optional worker-specific AppRole RoleID.",
        validation_alias="OZ_OPENBAO_WORKER_ROLE_ID",
    )
    OPENBAO_WORKER_SECRET_ID: str | None = Field(
        default=None,
        description="Optional worker-specific AppRole SecretID.",
        validation_alias="OZ_OPENBAO_WORKER_SECRET_ID",
    )

    model_config = SettingsConfigDict(
        extra="ignore",
        frozen=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Runtime settings model
# ═══════════════════════════════════════════════════════════════════════════════


class Settings(BaseModel):
    """Single source of truth for all OpenZync system configuration.

    Values are loaded from OpenBao KV (``system/`` namespace) at startup via
    :func:`init_settings`.  An instance is created once and exposed through
    :func:`get_settings`.  Do **not** instantiate ``Settings`` manually.

    Secrets (``DATABASE_URL``, ``REDIS_URL``, ``SECRET_KEY``,
    ``WEBHOOK_SIGNING_SECRET``) have **no defaults** — they are required and
    must be present in OpenBao.  Non-sensitive tunables have sensible defaults
    that can be overridden in OpenBao.

    .. note::

        Per-org configuration (LLM, embeddings, graph, behaviour) is **not**
        stored here.  Use ``core.org_config.get_org_config()`` for those
        values — they live in per-org OpenBao namespaces.
    """

    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        description="PostgreSQL connection string used by SQLAlchemy async engine.",
    )

    # ── Redis / Caching ───────────────────────────────────────────────────
    REDIS_URL: str = Field(
        description="Redis connection string for caching, pub/sub, and RQ/ARQ.",
    )

    # ── Secrets ───────────────────────────────────────────────────────────
    SECRET_KEY: str = Field(
        description=(
            "Secret key used for signing JWTs and other cryptographic "
            "operations.  Must be at least 32 characters in production."
        ),
        min_length=32,
    )
    WEBHOOK_SIGNING_SECRET: str = Field(
        description=(
            "Secret key for HMAC-SHA256 webhook signing. "
            "Must be at least 32 characters. "
            "Consumers use this to verify webhook authenticity."
        ),
        min_length=32,
    )

    # ── Metrics / Observability ───────────────────────────────────────────
    PROMETHEUS_URL: str = Field(
        default="http://localhost:9090",
        description="Prometheus server URL.  Used by the admin /metrics/summary endpoint.",
    )

    # ── HTTP / Server ─────────────────────────────────────────────────────
    CORS_ORIGINS: str = Field(
        default="http://localhost:3000",
        description="Comma-separated list of allowed CORS origins.",
    )
    HOSTS_ALLOWED: str = Field(
        default="localhost:8000",
        description=(
            "Comma-separated list of allowed Host header values for "
            "TrustedHostMiddleware in production "
            "(e.g. 'api.openzync.tech,localhost:3000'). "
            "Accepts '*' in development."
        ),
    )

    # ── Environment & Observability ───────────────────────────────────────
    ENVIRONMENT: str = Field(
        default="development",
        description="Deployment environment.  Controls logging format, etc.",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )

    # ── Concurrency ───────────────────────────────────────────────────────
    MAX_WORKERS: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Maximum number of worker threads/processes.",
    )

    # ── JWT ────────────────────────────────────────────────────────────────
    JWT_ACCESS_TOKEN_TTL_MINUTES: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="Access token TTL in minutes (default 30).",
    )
    JWT_REFRESH_TOKEN_TTL_DAYS: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Refresh token TTL in days (default 7).",
    )

    # ── FalkorDB (graph backend) ──────────────────────────────────────────
    FALKORDB_URL: str = Field(
        default="redis://localhost:6379",
        description=(
            "FalkorDB connection URL (Redis RESP protocol).  "
            "Defaults to localhost:6379."
        ),
    )
    FALKORDB_MAX_CONNECTIONS: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max connections in the FalkorDB connection pool.",
    )
    FALKORDB_SOCKET_TIMEOUT: int = Field(
        default=30,
        ge=1,
        description="Socket timeout in seconds for FalkorDB connections.",
    )

    # ── Rate Limiting ─────────────────────────────────────────────────────
    RATE_LIMIT_IP_MAX: int = Field(
        default=10,
        ge=1,
        description="Max requests per IP within the rate-limit window.",
    )
    RATE_LIMIT_WINDOW_SEC: int = Field(
        default=60,
        ge=1,
        description="Rate-limit window in seconds.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Singleton accessors
# ═══════════════════════════════════════════════════════════════════════════════

_settings: Settings | None = None
"""Module-level :class:`Settings` singleton, populated by a one-time init call."""


def set_settings(settings: Settings) -> None:
    """Store the :class:`Settings` singleton (called once by :func:`init_settings`).

    Args:
        settings: A fully-populated ``Settings`` instance from OpenBao.
    """
    global _settings  # noqa: PLW0603  — intentional module-level state
    _settings = settings


def get_settings() -> Settings:
    """Return the initialised :class:`Settings` singleton.

    Returns:
        The module-level singleton.

    Raises:
        RuntimeError: If :func:`init_settings` has not been called yet.
    """
    if _settings is None:
        raise RuntimeError(
            "Settings not initialised. Call init_settings(client) "
            "before get_settings().",
        )
    return _settings


# ═══════════════════════════════════════════════════════════════════════════════
# Backward-compatible ``from core.config import settings``
# ═══════════════════════════════════════════════════════════════════════════════
#
# ``settings`` is not a module-level variable anymore — it is resolved via
# ``__getattr__`` so that ``from core.config import settings`` still works
# (it calls ``get_settings()`` under the hood) while avoiding instantiation
# at import time.


def __getattr__(name: str) -> Any:
    """Resolve ``settings`` lazily through the singleton accessor.

    Args:
        name: The attribute name being looked up.

    Returns:
        The :class:`Settings` singleton if ``name == "settings"``.

    Raises:
        AttributeError: If the name is not recognised.
    """
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
