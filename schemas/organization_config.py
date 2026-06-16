"""Pydantic schemas for per-organization configuration.

All settings that were previously env-var-only (Groups A, B, C) are now
storable in the ``organizations.config`` JSONB column and exposed via UI.

Key pattern:
- ``OrgConfigBase`` — the raw DB shape (all fields optional).
- ``UpdateOrgConfigRequest`` — API input for partial updates.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── DB shape (stored in organizations.config JSONB) ──────────────────────────


class OrgConfigBase(BaseModel):
    """Raw per-org config stored in the ``organizations.config`` JSONB column.

    Every field is **optional**.  When a field is ``None`` (absent from the
    JSONB), the caller must decide what to do — there is no env-var fallback
    at this layer.
    """

    model_config = {"extra": "ignore"}  # silently drop unknown keys

    # ── LLM ────────────────────────────────────────────────────────────────
    llm_backend: str | None = Field(
        default=None,
        description="LLM provider (ollama, openai, azure, anthropic, openrouter).",
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
    openrouter_api_key: str | None = Field(
        default=None,
        description="OpenRouter API key.",
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
        default=None,
        description="Graph backend (postgres, graphiti, none).",
    )
    graph_max_traversal_depth: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description="Maximum BFS traversal depth for the graph backend.",
    )
    falkordb_url: str | None = Field(
        default=None,
        description="FalkorDB connection string (required for graphiti backend).",
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

    # ── Helpers for downstream callers ───────────────────────────────────────

    def to_llm_config_dict(self) -> dict[str, str | float | int]:
        """Return config as a dict suitable for ``core.llm.resolve_backend()``.

        Only non-``None`` fields are included.  The returned dict maps
        our canonical field names to the provider-specific keys that
        ``_create_backend()`` in ``core/llm.py`` expects.
        """
        d: dict[str, str | float | int] = {}
        if self.llm_backend is not None:
            d["llm_backend"] = self.llm_backend
        if self.openai_api_key is not None:
            d["openai_api_key"] = self.openai_api_key
        if self.llm_model is not None:
            d["openai_model"] = self.llm_model
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
        if self.openrouter_api_key is not None:
            d["openrouter_api_key"] = self.openrouter_api_key
            d["api_key"] = self.openrouter_api_key
        if self.llm_temperature is not None:
            d["temperature"] = self.llm_temperature
        if self.llm_max_tokens is not None:
            d["max_tokens"] = self.llm_max_tokens
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


# ── API request / response ─────────────────────────────────────────────────


class UpdateOrgConfigRequest(BaseModel):
    """Request body for ``PATCH /admin/organizations/{org_id}/config``.

    Same shape as ``OrgConfigBase`` — every field is optional, only provided
    fields are updated.  Set a field to ``null`` to remove it (the caller
    will receive ``None`` for that field on the next read).
    """

    # Same fields as OrgConfigBase, all optional
    llm_backend: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    llm_max_tokens: int | None = Field(default=None, ge=1)
    openai_api_key: str | None = None
    openrouter_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_key: str | None = None
    anthropic_api_key: str | None = None
    ollama_base_url: str | None = None
    embedding_backend: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = Field(default=None, ge=64, le=4096)
    graph_backend: str | None = None
    graph_max_traversal_depth: int | None = Field(default=None, ge=1, le=10)
    falkordb_url: str | None = None
    context_cache_ttl: int | None = Field(default=None, ge=1)
    audit_log_response_body: bool | None = None


class OrgConfigResponse(BaseModel):
    """Response for config GET endpoints.

    Returns the raw stored config.  There is no env-merged ``effective``
    layer — the stored config is the source of truth.
    """

    stored: OrgConfigBase = Field(
        description="Raw config stored in the DB — only explicitly set fields.",
    )
