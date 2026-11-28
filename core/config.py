"""Application configuration — read directly from ``OZ_*`` environment variables.

System-level configuration (database URLs, secrets, tunables) is a
pydantic-settings :class:`Settings` BaseSettings: every field binds to its
``OZ_*`` env var via ``validation_alias``.  The singleton is created lazily
on first :func:`get_settings` call and fails fast when ``OZ_SECRET_KEY``
(< 32 chars) or ``OZ_DATABASE_URL`` is missing.

Per-user configuration (LLM, embeddings, graph, behaviour) is **not**
stored here — use ``core.platform_config.get_platform_config()`` for those values
(they live in ``users.config_override``).  Platform-wide non-secret
tunables live in the platform root user's ``config_override`` — see
``core.system_config``.

Usage::

    from core.config import get_settings

    # At runtime:
    db_url = get_settings().DATABASE_URL
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Platform constants
# ═══════════════════════════════════════════════════════════════════════════════

PLATFORM_ORG_ID = UUID("00000000-0000-0000-0000-0000000000aa")
"""Fixed UUID of the platform (root admin) user scope.

The platform user scope (``name="SYSTEM"``) and its root ``admin`` user
(``external_id='root'``) are created exactly once via
``POST /v1/auth/setup`` (see ``services.setup_service``) — never at
startup.  A JWT session is root admin only when its ``user_id`` claim
equals this UUID AND the DB-verified role is ``admin`` (see
``dependencies.auth``).  Fixed — never generated — so auth checks are
deterministic and the setup is idempotent.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Runtime settings model
# ═══════════════════════════════════════════════════════════════════════════════


