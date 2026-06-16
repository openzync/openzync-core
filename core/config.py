"""Application configuration via pydantic-settings.

All environment variables are read at startup through a single Settings
singleton.  Every configurable value lives here — never hardcode secrets,
URLs, or tunables in application code.

Usage:
    from core.config import settings

    db_url = settings.DATABASE_URL
    redis_url = settings.REDIS_URL
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic.networks import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of truth for all OpenZep configuration.

    Values are read from environment variables (prefixed with ``MG_``) or a
    ``.env`` file.  An instance is created once at import time and reused
    throughout the application — import ``settings`` from this module, do not
    instantiate ``Settings`` yourself.

    .. note::

        **Per-org config (Groups A, B, C) moved to DB**

        Settings that were previously environment variables for LLM,
        Embeddings, Graph, and Behaviour are now stored per-organization
        in the ``organizations.config`` JSONB column and managed through
        the org-config UI/API.  Use ``core.org_config.get_org_config()``
        for those values — the corresponding fields have been removed
        from this class.
    """

    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: PostgresDsn = Field(
        description="PostgreSQL connection string used by SQLAlchemy async engine.",
        validation_alias="MG_DATABASE_URL",
    )

    # ── Redis / Caching ───────────────────────────────────────────────────
    REDIS_URL: RedisDsn = Field(
        description="Redis connection string for caching, pub/sub, and RQ/ARQ.",
        validation_alias="MG_REDIS_URL",
    )

    # ── Secrets ───────────────────────────────────────────────────────────
    SECRET_KEY: str = Field(
        description=(
            "Secret key used for signing JWTs and other cryptographic "
            "operations.  Must be at least 32 characters in production."
        ),
        validation_alias="MG_SECRET_KEY",
        min_length=32,
    )

    # ── Metrics / Observability ───────────────────────────────────────────
    PROMETHEUS_URL: str = Field(
        default="http://localhost:9090",
        description="Prometheus server URL.  Used by the admin /metrics/summary endpoint.",
        validation_alias="MG_PROMETHEUS_URL",
    )

    # ── HTTP / Server ─────────────────────────────────────────────────────
    CORS_ORIGINS: str = Field(
        default="http://localhost:3000",
        description="Comma-separated list of allowed CORS origins.",
        validation_alias="MG_CORS_ORIGINS",
    )
    HOSTS_ALLOWED: str = Field(
        default="localhost:8000",
        description=(
            "Comma-separated list of allowed Host header values for "
            "TrustedHostMiddleware in production "
            "(e.g. 'api.openzep.dev,localhost:3000'). "
            "Accepts '*' in development."
        ),
        validation_alias="MG_HOSTS_ALLOWED",
    )

    # ── Environment & Observability ───────────────────────────────────────
    ENVIRONMENT: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Deployment environment.  Controls logging format, etc.",
        validation_alias="MG_ENVIRONMENT",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
        validation_alias="MG_LOG_LEVEL",
    )

    # ── Concurrency ───────────────────────────────────────────────────────
    MAX_WORKERS: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Maximum number of worker threads/processes.",
        validation_alias="MG_MAX_WORKERS",
    )

    # ── JWT ────────────────────────────────────────────────────────────────
    JWT_ACCESS_TOKEN_TTL_MINUTES: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="Access token TTL in minutes (default 30).",
        validation_alias="MG_JWT_ACCESS_TOKEN_TTL_MINUTES",
    )
    JWT_REFRESH_TOKEN_TTL_DAYS: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Refresh token TTL in days (default 7).",
        validation_alias="MG_JWT_REFRESH_TOKEN_TTL_DAYS",
    )

    # ── Webhooks ───────────────────────────────────────────────────────────
    WEBHOOK_SIGNING_SECRET: str = Field(
        default="dev-webhook-signing-secret-change-in-production",
        description=(
            "Secret key for HMAC-SHA256 webhook signing. "
            "Must be at least 32 characters. "
            "Consumers use this to verify webhook authenticity."
        ),
        validation_alias="MG_WEBHOOK_SIGNING_SECRET",
        min_length=32,
    )

    # ── Rate Limiting ─────────────────────────────────────────────────────
    RATE_LIMIT_IP_MAX: int = Field(
        default=10,
        ge=1,
        description="Max requests per IP within the rate-limit window.",
        validation_alias="MG_RATE_LIMIT_IP_MAX",
    )
    RATE_LIMIT_WINDOW_SEC: int = Field(
        default=60,
        ge=1,
        description="Rate-limit window in seconds.",
        validation_alias="MG_RATE_LIMIT_WINDOW_SEC",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )


# Module-level singleton — import this, never instantiate Settings directly.
settings = Settings()  # type: ignore[call-arg]
