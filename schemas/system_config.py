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

⚠️ BREAKING: ``llm_*`` / ``reranker_*`` are removed from platform
defaults (org-level only).  Requests carrying them 422
(``extra="forbid"``); responses never include them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from schemas.organization_config import ENRICHMENT_KEYS  # noqa: TC001 — runtime import: pydantic resolves field types


class EnrichmentGateSystemConfig(BaseModel):
    """System-level enrichment gate block (non-secret tunables only).

    The gate skips the single LLM call for low-signal episodes.  The Hub
    token itself is never stored here — the worker reads ``HF_TOKEN``
    from the environment at call time.
    """

    enabled: bool = False
    threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Minimum should_enrich score to run the LLM.",
    )
    model: str = "OpenZync/enrichment-gating-v1"


class SystemConfigUpdate(BaseModel):
    """Editable whitelist for ``PATCH /admin/system/config``.

    Unknown keys are rejected with 422 (``extra="forbid"``) — a secret
    key such as ``openai_api_key`` or ``DATABASE_URL`` can never be
    written here.  Every field is optional; only provided fields update.
    """

    model_config = {"extra": "forbid"}

    # ── Non-secret system-level defaults (mirror OrgConfigBase keys) ──────
    # ⚠️ BREAKING: llm_* / reranker_* removed from platform defaults.
    # LLM/reranker live at org level only — platform accepts graph,
    # cache, enrichment, and gate keys.  (Unknown keys 422 via forbid.)
    graph_backend: str | None = None
    graph_search_type: str | None = None
    graph_max_traversal_depth: int | None = Field(default=None, ge=1, le=10)
    context_cache_ttl: int | None = Field(default=None, ge=1)
    enrichment: dict[str, bool] | None = Field(
        default=None,
        description="System-level default enrichment toggles (entities, "
        "embedding, facts, entity_links, classification, structured, "
        "observations, blob_text, llm_invalidation). Unset keys default "
        "to True; org config and per-ingest params override.",
    )
    enrichment_gate: EnrichmentGateSystemConfig | None = Field(
        default=None,
        description="System-level enrichment gate (enabled, threshold, "
        "model). Unset means the gate defaults (disabled).",
    )

    @field_validator("enrichment", mode="before")
    @classmethod
    def _validate_enrichment(
        cls, v: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        """Reject unknown toggle keys and non-boolean values (→ 422).

        Values must be real booleans — truthy coercion would silently
        enable a toggle the caller tried to disable (``bool("false")``
        is ``True``).
        """
        if v is None:
            return v
        unknown = set(v) - ENRICHMENT_KEYS
        if unknown:
            raise ValueError(
                f"Unknown enrichment keys: {sorted(unknown)}. "
                f"Expected a subset of: {sorted(ENRICHMENT_KEYS)}."
            )
        for key, value in v.items():
            if not isinstance(value, bool):
                raise ValueError(
                    f"Enrichment toggle {key!r} must be a boolean, "
                    f"got {type(value).__name__}."
                )
        return v

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

    ⚠️ BREAKING: llm_* / reranker_* removed — org-level only now.
    """

    graph_backend: str | None = None
    graph_search_type: str | None = None
    graph_max_traversal_depth: int | None = None
    context_cache_ttl: int | None = None
    enrichment: dict[str, bool] = Field(
        default_factory=lambda: {
            "entities": True,
            "embedding": True,
            "facts": True,
            "entity_links": True,
            "classification": True,
            "structured": True,
            "observations": True,
            "blob_text": True,
            "llm_invalidation": True,
        },
        description="Effective system-level enrichment toggles — all-True "
        "when nothing is stored.",
    )
    enrichment_gate: EnrichmentGateSystemConfig | None = Field(
        default=None,
        description="Effective system-level enrichment gate — None "
        "when nothing is stored (gate defaults apply: disabled).",
    )

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
