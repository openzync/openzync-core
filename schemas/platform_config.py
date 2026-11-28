"""Pydantic schemas for per-user platform configuration.

All settings that were previously env-var-only (Groups A, B, C) are now
storable in the ``users.config_override`` JSONB column and exposed via UI.

Key pattern:
- ``PlatformConfigBase`` — the raw DB shape (all fields optional).
- ``UpdatePlatformConfigRequest`` — API input for partial updates.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids a core import
    from core.llm import LLMConfig

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

#: Required unified LLM fields per backend: ``{backend: required}``.
#: Mirrors ``core.llm_backends.PROVIDER_REQUIREMENTS`` (single source of
#: truth lives there; this copy constrains the stored/request shapes).
_LLM_REQUIRED: dict[str, frozenset[str]] = {
    "openai": frozenset({"llm_api_key"}),
    "anthropic": frozenset({"llm_api_key"}),
    "azure": frozenset({"azure_endpoint", "llm_api_key", "azure_deployment"}),
    "ollama": frozenset({"llm_base_url"}),
    "openai_like": frozenset({"llm_base_url"}),
}


def _validate_llm_requirements(obj: BaseModel) -> BaseModel:
    """Reject a selected backend whose required unified fields are null (→ 422).

    Only ``None`` fails — empty strings pass (they are the onboarding
    placeholder convention: frontend treats ``""`` as unset).  Empties
    fail closed later at ``core.llm_backends.validate_llm_config``
    (probe reports ``ok: false``; workers raise loudly).
    """
    backend: str | None = getattr(obj, "llm_backend", None)
    if backend is None:
        return obj
    missing = sorted(f for f in _LLM_REQUIRED[backend] if getattr(obj, f, None) is None)
    if missing:
        raise ValueError(
            f"llm_backend={backend!r} requires {', '.join(missing)} — "
            "set them in the same PATCH (backend switches must ship credentials)."
        )
    return obj


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

    ⚠️ BREAKING: provider credentials collapsed to unified names —
    ``llm_api_key`` (shared by openai/anthropic/openai_like; Azure key),
    ``llm_base_url`` (ollama/openai_like), ``azure_endpoint``,
    ``azure_deployment``.  Dropped without fallback: ``openai_api_key``,
    ``anthropic_api_key``, ``azure_openai_endpoint``, ``azure_openai_key``,
    ``ollama_base_url``, ``openai_like_base_url``, ``openai_model``,
    ``anthropic_model`` (migration 0083 carries stored values forward;
    ``extra="ignore"`` drops any stragglers on read).
    """

    model_config = {"extra": "ignore"}  # silently drop unknown keys

    # ── LLM ────────────────────────────────────────────────────────────────
    llm_backend: LlmBackend | None = Field(
        default=None,
        description="LLM provider (ollama, openai, openai_like, azure, anthropic).",
    )
    llm_model: str | None = Field(
        default=None,
        description="Model name/tag for the LLM backend (all providers "
        "except azure, which models via azure_deployment).",
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
    llm_api_key: str | None = Field(
        default=None,
        description="Shared API key for the openai, anthropic and openai_like "
        "backends; the Azure API key when llm_backend is azure.",
    )
    llm_base_url: str | None = Field(
        default=None,
        description="Base URL for the ollama backend (local instance) or any "
        "OpenAI-compatible endpoint (openai_like: self-hosted vLLM, "
        "OpenRouter, LiteLLM proxy, …).",
    )
    azure_endpoint: str | None = Field(
        default=None,
        description="Azure OpenAI endpoint URL (azure backend only).",
    )
    azure_deployment: str | None = Field(
        default=None,
        description="Azure OpenAI deployment name (azure backend only).",
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

    def to_llm_config(self) -> LLMConfig:
        """Return unified config for ``core.llm.resolve_backend()``.

        ⚠️ BREAKING: replaces ``to_llm_config_dict()`` — callers pass the
        returned :class:`core.llm.LLMConfig` as ``config=``.  Dicts are
        rejected with ``TypeError`` at the resolve boundary.

        Returns:
            An ``LLMConfig`` built from the unified stored fields.

        Raises:
            LLMConfigurationError: If ``llm_backend`` is unset.
        """
        from core.exceptions import LLMConfigurationError  # noqa: PLC0415 — lazy, schemas must not import core at module level
        from core.llm import LLMConfig, LLMProvider  # noqa: PLC0415 — lazy, same reason

        if self.llm_backend is None:
            raise LLMConfigurationError(
                "No LLM backend configured.  Set llm_backend via "
                "PATCH /admin/platform-config."
            )
        return LLMConfig(
            provider=LLMProvider(self.llm_backend),
            model=self.llm_model,
            api_key=self.llm_api_key,
            base_url=self.llm_base_url,
            endpoint=self.azure_endpoint,
            deployment=self.azure_deployment,
            temperature=self.llm_temperature,
            max_tokens=self.llm_max_tokens,
        )

    @model_validator(mode="after")
    def _check_llm_requirements(self) -> PlatformConfigBase:
        """Enforce per-backend required unified fields (→ 422)."""
        return _validate_llm_requirements(self)  # type: ignore[return-value]


# ── API request / response ─────────────────────────────────────────────────


class UpdatePlatformConfigRequest(BaseModel):
    """Request body for ``PATCH /admin/platform-config``.

    Same shape as ``PlatformConfigBase`` — every field is optional, only provided
    fields are updated.  Set a field to ``null`` to remove it (the caller
    will receive ``None`` for that field on the next read).

    ⚠️ BREAKING: unified LLM names only (``llm_api_key``, ``llm_base_url``,
    ``azure_endpoint``, ``azure_deployment``).  Legacy provider keys
    (``openai_api_key``, ``anthropic_api_key``, ``azure_openai_endpoint``,
    ``azure_openai_key``, ``ollama_base_url``, ``openai_like_base_url``,
    ``openai_model``, ``anthropic_model``) are dropped without fallback —
    unknown keys are ignored, so sending them silently does nothing.
    When ``llm_backend`` is provided, its required unified fields must be
    present (non-null) in the same body, else 422 — backend switches must
    ship credentials.
    """

    # Same fields as PlatformConfigBase, all optional
    llm_backend: LlmBackend | None = None
    llm_model: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    llm_max_tokens: int | None = Field(default=None, ge=1)
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    azure_endpoint: str | None = None
    azure_deployment: str | None = None
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

    @model_validator(mode="after")
    def _check_llm_requirements_update(self) -> UpdatePlatformConfigRequest:
        """Enforce per-backend required unified fields on PATCH bodies (→ 422)."""
        return _validate_llm_requirements(self)  # type: ignore[return-value]

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
