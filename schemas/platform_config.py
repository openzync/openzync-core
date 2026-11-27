"""Pydantic schemas for per-user platform configuration.

All settings that were previously env-var-only (Groups A, B, C) are now
storable in the ``users.config_override`` JSONB column and exposed via UI.

Key pattern:
- ``PlatformConfigBase`` — the raw DB shape (all fields optional).
- ``UpdatePlatformConfigRequest`` — API input for partial updates.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

LlmBackend = Literal["ollama", "openai", "openai_like", "azure", "anthropic"]

# ⚠️ BREAKING: ``EmbeddingBackend`` was removed. Embeddings are produced by a
# single local ONNX model (``core.embeddings``) with no provider routing, so
# there is nothing left for a Literal to constrain.

# ── System-managed field sets ──────────────────────────────────────────────
# These fields are overridden at the system level (via OZ_* env vars)
# and cannot be set through platform_config when the corresponding system
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

#: Allowed per-episode enrichment toggle keys. Local copy — schemas must
#: never import from ``core/`` (canonical home is
#: ``core.enrichment_bits.ENRICHMENT_KEYS``); parity is enforced by the
#: import-time assert in ``core.enrichment_config``.
ENRICHMENT_KEYS: frozenset[str] = frozenset(
    {
        "entities",
        "embedding",
        "facts",
        "entity_links",
        "classification",
        "structured",
        "observations",
        "blob_text",
        "llm_invalidation",
    }
)

EnrichmentMap = dict[str, bool]
"""Per-episode enrichment toggles (every key defaults to ``True``)."""


def _validate_enrichment_keys(v: dict[str, bool] | None) -> dict[str, bool] | None:
    """Reject unknown enrichment toggle keys (→ 422)."""
    if v is None:
        return v
    unknown = set(v) - ENRICHMENT_KEYS
    if unknown:
        raise ValueError(
            f"Unknown enrichment keys: {sorted(unknown)}. "
            f"Expected a subset of: {sorted(ENRICHMENT_KEYS)}."
        )
    return v


def _validate_enrichment_values(
    v: dict[str, bool] | dict[str, bool | None] | None,
) -> dict[str, bool] | dict[str, bool | None] | None:
    """Reject non-boolean toggle values (→ 422).

    Truthy coercion would silently enable a toggle the caller tried to
    disable (``bool("false")`` is ``True``), so only real booleans pass.
    ``None`` values pass through untouched — they are staged resets (key
    removal), honoured by the merge path, not validation.
    """
    if v is None:
        return v
    for key, value in v.items():
        if value is not None and not isinstance(value, bool):
            raise ValueError(
                f"Enrichment toggle {key!r} must be a boolean or null, "
                f"got {type(value).__name__}."
            )
    return v


def _coerce_enrichment_gate(v: Any) -> Any:
    """Coerce legacy gate shapes to the user gate object shape.

    ``True``/``False`` → ``{"enabled": v, "threshold": 0.5}``; dicts pass
    through with the legacy ``model`` subkey stripped (the model is
    fixed to the platform default, never configured).
    """
    if v is None or isinstance(v, EnrichmentGatePlatformConfig):
        return v
    if isinstance(v, bool):
        return {"enabled": v, "threshold": 0.5}
    if isinstance(v, dict):
        v = dict(v)
        v.pop("model", None)
        return v
    return v


class EnrichmentGatePlatformConfig(BaseModel):
    """Per-user enrichment gate (non-secret tunables only).

    The gate skips the single LLM call for low-signal episodes.  The Hub
    model is fixed to the platform default — a ``model`` subkey is
    stripped, never honored.  This endpoint serves unauthenticated
    defaults, so nothing secret belongs here.
    """

    model_config = {"extra": "ignore"}  # strip legacy ``model`` subkey

    enabled: bool = False
    threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Minimum should_enrich score to run the LLM.",
    )


class PromptCachingPlatformConfig(BaseModel):
    """Platform overrides for provider-side prompt caching.

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


