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

⚠️ BREAKING: ``llm_*`` / ``reranker_*`` / ``graph_*`` / ``context_cache_ttl``
are removed from platform defaults (org-level only).  Requests carrying
them 422 (``extra="forbid"``); responses never include them.  Only
``enrichment_gate`` remains (``enrichment`` is org-level only; the gate
``model`` is fixed to the platform default and not configurable).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EnrichmentGateSystemConfig(BaseModel):
    """System-level enrichment gate block (non-secret tunables only).

    The gate skips the single LLM call for low-signal episodes.  The Hub
    token itself is never stored here — the worker reads ``HF_TOKEN``
    from the environment at call time.  The gate model is fixed to the
    platform default — a request carrying ``model`` 422s
    (``extra="forbid"``).
    """

    model_config = {"extra": "forbid"}

    enabled: bool = False
    threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Minimum should_enrich score to run the LLM.",
    )


class SystemConfigUpdate(BaseModel):
    """Editable whitelist for ``PATCH /admin/system/config``.

    Unknown keys are rejected with 422 (``extra="forbid"``) — a secret
    key such as ``openai_api_key`` or ``DATABASE_URL`` can never be
    written here.  Every field is optional; only provided fields update.
    """

    model_config = {"extra": "forbid"}

    # ── Non-secret system-level defaults ─────────────────────────────────
    # ⚠️ BREAKING: llm_* / reranker_* / graph_* / context_cache_ttl removed
    # from platform defaults — org level only.  ``enrichment`` removed too
    # (org-level only); gate ``model`` is fixed, not configurable.
    # Only enrichment_gate remains.  (Unknown keys 422 via forbid.)
    enrichment_gate: EnrichmentGateSystemConfig | None = Field(
        default=None,
        description="System-level enrichment gate (enabled, threshold). "
        "Unset means the gate defaults (disabled).",
    )


class SystemConfigResponse(BaseModel):
    """System config as exposed to the platform UI.

    Mirrors :class:`SystemConfigUpdate` — the same whitelist, no secrets.

    ⚠️ BREAKING: llm_* / reranker_* / graph_* / context_cache_ttl removed —
    org-level only now.  ``enrichment`` removed too (org-level only).
    Only enrichment_gate remains.
    """

    enrichment_gate: EnrichmentGateSystemConfig | None = Field(
        default=None,
        description="Effective system-level enrichment gate — None "
        "when nothing is stored (gate defaults apply: disabled).",
    )


# ── Whitelist helpers ─────────────────────────────────────────────────────────

#: Every key ``core.system_config`` may read from / write to the platform
#: root user's ``config_override``.  Single source of truth for the whitelist.
SYSTEM_CONFIG_WHITELIST: frozenset[str] = frozenset(SystemConfigResponse.model_fields)
