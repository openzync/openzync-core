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

from pydantic import Field, model_validator
from pydantic.networks import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of truth for all OpenZep configuration.

    Values are read from environment variables (prefixed with ``MG_``) or a
    ``.env`` file.  An instance is created once at import time and reused
    throughout the application — import ``settings`` from this module, do not
    instantiate ``Settings`` yourself.
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

    # ── Graph Backend ─────────────────────────────────────────────────────
    FALKORDB_URL: RedisDsn | None = Field(
        default=None,
        description="FalkorDB connection string (required only when GRAPH_BACKEND=graphiti).",
        validation_alias="MG_FALKORDB_URL",
    )
    GRAPH_BACKEND: Literal["postgres", "graphiti", "falkordb", "neo4j", "none"] = Field(
        default="postgres",
        description=(
            "Graph backend to use: postgres (native), graphiti (FalkorDB), "
            "or none (disable).  Accepts 'falkordb' as an alias for 'graphiti'."
        ),
        validation_alias="MG_GRAPH_BACKEND",
    )
    GRAPH_MAX_TRAVERSAL_DEPTH: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Maximum BFS traversal depth for the graph backend.",
        validation_alias="MG_GRAPH_MAX_TRAVERSAL_DEPTH",
    )

    # ── LLM ───────────────────────────────────────────────────────────────
    LLM_BACKEND: Literal["ollama", "openai", "azure", "anthropic", "openrouter"] = (
        Field(
            default="ollama",
            description="LLM provider backend.",
            validation_alias="MG_LLM_BACKEND",
        )
    )
    LLM_MODEL: str = Field(
        default="llama3.2:3b",
        description="Model name / tag to use with the LLM backend.",
        validation_alias="MG_LLM_MODEL",
    )

    # ── Embeddings ────────────────────────────────────────────────────────
    EMBEDDING_BACKEND: str = Field(
        default="",
        description=(
            "Embedding provider.  When empty (default) it falls back to the "
            "same provider as LLM_BACKEND."
        ),
        validation_alias="MG_EMBEDDING_BACKEND",
    )
    EMBEDDING_MODEL: str = Field(
        default="nomic-embed-text",
        description="Embedding model name / tag.",
        validation_alias="MG_EMBEDDING_MODEL",
    )
    EMBEDDING_DIM: int = Field(
        default=768,
        description="Output dimensionality of EMBEDDING_MODEL.",
        validation_alias="MG_EMBEDDING_DIM",
    )

    # ── Context / Memory ──────────────────────────────────────────────────
    CONTEXT_CACHE_TTL: int = Field(
        default=30,
        description="TTL in seconds for cached context summaries.",
        validation_alias="MG_CONTEXT_CACHE_TTL",
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

    # ── LLM API Keys (optional — backend-dependent) ───────────────────────
    OPENAI_API_KEY: str = Field(
        default="",
        description="OpenAI API key.  Required when LLM_BACKEND == 'openai'.",
        validation_alias="OPENAI_API_KEY",
    )
    OPENROUTER_API_KEY: str = Field(
        default="",
        description="OpenRouter API key.  Required when LLM_BACKEND == 'openrouter'.",
        validation_alias="OPENROUTER_API_KEY",
    )
    AZURE_OPENAI_ENDPOINT: str = Field(
        default="",
        description="Azure OpenAI endpoint URL.",
        validation_alias="AZURE_OPENAI_ENDPOINT",
    )
    AZURE_OPENAI_KEY: str = Field(
        default="",
        description="Azure OpenAI API key.",
        validation_alias="AZURE_OPENAI_KEY",
    )
    ANTHROPIC_API_KEY: str = Field(
        default="",
        description="Anthropic API key.  Required when LLM_BACKEND == 'anthropic'.",
        validation_alias="ANTHROPIC_API_KEY",
    )
    OLLAMA_BASE_URL: str = Field(
        default="http://localhost:11434",
        description="Base URL for a local Ollama instance.",
        validation_alias="OLLAMA_BASE_URL",
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

    # ── Rate Limiting ─────────────────────────────────────────────────────

    @model_validator(mode="after")
    def validate_graph_config(self) -> "Settings":
        """Validate graph backend configuration.

        Ensures FALKORDB_URL is set when using the graphiti backend.
        Accepts 'falkordb' as a backward-compatible alias for 'graphiti'.
        """
        backend = self.GRAPH_BACKEND
        if backend in ("falkordb", "neo4j"):
            # Map legacy aliases — they all route to the graphiti code path
            object.__setattr__(self, "GRAPH_BACKEND", "graphiti")
        if self.GRAPH_BACKEND == "graphiti" and not self.FALKORDB_URL:
            raise ValueError("FALKORDB_URL is required when GRAPH_BACKEND=graphiti")
        return self

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
# NOTE: call-arg is ignored because the validator resolves env-var aliases at
# runtime.  Mypy cannot see that pydantic-settings will populate fields from
# environment variables.