# ── DB shape (stored in users.config_override JSONB) ──────────────────────────


class PlatformConfigBase(BaseModel):
    """Raw per-user config stored in the ``users.config_override`` JSONB column.

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
    enrichment: dict[str, bool] | None = Field(
        default=None,
        description="Per-episode enrichment toggles (entities, embedding, "
        "facts, entity_links, classification, structured, observations, "
        "blob_text, llm_invalidation). Unset keys default to True. "
        "Validated on ingest: facts/entity_links require entities, "
        "llm_invalidation requires facts.",
    )
    enrichment_gate: EnrichmentGatePlatformConfig | None = Field(
        default=None,
        description="Enrichment gate for this platform "
        "({enabled, threshold 0-1}); None inherits the gate defaults "
        "(disabled, 0.5).",
    )
    prompt_caching: PromptCachingPlatformConfig | None = Field(
        default=None,
        description="Platform overrides for provider-side prompt caching "
        "(enabled, anthropic_min_tokens, anthropic_cache_ttl).  Unset "
        "fields fall back to the global PROMPT_CACHING_* settings.",
    )

    # ── Embeddings ─────────────────────────────────────────────────────────
    # ⚠️ BREAKING: embedding_backend / embedding_model / embedding_dim /
    # embedding_api_key / embedding_openai_like_base_url were removed from this
    # model.  Embeddings are a local ONNX model with zero configuration — see
    # ``core.embeddings``.  Stored rows carrying these keys still validate,
    # Pydantic ignores unknown keys by default, and they are dropped on the
    # next write.

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

    # ── SurrealDB (platform connection) ────────────────────────────
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

    # ── FalkorDB (platform connection details) ─────────────────────────
    falkordb_url: str | None = Field(
        default=None,
        description="FalkorDB connection URL for platform configuration "
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

    # ⚠️ BREAKING: per-user blob storage was removed — one global backend
    # (``OZ_S3_*`` / ``OZ_BLOB_*`` Settings, env-owned). The deleted
    # fields (``blob_storage_backend``, ``s3_endpoint_url``, ``s3_region``,
    # ``s3_access_key_id``, ``s3_secret_access_key``, ``s3_bucket_name``,
    # ``max_blob_size_mb``, ``image_extraction``) are ignored on read
    # (``extra="ignore"``) and dropped on the next write; migration 0077
    # strips them from stored JSONB.

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

    @field_validator("enrichment", mode="before")
    @classmethod
    def _validate_enrichment(
        cls, v: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        """Reject unknown toggle keys and non-boolean values (→ 422)."""
        _validate_enrichment_values(v)
        return _validate_enrichment_keys(v)

    @field_validator("enrichment_gate", mode="before")
    @classmethod
    def _coerce_enrichment_gate(cls, v: Any) -> Any:
        """Coerce legacy bool/dict gate shapes to the gate object."""
        return _coerce_enrichment_gate(v)

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
            raise ValueError(
                f"Invalid PII types: {invalid}. Allowed: {sorted(allowed)}"
            )
        return v

    # ── Helpers for downstream callers ───────────────────────────────────────

    def to_llm_config_dict(self) -> dict[str, str | float | int | dict[str, object]]:
        """Return config as a dict suitable for ``core.llm.resolve_backend()``.

        Only non-``None`` fields are included.  The returned dict maps
        our canonical field names to the provider-specific keys that
        ``_create_backend()`` in ``core/llm.py`` expects.  ``prompt_caching``
        is included as a nested dict so ``build_cache_config()`` can honour
        platform overrides.
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


# ── API request / response ─────────────────────────────────────────────────