class Settings(BaseSettings):
    """Single source of truth for all OpenZync system configuration.

    Values are read from ``OZ_*`` environment variables.  ``SECRET_KEY``
    (min 32 chars) and ``DATABASE_URL`` have **no defaults** — the process
    fails fast at startup when they are missing.  Non-sensitive tunables
    have sensible defaults overridable via env.

    .. note::

        Per-user configuration (LLM, embeddings, graph, behaviour) is **not**
        stored here.  Use ``core.platform_config.get_platform_config()`` for those
        values — they live in ``users.config_override``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        description="PostgreSQL connection string used by SQLAlchemy async engine.",
        validation_alias="OZ_DATABASE_URL",
    )
    DB_POOL_SIZE: int = Field(
        default=4,
        ge=1,
        le=100,
        description=(
            "SQLAlchemy pool size per process.  Sizing rule: "
            "replicas x (pool_size + max_overflow) < Postgres max_connections.  "
            "The default (4 + 1) keeps the Helm maximum (10 API + 5 worker "
            "replicas) at 75 connections, inside the Postgres default of 100."
        ),
        validation_alias="OZ_DB_POOL_SIZE",
    )
    DB_MAX_OVERFLOW: int = Field(
        default=1,
        ge=0,
        le=100,
        description=(
            "Extra connections a process may open above DB_POOL_SIZE before "
            "requests queue.  Counted in the replicas x (pool_size + "
            "max_overflow) < max_connections budget."
        ),
        validation_alias="OZ_DB_MAX_OVERFLOW",
    )

    # ── Redis / Caching ───────────────────────────────────────────────────
    REDIS_URL: str = Field(
        description="Redis connection string for caching, pub/sub, and RQ/ARQ.",
        validation_alias="OZ_REDIS_URL",
    )

    # ── Secrets ───────────────────────────────────────────────────────────
    SECRET_KEY: str = Field(
        description=(
            "Secret key used for signing JWTs and deriving encryption "
            "subkeys (see core.crypto).  Must be at least 32 characters."
        ),
        min_length=32,
        validation_alias="OZ_SECRET_KEY",
    )
    SECRET_KEY_PREVIOUS: str | None = Field(
        default=None,
        description=(
            "Previous OZ_SECRET_KEY value — decrypt-only, for the rotation "
            "window.  Never used for encryption."
        ),
        validation_alias="OZ_SECRET_KEY_PREVIOUS",
    )
    # ── Metrics / Observability ───────────────────────────────────────────
    PROMETHEUS_URL: str = Field(
        default="http://localhost:9090",
        description="Prometheus server URL.  Used by the admin /metrics/summary endpoint.",
        validation_alias="OZ_PROMETHEUS_URL",
    )

    # ── HTTP / Server ─────────────────────────────────────────────────────
    CORS_ORIGINS: str = Field(
        default="http://localhost:3000",
        description="Comma-separated list of allowed CORS origins.",
        validation_alias="OZ_CORS_ORIGINS",
    )
    FRONTEND_URL: str = Field(
        default="http://localhost:3000",
        description=(
            "Base URL of the frontend web app, used for invite email links "
            "(e.g. ``{FRONTEND_URL}/invite?token=...``)."
        ),
        validation_alias="OZ_FRONTEND_URL",
    )
    HOSTS_ALLOWED: str = Field(
        default="localhost:8000",
        description=(
            "Comma-separated list of allowed Host header values for "
            "TrustedHostMiddleware in production "
            "(e.g. 'api.openzync.tech,localhost:3000'). "
            "Accepts '*' in development."
        ),
        validation_alias="OZ_HOSTS_ALLOWED",
    )
    TRUSTED_PROXY_IPS: str = Field(
        default="127.0.0.1",
        description=(
            "Peers whose X-Forwarded-For header is trusted as the client IP. "
            "Consumed by uvicorn (--forwarded-allow-ips) so that the "
            "per-IP rate limiters and the audit log record the real client "
            "instead of the reverse proxy.  A CIDR range is allowed. "
            "'*' is rejected — trusting every peer lets a client forge its "
            "own IP and bypass the rate limiters."
        ),
        validation_alias="OZ_TRUSTED_PROXY_IPS",
    )

    # ── Environment & Observability ───────────────────────────────────────
    ENVIRONMENT: str = Field(
        default="development",
        description="Deployment environment.  Controls logging format, etc.",
        validation_alias="OZ_ENVIRONMENT",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
        validation_alias="OZ_LOG_LEVEL",
    )

    # ── Concurrency ───────────────────────────────────────────────────────
    MAX_WORKERS: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Maximum number of worker threads/processes.",
        validation_alias="OZ_MAX_WORKERS",
    )
    LOW_MAX_WORKERS: int = Field(
        default=2,
        ge=2,
        le=32,
        description="Concurrency floor for the low-priority queue worker.",
        validation_alias="OZ_LOW_MAX_WORKERS",
    )

    # ── JWT ────────────────────────────────────────────────────────────────
    JWT_ACCESS_TOKEN_TTL_MINUTES: int = Field(
        default=30,
        ge=1,
        le=1440,
        description="Access token TTL in minutes (default 30).",
        validation_alias="OZ_JWT_ACCESS_TOKEN_TTL_MINUTES",
    )
    JWT_REFRESH_TOKEN_TTL_DAYS: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Refresh token TTL in days (default 7).",
        validation_alias="OZ_JWT_REFRESH_TOKEN_TTL_DAYS",
    )

    # ── FalkorDB (graph backend — REQUIRED, system-level) ───────────────────
    FALKORDB_URL: str = Field(
        description=(
            "FalkorDB connection URL (Redis RESP protocol, e.g. "
            "redis://falkordb:6379). REQUIRED — the default graph backend "
            "is falkordb; the process fails fast at startup when unset, "
            "like OZ_DATABASE_URL. No per-user override — graph connection "
            "details live only in .env."
        ),
        validation_alias="OZ_FALKORDB_URL",
    )
    FALKORDB_MAX_CONNECTIONS: int = Field(
        default=10,
        ge=1,
        le=100,
        description=(
            "Max connections in the FalkorDB connection pool.  THE single "
            "canonical value — the worker and every per-request fallback "
            "pool reuse this; never hardcode a pool size at a call site.  "
            "Budget: 15 max Helm replicas x 10 = 150 worst-case, inside "
            "an unknown external FalkorDB maxclients (compose pins none)."
        ),
        validation_alias="OZ_FALKORDB_MAX_CONNECTIONS",
    )
    FALKORDB_SOCKET_TIMEOUT: int = Field(
        default=30,
        ge=1,
        description="Socket timeout in seconds for FalkorDB connections.",
        validation_alias="OZ_FALKORDB_SOCKET_TIMEOUT",
    )

    # ── SurrealDB (graph backend — optional system-level) ─────────────────
    # ⚠️ BREAKING: all five SurrealDB connection values live only in .env —
    # removed from platform_config with no per-user override.
    SURREALDB_URL: str | None = Field(
        default=None,
        description=(
            "SurrealDB WebSocket connection URL "
            "(e.g. ws://surrealdb:8000/rpc).  Optional — unset means the "
            "surrealdb graph backend is unavailable; selecting "
            "graph_backend='surrealdb' without it raises "
            "GraphBackendUnavailableError (loud, no fallback)."
        ),
        validation_alias="OZ_SURREALDB_URL",
    )
    SURREALDB_USER: str = Field(
        default="root",
        description="SurrealDB authentication username.",
        validation_alias="OZ_SURREALDB_USER",
    )
    SURREALDB_PASS: str = Field(
        default="root",
        description="SurrealDB authentication password.",
        validation_alias="OZ_SURREALDB_PASS",
    )
    SURREALDB_NAMESPACE: str = Field(
        default="openzync",
        description="SurrealDB namespace (tenant isolation boundary).",
        validation_alias="OZ_SURREALDB_NAMESPACE",
    )
    SURREALDB_DATABASE: str = Field(
        default="openzync",
        description="SurrealDB database within the namespace.",
        validation_alias="OZ_SURREALDB_DATABASE",
    )

    # ── Embeddings (Ollama — sole embedder) ─────────────────────────────
    OLLAMA_EMBED_URL: str = Field(
        default="http://ollama:11434",
        description=(
            "Base URL of the Ollama server serving the sole embedding model "
            "(nomic-embed-text:v1.5 via /api/embed). System-level only — no "
            "per-user override, no model selection."
        ),
        validation_alias="OZ_OLLAMA_EMBED_URL",
    )

    # ── Prompt Caching ────────────────────────────────────────────────────
    PROMPT_CACHING_ENABLED: bool = Field(
        default=True,
        description=(
            "Master switch for provider-side prompt caching. "
            "When True, Anthropic gets cache_control markers on system prompts, "
            "and OpenAI/Azure get automatic prefix caching."
        ),
        validation_alias="OZ_PROMPT_CACHING_ENABLED",
    )
    PROMPT_CACHING_ANTHROPIC_MIN_TOKENS: int = Field(
        default=1024, ge=512,
        description=(
            "Minimum estimated token count for the system prompt before "
            "cache_control is applied to Anthropic calls. "
            "Approximated as len(text) // 4. "
            "Models vary: 1024 covers Sonnet 4.6, 4096 covers Opus 4.6."
        ),
        validation_alias="OZ_PROMPT_CACHING_ANTHROPIC_MIN_TOKENS",
    )
    PROMPT_CACHING_ANTHROPIC_TTL: str = Field(
        default="5m",
        description=(
            "Anthropic cache TTL: '5m' (1.25x write cost, default) "
            "or '1h' (2x write cost). Cache reads are always 0.1x. "
            "Invalid values fall back to '5m'."
        ),
        validation_alias="OZ_PROMPT_CACHING_ANTHROPIC_TTL",
    )

    # ── Validators ────────────────────────────────────────────────────────

    @field_validator("TRUSTED_PROXY_IPS")
    @classmethod
    def _reject_wildcard_trusted_proxies(cls, v: str) -> str:
        """Reject a wildcard trusted-proxy list.

        Trusting every peer means any client can set ``X-Forwarded-For``
        and pick its own rate-limit bucket, which defeats the per-IP
        limiters.  Only explicit addresses or CIDR ranges are allowed.

        Args:
            v: The configured trusted-proxy list.

        Returns:
            The validated value.

        Raises:
            ValueError: If the list contains a wildcard.
        """
        if "*" in v:
            raise ValueError(
                "TRUSTED_PROXY_IPS must not contain '*' — list the proxy "
                "addresses or CIDR ranges explicitly"
            )
        return v

    # ⚠️ BREAKING: a "*" CORS origin is now a startup error, not a
    # permissive default. main.py always sets allow_credentials=True, and
    # browsers reject "*" + credentials — the old value silently broke
    # credentialed requests while looking open. List explicit origins.
    @field_validator("CORS_ORIGINS")
    @classmethod
    def _reject_wildcard_cors_origins(cls, v: str) -> str:
        """Reject a wildcard CORS origin while credentials are allowed.

        ``main.py`` serves CORS with ``allow_credentials=True`` (the
        dashboard sends ``Authorization``), under which a ``"*"`` origin
        is both rejected by browsers and — where tolerated — a credential
        leak to any site.  Only explicit origins are allowed.

        Args:
            v: The configured comma-separated origin list.

        Returns:
            The validated value.

        Raises:
            ValueError: If the list contains a wildcard.
        """
        if "*" in [o.strip() for o in v.split(",") if o.strip()]:
            raise ValueError(
                "CORS_ORIGINS must not contain '*' while "
                "allow_credentials is True — list the allowed origins "
                "explicitly"
            )
        return v

    @field_validator("PROMPT_CACHING_ANTHROPIC_TTL", mode="before")
    @classmethod
    def _validate_cache_ttl(cls, v: str) -> str:
        """Validate cache TTL, falling back to default on invalid input.

        Env vars have no schema enforcement — a manual typo could write
        any value. Rather than crashing the application on a Pydantic
        ``ValidationError``, we silently fall back to the default
        of ``"5m"``.
        """
        if v in ("5m", "1h"):
            return v
        logger.warning(
            "Invalid PROMPT_CACHING_ANTHROPIC_TTL=%r, falling back to '5m'",
            v,
        )
        return "5m"

    # ── Email / SMTP ──────────────────────────────────────────────────────
    SMTP_HOST: str = Field(
        default="localhost",
        description="SMTP server hostname for sending transactional emails.",
        validation_alias="OZ_SMTP_HOST",
    )
    SMTP_PORT: int = Field(
        default=587,
        ge=1,
        le=65535,
        description="SMTP server port (default 587 for STARTTLS).",
        validation_alias="OZ_SMTP_PORT",
    )
    SMTP_USERNAME: str = Field(
        default="",
        description="SMTP username (empty = no auth).",
        validation_alias="OZ_SMTP_USERNAME",
    )
    SMTP_PASSWORD: str = Field(
        default="",
        description="SMTP password (empty = no auth).",
        validation_alias="OZ_SMTP_PASSWORD",
    )
    SMTP_FROM_ADDR: str = Field(
        default="noreply@openzync.tech",
        description="From: address for outgoing emails.",
        validation_alias="OZ_SMTP_FROM_ADDR",
    )
    SMTP_USE_TLS: bool = Field(
        default=False,
        description="Use implicit TLS (SMTPS) on connect (port 465). Set false for STARTTLS (port 587).",
        validation_alias="OZ_SMTP_USE_TLS",
    )
    SMTP_START_TLS: bool = Field(
        default=True,
        description="Use STARTTLS to upgrade to TLS after connect.",
        validation_alias="OZ_SMTP_START_TLS",
    )

    # ── Rate Limiting ─────────────────────────────────────────────────────
    RATE_LIMIT_IP_MAX: int = Field(
        default=10,
        ge=1,
        description="Max requests per IP within the rate-limit window.",
        validation_alias="OZ_RATE_LIMIT_IP_MAX",
    )
    RATE_LIMIT_WINDOW_SEC: int = Field(
        default=60,
        ge=1,
        description="Rate-limit window in seconds.",
        validation_alias="OZ_RATE_LIMIT_WINDOW_SEC",
    )

    # ── Blob / File Upload Limits ────────────────────────────────────────
    MAX_BLOBS_PER_MESSAGE: int = Field(
        default=10,
        description="Maximum number of blobs that can be attached to a single message.",
        validation_alias="OZ_MAX_BLOBS_PER_MESSAGE",
    )
    MAX_BLOBS_PER_REQUEST: int = Field(
        default=50,
        description="Maximum total blobs across all messages in one request.",
        validation_alias="OZ_MAX_BLOBS_PER_REQUEST",
    )

    # ── Blob Storage (S3-compatible, global — env-owned) ─────────────
    # ⚠️ BREAKING: per-user blob storage was removed — one global backend
    # for all users, configured here. Missing required fields fail fast
    # at startup (no silent fallback to shared defaults).
    S3_ENDPOINT_URL: str = Field(
        description="S3-compatible endpoint URL for blob storage (e.g. http://minio:9000).",
        validation_alias="OZ_S3_ENDPOINT_URL",
    )
    S3_REGION: str = Field(
        default="auto",
        description="S3 region (use 'auto' for MinIO).",
        validation_alias="OZ_S3_REGION",
    )
    S3_ACCESS_KEY_ID: str = Field(
        description="S3 access key ID for blob storage.",
        validation_alias="OZ_S3_ACCESS_KEY_ID",
    )
    S3_SECRET_ACCESS_KEY: str = Field(
        description="S3 secret access key for blob storage.",
        validation_alias="OZ_S3_SECRET_ACCESS_KEY",
    )
    S3_BUCKET_NAME: str = Field(
        default="openzync-blobs",
        description="S3 bucket for blob storage.",
        validation_alias="OZ_S3_BUCKET_NAME",
    )
    S3_BACKEND: str = Field(
        default="s3",
        description="Blob storage backend (s3, none).",
        validation_alias="OZ_S3_BACKEND",
    )
    BLOB_MAX_SIZE_MB: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Max upload size per blob in MB.",
        validation_alias="OZ_BLOB_MAX_SIZE_MB",
    )
    BLOB_IMAGE_EXTRACTION: str = Field(
        default="none",
        description="Image text extraction method: 'ocr' (Tesseract), "
        "'vision' (LLM vision API), 'none' (store only, no extraction).",
        validation_alias="OZ_BLOB_IMAGE_EXTRACTION",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Singleton accessors
# ═══════════════════════════════════════════════════════════════════════════════

_settings: Settings | None = None
"""Module-level :class:`Settings` singleton, populated lazily from env."""



def get_settings() -> Settings:
    """Return the :class:`Settings` singleton, creating it from env on first call.

    Returns:
        The module-level singleton.

    Raises:
        ValidationError: If required ``OZ_*`` env vars are missing or invalid
            (fail-fast at startup).
    """
    global _settings  # noqa: PLW0603  — intentional module-level state
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


# ═══════════════════════════════════════════════════════════════════════════════
# Backward-compatible ``from core.config import settings``
# ═══════════════════════════════════════════════════════════════════════════════


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
