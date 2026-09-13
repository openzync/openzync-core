"""Pydantic schemas for per-organization configuration.

All settings that were previously env-var-only (Groups A, B, C) are now
storable in the ``organizations.config`` JSONB column and exposed via UI.

Key pattern:
- ``OrgConfigBase`` — the raw DB shape (all fields optional).
- ``UpdateOrgConfigRequest`` — API input for partial updates.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

LlmBackend = Literal["ollama", "openai", "openai_like", "azure", "anthropic"]

# ── System-managed field sets ──────────────────────────────────────────────
# These fields are overridden at the system level (via OpenBao / env vars)
# and cannot be set through org_config when the corresponding system
# setting is active.

SYSTEM_MANAGED_SURREALDB_FIELDS: frozenset[str] = frozenset(
    {
        "surrealdb_url",
        "surrealdb_user",
        "surrealdb_pass",
        "surrealdb_namespace",
        "surrealdb_database",
    }
)

SYSTEM_MANAGED_FALKORDB_FIELDS: frozenset[str] = frozenset(
    {
        "falkordb_url",
    }
)


class PromptCachingOrgConfig(BaseModel):
    """Per-org overrides for provider-side prompt caching.

    Mirrors the global ``PROMPT_CACHING_*`` settings.  Every field is
    optional — unset fields fall back to the global value in
    ``core.llm.build_cache_config``.
    """

    enabled: bool | None = None
    anthropic_min_tokens: int | None = Field(default=None, ge=512)
    anthropic_cache_ttl: str | None = Field(default=None)

    @field_validator("anthropic_cache_ttl", mode="before")
    @classmethod
    def _validate_cache_ttl(cls, v: str | None) -> str | None:
        """Validate cache TTL, falling back to ``"5m"`` on invalid input.

        Mirrors the global ``PROMPT_CACHING_ANTHROPIC_TTL`` validator in
        ``core.config`` — invalid values degrade to the default instead of
        failing the whole config update.
        """
        if v is None or v in ("5m", "1h"):
            return v
        logger.warning(
            "Invalid prompt_caching.anthropic_cache_ttl=%r, falling back to '5m'",
            v,
        )
        return "5m"


# ── DB shape (stored in organizations.config JSONB) ──────────────────────────


class OrgConfigBase(BaseModel):
    """Raw per-org config stored in the ``organizations.config`` JSONB column.

    Every field is **optional**.  When a field is ``None`` (absent from the
    JSONB), the caller must decide what to do — there is no env-var fallback
    at this layer.
    """

    model_config = {"extra": "ignore"}  # silently drop unknown keys

    # ── LLM ────────────────────────────────────────────────────────────────
    llm_backend: LlmBackend | None = Field(
        default=None,
        description="LLM provider (ollama, openai, openai_like, azure, anthropic).",
    )
    llm_model: str | None = Field(
        default=None,
        description="Model name/tag for the LLM backend.",
    )
    llm_temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="LLM sampling temperature (0.0–2.0).",
    )
    llm_max_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Maximum tokens in the LLM response.",
    )
    openai_api_key: str | None = Field(
        default=None,
        description="OpenAI API key.",
    )
    azure_openai_endpoint: str | None = Field(
        default=None,
        description="Azure OpenAI endpoint URL.",
    )
    azure_openai_key: str | None = Field(
        default=None,
        description="Azure OpenAI API key.",
    )
    anthropic_api_key: str | None = Field(
        default=None,
        description="Anthropic API key.",
    )
    ollama_base_url: str | None = Field(
        default=None,
        description="Base URL for a local Ollama instance.",
    )
    openai_like_base_url: str | None = Field(
        default=None,
        description="Base URL for any OpenAI-compatible endpoint "
        "(self-hosted vLLM, OpenRouter, LiteLLM proxy, …).",
    )
    llm_fact_invalidation_enabled: bool | None = Field(
        default=None,
        description="Enable LLM-driven fact invalidation during episode "
        "enrichment (defaults to ON when unset).",
    )
    prompt_caching: PromptCachingOrgConfig | None = Field(
        default=None,
        description="Per-org overrides for provider-side prompt caching "
        "(enabled, anthropic_min_tokens, anthropic_cache_ttl).  Unset "
        "fields fall back to the global PROMPT_CACHING_* settings.",
    )

    # ── Embeddings ─────────────────────────────────────────────────────────
    embedding_backend: str | None = Field(
        default=None,
        description="Embedding provider.  Falls back to LLM_BACKEND when empty.",
    )
    embedding_model: str | None = Field(
        default=None,
        description="Embedding model name/tag.",
    )
    embedding_dim: int | None = Field(
        default=None,
        ge=64,
        le=4096,
        description="Output dimensionality of the embedding model.",
    )

    # ── Graph ──────────────────────────────────────────────────────────────
    graph_backend: str | None = Field(
        default="falkordb",
        description="Graph backend (falkordb, surrealdb, none). Defaults to "
        "falkordb — FalkorDB is the default graph engine. "
        "'postgres' was removed in v1.1.0 (410 Gone). "
        "'none' disables the graph.",
    )
    graph_search_type: str | None = Field(
        default=None,
        description="Graph search algorithm (hybrid, bm25, vector).",
    )
    graph_max_traversal_depth: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="Maximum BFS traversal depth for the graph backend.",
    )

    # ── SurrealDB (per-org connection) ─────────────────────────────
    surrealdb_url: str | None = Field(
        default=None,
        description="SurrealDB WebSocket connection URL (e.g. ws://surrealdb:8000/rpc).",
    )
    surrealdb_user: str | None = Field(
        default=None,
        description="SurrealDB authentication username.",
    )
    surrealdb_pass: str | None = Field(
        default=None,
        description="SurrealDB authentication password.",
    )
    surrealdb_namespace: str | None = Field(
        default=None,
        description="SurrealDB namespace (tenant isolation boundary).",
    )
    surrealdb_database: str | None = Field(
        default=None,
        description="SurrealDB database within the namespace.",
    )

    # ── FalkorDB (per-org connection details) ────────────────────────────
    falkordb_url: str | None = Field(
        default=None,
        description="FalkorDB connection URL for per-org configuration "
        "(e.g. redis://falkordb:6379).  Only used when system-level "
        "FALKORDB_URL is not set.",
    )

    # ── Behaviour ──────────────────────────────────────────────────────────
    context_cache_ttl: int | None = Field(
        default=None,
        ge=1,
        description="TTL in seconds for cached context summaries.",
    )
    audit_log_response_body: bool | None = Field(
        default=None,
        description="Capture response body in audit_logs.details (may contain PII).",
    )

    # ── Re-ranker (RET-05) ────────────────────────────────────────────────
    reranker_backend: str | None = Field(
        default=None,
        description="Re-ranker backend (sentence_transformers, cohere, or null to disable).",
    )
    reranker_model: str | None = Field(
        default=None,
        description="Model name for the re-ranker (e.g. cross-encoder/ms-marco-MiniLM-L-6-v2 or rerank-english-v3.0).",
    )
    reranker_top_k: int | None = Field(
        default=None,
        ge=10,
        le=200,
        description="Number of RRF candidates to pass to the re-ranker (default 50).",
    )
    reranker_top_n: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Number of results to return after re-ranking (default 10).",
    )
    cohere_api_key: str | None = Field(
        default=None,
        description="Cohere API key for the Cohere Rerank backend.",
    )

    # ── Blob Storage (S3-compatible) ────────────────────────────────────
    blob_storage_backend: str | None = Field(
        default=None,
        description="Blob storage backend (s3, none). Defaults to 's3' when endpoint is configured.",
    )
    s3_endpoint_url: str | None = Field(
        default=None,
        description="S3-compatible endpoint URL (e.g. http://minio:9000).",
    )
    s3_region: str | None = Field(
        default=None,
        description="S3 region (use 'auto' for MinIO).",
    )
    s3_access_key_id: str | None = Field(
        default=None,
        description="S3 access key ID.",
    )
    s3_secret_access_key: str | None = Field(
        default=None,
        description="S3 secret access key.",
    )
    s3_bucket_name: str | None = Field(
        default=None,
        description="S3 bucket for blob storage.",
    )
    max_blob_size_mb: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description="Max upload size per blob in MB (default 50). Overrides the system default.",
    )

    # ── Image Extraction ─────────────────────────────────────────────────
    image_extraction: str | None = Field(
        default=None,
        description="Image text extraction method: 'ocr' (Tesseract), "
        "'vision' (LLM vision API), 'none' (store only, no extraction). "
        "Default 'none'.",
    )

    # ── PII ──────────────────────────────────────────────────────────────────
    pii_mode: str | None = Field(
        default=None,
        pattern=r"^(off|mask|block)$",
        description="PII mode: off|mask|block. Defaults to mask when unset.",
    )
    pii_sensitivity: str | None = Field(
        default=None,
        pattern=r"^(low|medium|high)$",
        description="low=regex only, medium=regex+NER, high=regex+NER+LLM.",
    )
    pii_enabled_types: list[str] | None = Field(
        default=None,
        description="Subset of PII types to scan; None means all defaults.",
    )
    pii_min_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )

    @field_validator("pii_enabled_types", mode="before")
    @classmethod
    def _validate_pii_types(cls, v: list[str] | None) -> list[str] | None:
        """Validate PII enabled types against known set."""
        if v is None:
            return v
        allowed = {
            "email",
            "phone",
            "ssn",
            "credit_card",
            "ip_address",
            "api_key",
            "crypto_wallet",
            "name",
            "address",
            "organization",
            "date",
        }
        invalid = [t for t in v if t not in allowed]
        if invalid:
            raise ValueError(f"Invalid PII types: {invalid}. Allowed: {sorted(allowed)}")
        return v

    # ── Helpers for downstream callers ───────────────────────────────────────

    def to_llm_config_dict(self) -> dict[str, str | float | int | dict[str, object]]:
        """Return config as a dict suitable for ``core.llm.resolve_backend()``.

        Only non-``None`` fields are included.  The returned dict maps
        our canonical field names to the provider-specific keys that
        ``_create_backend()`` in ``core/llm.py`` expects.  ``prompt_caching``
        is included as a nested dict so ``build_cache_config()`` can honour
        per-org overrides.
        """
        d: dict[str, str | float | int | dict[str, object]] = {}
        if self.llm_backend is not None:
            d["llm_backend"] = self.llm_backend
        if self.openai_api_key is not None:
            d["openai_api_key"] = self.openai_api_key
        if self.openai_like_base_url is not None:
            d["openai_like_base_url"] = self.openai_like_base_url
        if self.llm_model is not None:
            d["openai_model"] = self.llm_model
            d["llm_model"] = self.llm_model
            d["azure_deployment"] = self.llm_model
            d["anthropic_model"] = self.llm_model
            d["model"] = self.llm_model
        if self.azure_openai_endpoint is not None:
            d["azure_endpoint"] = self.azure_openai_endpoint
        if self.azure_openai_key is not None:
            d["azure_api_key"] = self.azure_openai_key
        if self.anthropic_api_key is not None:
            d["anthropic_api_key"] = self.anthropic_api_key
        if self.ollama_base_url is not None:
            d["ollama_base_url"] = self.ollama_base_url
        if self.llm_temperature is not None:
            d["temperature"] = self.llm_temperature
        if self.llm_max_tokens is not None:
            d["max_tokens"] = self.llm_max_tokens
        if self.prompt_caching is not None:
            d["prompt_caching"] = self.prompt_caching.model_dump(
                mode="python", exclude_none=True
            )
        return d

    def to_embedding_config_dict(self) -> dict[str, str | int]:
        """Return embedding config as a flat dict.

        Only non-``None`` fields are included.  Used by worker tasks that
        read embedding settings directly.
        """
        d: dict[str, str | int] = {}
        if self.embedding_backend is not None:
            d["embedding_backend"] = self.embedding_backend
        if self.embedding_model is not None:
            d["embedding_model"] = self.embedding_model
        if self.embedding_dim is not None:
            d["embedding_dim"] = self.embedding_dim
        return d

    def to_blob_storage_config(self) -> dict[str, Any]:
        """Return blob storage config as a flat dict with sensible defaults.

        Only non-``None`` fields override defaults.  Used by
        :class:`BlobStorageConfig` to instantiate a backend.
        """
        config: dict[str, Any] = {}
        if self.blob_storage_backend is not None:
            config["backend"] = self.blob_storage_backend
        if self.s3_endpoint_url is not None:
            config["endpoint_url"] = self.s3_endpoint_url
        if self.s3_region is not None:
            config["region"] = self.s3_region
        if self.s3_access_key_id is not None:
            config["access_key_id"] = self.s3_access_key_id
        if self.s3_secret_access_key is not None:
            config["secret_access_key"] = self.s3_secret_access_key
        if self.s3_bucket_name is not None:
            config["bucket_name"] = self.s3_bucket_name
        if self.max_blob_size_mb is not None:
            config["max_blob_size_mb"] = self.max_blob_size_mb
        return config


# ── API request / response ─────────────────────────────────────────────────


class UpdateOrgConfigRequest(BaseModel):
    """Request body for ``PATCH /admin/organizations/{org_id}/config``.

    Same shape as ``OrgConfigBase`` — every field is optional, only provided
    fields are updated.  Set a field to ``null`` to remove it (the caller
    will receive ``None`` for that field on the next read).
    """

    # Same fields as OrgConfigBase, all optional
    llm_backend: LlmBackend | None = None
    llm_model: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    llm_max_tokens: int | None = Field(default=None, ge=1)
    openai_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str | None = None
    openai_like_base_url: str | None = None
    llm_fact_invalidation_enabled: bool | None = Field(
        default=None,
        description="Enable LLM-driven fact invalidation during episode "
        "enrichment (defaults to ON when unset).",
    )
    prompt_caching: PromptCachingOrgConfig | None = None
    embedding_backend: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = Field(default=None, ge=64, le=4096)
    graph_backend: str | None = Field(
        default=None,
        description="Graph backend (falkordb, surrealdb, none). "
        "`postgres` was removed in v1.1.0 (410 Gone) — hard-rejected on write.",
    )
    graph_search_type: str | None = None
    graph_max_traversal_depth: int | None = Field(default=None, ge=1, le=10)
    surrealdb_url: str | None = None
    surrealdb_user: str | None = None
    surrealdb_pass: str | None = None
    surrealdb_namespace: str | None = None
    surrealdb_database: str | None = None
    falkordb_url: str | None = None

    @field_validator("graph_backend", mode="before")
    @classmethod
    def _reject_postgres(cls, v: str | None) -> str | None:
        """Hard-reject `postgres` — removed in v1.1.0, returns 410 Gone."""
        if v == "postgres":
            from core.exceptions import GoneError

            raise GoneError(
                "PostgreSQL graph backend deprecated — gone, removed in v1.1.0. "
                "Migrate to `falkordb` (410)."
            )
        return v

    context_cache_ttl: int | None = Field(default=None, ge=1)
    audit_log_response_body: bool | None = None
    reranker_backend: str | None = None
    reranker_model: str | None = None
    reranker_top_k: int | None = Field(default=None, ge=10, le=200)
    reranker_top_n: int | None = Field(default=None, ge=1, le=100)
    cohere_api_key: str | None = None
    blob_storage_backend: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket_name: str | None = None
    max_blob_size_mb: int | None = Field(default=None, ge=1, le=500)
    image_extraction: str | None = None
    pii_mode: str | None = Field(
        default=None,
        pattern=r"^(off|mask|block)$",
        description="PII mode: off|mask|block. Defaults to mask when unset.",
    )
    pii_sensitivity: str | None = Field(
        default=None,
        pattern=r"^(low|medium|high)$",
        description="low=regex only, medium=regex+NER, high=regex+NER+LLM.",
    )
    pii_enabled_types: list[str] | None = Field(
        default=None,
        description="Subset of PII types to scan; None means all defaults.",
    )
    pii_min_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )

    @field_validator("pii_enabled_types", mode="before")
    @classmethod
    def _validate_pii_types_update(cls, v: list[str] | None) -> list[str] | None:
        """Validate PII enabled types against known set (update request)."""
        if v is None:
            return v
        allowed = {
            "email",
            "phone",
            "ssn",
            "credit_card",
            "ip_address",
            "api_key",
            "crypto_wallet",
            "name",
            "address",
            "organization",
            "date",
        }
        invalid = [t for t in v if t not in allowed]
        if invalid:
            raise ValueError(f"Invalid PII types: {invalid}. Allowed: {sorted(allowed)}")
        return v


ConfigTestDomain = Literal["llm", "embeddings", "graph", "blob"]
"""Domain selector for ``POST /admin/org/config/test``."""


class TestOrgConfigRequest(BaseModel):
    """Request body for ``POST /admin/org/config/test``.

    ``config`` is a partial candidate — same shape as
    :class:`UpdateOrgConfigRequest`, only explicitly provided fields
    overlay the stored config in-memory.  Nothing is persisted.
    """

    domain: ConfigTestDomain = Field(
        description="Which connection family to probe (llm, embeddings, graph, blob).",
    )
    config: UpdateOrgConfigRequest = Field(
        description="Partial candidate config overlaid on the stored "
        "config in-memory for this probe only.",
    )


class ProbeResult(BaseModel):
    """Outcome of a single connection probe."""

    ok: bool = Field(description="Whether the probe succeeded.")
    latency_ms: int = Field(
        ge=0, description="Wall-clock probe latency in milliseconds."
    )
    detail: str = Field(
        max_length=500,
        description="Human-readable outcome (truncated, secrets redacted).",
    )


class TestOrgConfigResponse(BaseModel):
    """Response for ``POST /admin/org/config/test``.

    Per-probe failures are reported inline with ``ok: false`` — the
    endpoint still returns 200.  Only invalid payloads (422) or a
    down secrets backend (503) change the status code.
    """

    results: dict[str, ProbeResult] = Field(
        description="Probe outcomes keyed by probe name.",
    )


class OrgConfigResponse(BaseModel):
    """Response for config GET endpoints.

    Returns the raw stored config along with metadata about which
    fields are managed at the system level.
    """

    stored: OrgConfigBase = Field(
        description="Raw config stored in the DB — only explicitly set fields.",
    )
    system_managed_fields: list[str] = Field(
        default=[],
        description="List of field names that are configured at the system level "
        "and cannot be modified via this API.",
    )
