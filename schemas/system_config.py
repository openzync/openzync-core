"""Pydantic schemas for platform system configuration.

The platform operator (admin) manages a small whitelist of
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

⚠️ BREAKING: ``llm_*`` / ``reranker_*`` / ``graph_*`` /
``context_cache_ttl`` were removed from platform defaults (user-level
only).  ``enrichment_gate`` is removed too — the gate lives at user
level only now (``users.config_override``), with the model fixed to the
platform default.  Both models expose no fields: ``GET`` returns ``{}``
and any stored/carried key 422s (``extra="forbid"``).
"""

from __future__ import annotations

from pydantic import BaseModel


class SystemConfigUpdate(BaseModel):
    """Editable whitelist for ``PATCH /admin/system/config``.

    Unknown keys are rejected with 422 (``extra="forbid"``) — a secret
    key such as ``openai_api_key`` or ``DATABASE_URL`` can never be
    written here.  Every field is optional; only provided fields update.

    ⚠️ BREAKING: the whitelist is currently empty — every key (including
    the former ``enrichment_gate``) is user-level only and 422s here.
    """

    model_config = {"extra": "forbid"}


class SystemConfigResponse(BaseModel):
    """System config as exposed to the platform UI.

    ⚠️ BREAKING: all former keys (``llm_*`` / ``reranker_*`` / ``graph_*``
    / ``context_cache_ttl`` / ``enrichment_gate``) are user-level only
    now.  No fields remain — the response is ``{}``.
    """


# ── Whitelist helpers ─────────────────────────────────────────────────────────

#: Every key ``core.system_config`` may read from / write to the platform
#: root user's ``config_override``.  Single source of truth for the whitelist.
SYSTEM_CONFIG_WHITELIST: frozenset[str] = frozenset(SystemConfigResponse.model_fields)
