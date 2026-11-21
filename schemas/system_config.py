"""Pydantic schemas for platform system configuration.

The platform operator (superadmin) manages a small whitelist of
non-secret system-level defaults through ``core.system_config``, which
persists them in the platform root user's ``users.config_override``
alongside other platform keys.

**Secrets are never part of these schemas — by construction.**  The
whitelist contains only non-secret tunables.  ``*_api_key``,
``*_password``, ``*_secret``, ``DATABASE_URL`` and ``SMTP_*`` fields
do not exist on either model, so a request carrying them fails
validation (``extra="forbid"``) and a response can never leak them.

⚠️ BREAKING: ``org_creation_policy`` / ``approval_scope`` are removed.
Single platform org — registration is governed by
``instance_settings.registration_mode`` (``open`` / ``invite_only`` /
``approval``), not by config policy keys.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from schemas.organization_config import (  # noqa: TC001 — runtime import: pydantic resolves field types
    LlmBackend,
)


class SystemConfigUpdate(BaseModel):
    """Editable whitelist for ``PATCH /admin/system/config``.

    Unknown keys are rejected with 422 (``extra="forbid"``) — a secret
    key such as ``openai_api_key`` or ``DATABASE_URL`` can never be
    written here.  Every field is optional; only provided fields update.
    """

    model_config = {"extra": "forbid"}

    # ── Non-secret system-level defaults (mirror OrgConfigBase keys) ──────
    llm_backend: LlmBackend | None = None
    llm_model: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    llm_max_tokens: int | None = Field(default=None, ge=1)
    # ⚠️ BREAKING: embedding_backend / embedding_model / embedding_dim were
    # write-only (nothing ever read them back) and are removed.  Embeddings
    # are a local ONNX model with no configuration surface.
    graph_backend: str | None = None
    graph_search_type: str | None = None
    graph_max_traversal_depth: int | None = Field(default=None, ge=1, le=10)
    reranker_backend: str | None = None
    reranker_model: str | None = None
    reranker_top_k: int | None = Field(default=None, ge=10, le=200)
    reranker_top_n: int | None = Field(default=None, ge=1, le=100)
    context_cache_ttl: int | None = Field(default=None, ge=1)

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


class SystemConfigResponse(BaseModel):
    """System config as exposed to the platform UI.

    Mirrors :class:`SystemConfigUpdate` — the same whitelist, no secrets.
    """

    llm_backend: LlmBackend | None = None
    llm_model: str | None = None
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    graph_backend: str | None = None
    graph_search_type: str | None = None
    graph_max_traversal_depth: int | None = None
    reranker_backend: str | None = None
    reranker_model: str | None = None
    reranker_top_k: int | None = None
    reranker_top_n: int | None = None
    context_cache_ttl: int | None = None

    @field_validator("graph_backend", mode="before")
    @classmethod
    def _reject_postgres(cls, v: str | None) -> str | None:
        """Hard-reject `postgres` — removed in v1.1.0, returns 410 Gone."""
        if v == "postgres":
            # Plain ValueError (never GoneError): see SystemConfigUpdate.
            raise ValueError(
                "PostgreSQL graph backend deprecated — gone, removed in v1.1.0. "
                "Migrate to `falkordb` (410)."
            )
        return v


# ── Whitelist helpers ─────────────────────────────────────────────────────────

#: Every key ``core.system_config`` may read from / write to the platform
#: root user's ``config_override``.  Single source of truth for the whitelist.
SYSTEM_CONFIG_WHITELIST: frozenset[str] = frozenset(SystemConfigResponse.model_fields)
