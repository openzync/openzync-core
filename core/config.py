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

import base64
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
            "Legacy secret key used for various cryptographic operations. "
            "Must be at least 32 characters in production."
        ),
        validation_alias="MG_SECRET_KEY",
        min_length=32,
    )
    # Master encryption key for the secret store (Fernet-compatible, 44-char
    # base64-encoded 32-byte key).  Used to encrypt API keys and passwords
    # at rest in the org config.
    # Generate with: python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"
    MASTER_ENCRYPTION_KEY: str = Field(
        default="",
        description=(
            "Master encryption key for the secret store — 44-char "
            "base64-encoded 32-byte Fernet key.  Required for encrypting "
            "API keys and passwords at rest."
        ),
        validation_alias="MG_MASTER_ENCRYPTION_KEY",
    )
    SECRET_STORE_BACKEND: str = Field(
        default="fernet",
        description=(
            "Active secret store backend.  Currently supported: 'fernet'. "
            "Future options: 'vault', 'kms'."
        ),
        validation_alias="MG_SECRET_STORE_BACKEND",
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
    # ES256 private key — base64-encoded PEM (P-256 EC private key).
    # Generate:
    #   openssl ecparam -name prime256v1 -genkey -noout -out private.pem
    #   base64 -w0 private.pem   # → put in MG_JWT_PRIVATE_KEY_B64
    JWT_PRIVATE_KEY: str = Field(
        default="",
        description=(
            "ES256 private key — base64-encoded PEM (P-256 EC). "
            "Generate with openssl ecparam and encode with base64 -w0."
        ),
        validation_alias="MG_JWT_PRIVATE_KEY_B64",
    )
    # ES256 public key — base64-encoded PEM (P-256 EC public key).
    # Generate:
    #   openssl ec -in private.pem -pubout -out public.pem
    #   base64 -w0 public.pem   # → put in MG_JWT_PUBLIC_KEY_B64
    JWT_PUBLIC_KEY: str = Field(
        default="",
        description=(
            "ES256 public key — base64-encoded PEM (P-256 EC). "
            "Generate with openssl ec and encode with base64 -w0."
        ),
        validation_alias="MG_JWT_PUBLIC_KEY_B64",
    )

    # ── Computed properties ─────────────────────────────────────────────────

    @property
    def jwt_private_key_pem(self) -> str | None:
        """Decoded ES256 private key PEM.

        Base64-decoded from ``JWT_PRIVATE_KEY`` (``MG_JWT_PRIVATE_KEY_B64``).
        Returns ``None`` if no private key is configured.

        Usage::

            key = settings.jwt_private_key_pem
            if key is None:
                raise RuntimeError("MG_JWT_PRIVATE_KEY_B64 not configured")
        """
        if not self.JWT_PRIVATE_KEY:
            return None
        return base64.b64decode(self.JWT_PRIVATE_KEY).decode("utf-8")

    @property
    def jwt_public_key_pem(self) -> str | None:
        """Decoded ES256 public key PEM.

        Base64-decoded from ``JWT_PUBLIC_KEY`` (``MG_JWT_PUBLIC_KEY_B64``).
        Returns ``None`` if no public key is configured.
        """
        if not self.JWT_PUBLIC_KEY:
            return None
        return base64.b64decode(self.JWT_PUBLIC_KEY).decode("utf-8")

    # ── Email / SMTP ─────────────────────────────────────────────────────────
    SMTP_HOST: str = Field(
        default="",
        description="SMTP server hostname (e.g. smtp.sendgrid.net).",
        validation_alias="MG_SMTP_HOST",
    )
    SMTP_PORT: int = Field(
        default=587,
        ge=1,
        le=65535,
        description="SMTP server port (default 587 for STARTTLS).",
        validation_alias="MG_SMTP_PORT",
    )
    SMTP_USERNAME: str = Field(
        default="",
        description="SMTP authentication username.",
        validation_alias="MG_SMTP_USERNAME",
    )
    SMTP_PASSWORD: str = Field(
        default="",
        description="SMTP authentication password.",
        validation_alias="MG_SMTP_PASSWORD",
    )
    SMTP_FROM_EMAIL: str = Field(
        default="noreply@openzep.dev",
        description="From address for outgoing emails.",
        validation_alias="MG_SMTP_FROM_EMAIL",
    )
    APP_BASE_URL: str = Field(
        default="http://localhost:8000",
        description=(
            "Public base URL of the application, used to construct "
            "verification links in emails.  E.g. 'https://app.openzep.dev'."
        ),
        validation_alias="MG_APP_BASE_URL",
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

    # ── FalkorDB (graph backend) ──────────────────────────────────────────
    FALKORDB_URL: str = Field(
        default="redis://localhost:6379",
        description=(
            "FalkorDB connection URL (Redis RESP protocol).  "
            "Defaults to localhost:6379."
        ),
        validation_alias="MG_FALKORDB_URL",
    )
    FALKORDB_MAX_CONNECTIONS: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max connections in the FalkorDB connection pool.",
        validation_alias="MG_FALKORDB_MAX_CONNECTIONS",
    )
    FALKORDB_SOCKET_TIMEOUT: int = Field(
        default=30,
        ge=1,
        description="Socket timeout in seconds for FalkorDB connections.",
        validation_alias="MG_FALKORDB_SOCKET_TIMEOUT",
    )

    # ── Rate Limiting ─────────────────────────────────────────────────────
    RATE_LIMIT_IP_MAX: int = Field(
        default=10,
        ge=1,
        description=(
            "Max requests per IP within the rate-limit window. "
            "Default is 10 requests per 60-second window."
        ),
        validation_alias="MG_RATE_LIMIT_IP_MAX",
    )
    RATE_LIMIT_WINDOW_SEC: int = Field(
        default=60,
        ge=1,
        description=(
            "Rate-limit window in seconds. "
            "Default is 60 seconds (paired with MG_RATE_LIMIT_IP_MAX)."
        ),
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