class UpdatePlatformConfigRequest(BaseModel):
    """Request body for ``PATCH /admin/platform-config``.

    Same shape as ``PlatformConfigBase`` — every field is optional, only provided
    fields are updated.  Set a field to ``null`` to remove it (the caller
    will receive ``None`` for that field on the next read).
    """

    # Same fields as PlatformConfigBase, all optional
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
    enrichment: dict[str, bool | None] | None = Field(
        default=None,
        description="Per-episode enrichment toggles (entities, embedding, "
        "facts, entity_links, classification, structured, observations, "
        "blob_text, llm_invalidation). Unset keys default to True. "
        "Set a toggle to null to reset it (removes the stored override, "
        "restoring the default True). "
        "Validated on ingest: facts/entity_links require entities, "
        "llm_invalidation requires facts.",
    )
    enrichment_gate: EnrichmentGatePlatformConfig | None = Field(
        default=None,
        description="Enrichment gate for this platform "
        "({enabled, threshold 0-1}). Null removes the stored override "
        "(restores the gate defaults: disabled, 0.5).",
    )
    prompt_caching: PromptCachingPlatformConfig | None = None
    # ⚠️ BREAKING: embedding_* fields removed — see the note on
    # ``PlatformConfigBase``. Embedding settings now live in no config surface at all.
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
            # Plain ValueError (never GoneError): pydantic wraps it as a
            # field error and the RequestValidationError handler in
            # core/exceptions.py maps this message to a 410 problem JSON.
            raise ValueError(
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
    # ⚠️ BREAKING: blob_storage_backend / s3_* / max_blob_size_mb /
    # image_extraction removed — see the note on ``PlatformConfigBase``.
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

    @field_validator("enrichment", mode="before")
    @classmethod
    def _validate_enrichment_update(
        cls, v: dict[str, bool | None] | None
    ) -> dict[str, bool | None] | None:
        """Reject unknown keys and non-boolean values (→ 422).

        ``None`` values are staged resets: the merge path removes that
        toggle from the stored override (restoring the default ``True``).
        """
        _validate_enrichment_values(v)
        return _validate_enrichment_keys(v)  # type: ignore[return-value]

    @field_validator("enrichment_gate", mode="before")
    @classmethod
    def _coerce_enrichment_gate_update(cls, v: Any) -> Any:
        """Coerce legacy bool/dict gate shapes to the gate object (update)."""
        return _coerce_enrichment_gate(v)

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
            raise ValueError(
                f"Invalid PII types: {invalid}. Allowed: {sorted(allowed)}"
            )
        return v


ConfigTestDomain = Literal["llm", "graph"]
"""Domain selector for ``POST /admin/platform-config/test``.

⚠️ BREAKING: ``"embeddings"`` was removed — there is no embedder to
configure or probe.  POSTing it now fails schema validation (422).
⚠️ BREAKING: ``"blob"`` was removed with per-user blob storage — blob
storage is global (``OZ_S3_*``) with nothing to probe.  POSTing it now
fails schema validation (422).
"""


class TestPlatformConfigRequest(BaseModel):
    """Request body for ``POST /admin/platform-config/test``.

    ``config`` is a partial candidate — same shape as
    :class:`UpdatePlatformConfigRequest`, only explicitly provided fields
    overlay the stored config in-memory.  Nothing is persisted.
    """

    domain: ConfigTestDomain = Field(
        description="Which connection family to probe (llm, graph).",
    )
    config: UpdatePlatformConfigRequest = Field(
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


class TestPlatformConfigResponse(BaseModel):
    """Response for ``POST /admin/platform-config/test``.

    Per-probe failures are reported inline with ``ok: false`` — the
    endpoint still returns 200.  Only invalid payloads (422) or a
    down secrets backend (503) change the status code.
    """

    results: dict[str, ProbeResult] = Field(
        description="Probe outcomes keyed by probe name.",
    )


class PlatformConfigResponse(BaseModel):
    """Response for config GET endpoints.

    Returns the raw stored config along with metadata about which
    fields are managed at the system level.
    """

    stored: PlatformConfigBase = Field(
        description="Raw config stored in the DB — only explicitly set fields.",
    )
    system_managed_fields: list[str] = Field(
        default=[],
        description="List of field names that are configured at the system level "
        "and cannot be modified via this API.",
    )
