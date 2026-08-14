"""Pydantic schemas for platform system configuration.

The platform operator (superadmin) manages a small whitelist of
system-level defaults through ``core.system_config``, which persists them
in the OpenBao system secret alongside the ``OZ_*`` settings.

**Secrets are never part of these schemas — by construction.**  The
whitelist contains only non-secret tunables (policies + non-secret org
config defaults).  ``*_api_key``, ``*_password``, ``*_secret``,
``DATABASE_URL`` and ``SMTP_*`` fields do not exist on either model, so a
request carrying them fails validation (``extra="forbid"``) and a
response can never leak them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class OrgCreationPolicy(StrEnum):
    """Platform policy controlling how new organizations are created."""

    allow_all = "allow_all"
    """Signup and in-app org requests create orgs instantly (pre-feature behaviour)."""

    reject_all = "reject_all"
    """No new orgs may be created through any channel."""

    approvals = "approvals"
    """New orgs enter a pending state and require superadmin approval."""


class ApprovalScope(StrEnum):
    """Which channels the ``approvals`` policy applies to."""

    in_app = "in_app"
    """Only the in-app request channel (``POST /v1/org-requests``)."""

    public_signup = "public_signup"
    """Only the public signup channel (``POST /v1/auth/signup``)."""

    both = "both"
    """Both channels."""


class SystemConfigUpdate(BaseModel):
    """Editable whitelist for ``PATCH /admin/system/config``.

    Unknown keys are rejected with 422 (``extra="forbid"``) — a secret
    key such as ``openai_api_key`` or ``DATABASE_URL`` can never be
    written here.  Every field is optional; only provided fields update.
    """

    model_config = {"extra": "forbid"}

    # ── Platform policies ─────────────────────────────────────────────────
    org_creation_policy: OrgCreationPolicy | None = Field(
        default=None,
        description="Policy for new-organization creation across all channels.",
    )
    approval_scope: ApprovalScope | None = Field(
        default=None,
        description="Channels gated by the approvals policy.",
    )

    # ── Non-secret system-level defaults (mirror OrgConfigBase keys) ──────
    llm_backend: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    llm_max_tokens: int | None = Field(default=None, ge=1)
    embedding_backend: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = Field(default=None, ge=64, le=4096)
    graph_backend: str | None = None
    graph_search_type: str | None = None
    graph_max_traversal_depth: int | None = Field(default=None, ge=1, le=10)
    reranker_backend: str | None = None
    reranker_model: str | None = None
    reranker_top_k: int | None = Field(default=None, ge=10, le=200)
    reranker_top_n: int | None = Field(default=None, ge=1, le=100)
    context_cache_ttl: int | None = Field(default=None, ge=1)


class SystemConfigResponse(BaseModel):
    """System config as exposed to the platform UI.

    Mirrors :class:`SystemConfigUpdate` — the same whitelist, no secrets.
    ``org_creation_policy`` and ``approval_scope`` default to
    ``allow_all`` / ``both`` when OpenBao has no record (backward
    compatible — existing installs keep working).
    """

    org_creation_policy: OrgCreationPolicy = Field(
        default=OrgCreationPolicy.allow_all,
        description="Current new-org creation policy.",
    )
    approval_scope: ApprovalScope = Field(
        default=ApprovalScope.both,
        description="Channels gated by the approvals policy.",
    )

    llm_backend: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    embedding_backend: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    graph_backend: str | None = None
    graph_search_type: str | None = None
    graph_max_traversal_depth: int | None = None
    reranker_backend: str | None = None
    reranker_model: str | None = None
    reranker_top_k: int | None = None
    reranker_top_n: int | None = None
    context_cache_ttl: int | None = None


# ── Whitelist helpers ─────────────────────────────────────────────────────────

#: Every key ``core.system_config`` may read from / write to the OpenBao
#: system secret.  Single source of truth for the whitelist.
SYSTEM_CONFIG_WHITELIST: frozenset[str] = frozenset(
    SystemConfigResponse.model_fields
)
