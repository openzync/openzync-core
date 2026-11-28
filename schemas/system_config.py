"""Pydantic schemas for platform system configuration.

The platform operator (admin) manages a small whitelist of
non-secret system-level defaults through ``core.system_config``, which
persists them in the platform root user's ``users.config_override``
alongside other platform keys.

**Secrets are never part of these schemas — by construction.**  The
whitelist contains only non-secret tunables.  ``*_api_key``,
``*_password``, ``*_secret``, ``DATABASE_URL`` and ``SMTP_*`` fields
do not exist on the model, so a request carrying them fails
validation (``extra="forbid"``) and a response can never leak them.

⚠️ BREAKING: ``org_creation_policy`` / ``approval_scope`` are removed.
Single platform org — registration is governed by
``instance_settings.registration_mode`` (``open`` / ``invite_only`` /
``approval``), not by config policy keys.

⚠️ BREAKING: ``llm_*`` / ``reranker_*`` / ``graph_*`` /
``context_cache_ttl`` were removed from platform defaults (user-level
only).  ``enrichment_gate`` is removed too — the gate lives at user
level only now (``users.config_override``), with the model fixed to the
platform default.  The model exposes no fields: ``GET`` returns ``{}``
and any stored/carried key 422s (``extra="forbid"``).
"""

from __future__ import annotations

from pydantic import BaseModel


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
