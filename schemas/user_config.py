"""Pydantic schemas for per-user config overrides.

Canonical paths: ``GET`` / ``PATCH /v1/me/config`` (routers/me.py).
The former ``/v1/auth/me/config`` alias was removed (BREAKING) — no
compat shims are kept.

A user override is a partial config dict layered over the platform
system config — user keys win. Only keys present in the override apply; the
effective config is resolved by
``services.user_config_service.get_effective_config``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserConfigUpdate(BaseModel):
    """Request body for ``PATCH /v1/me/config``.

    Partial deep-merge onto the stored override: provided keys override
    recursively, ``None`` removes that **top-level** key (nested removal
    is not supported — a nested ``None`` replaces the nested value, and
    only a top-level ``None`` deletes).  Unknown keys are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any] = Field(
        ...,
        description="Partial config keys to merge into the user's override.",
    )


class UserConfigResponse(BaseModel):
    """Response body for ``GET /v1/me/config``.

    Attributes:
        override: Only the keys the user explicitly set.
        effective: The merged ``user ⊕ system`` view actually applied.
    """

    override: dict[str, Any] = Field(
        default_factory=dict,
        description="Keys the user explicitly set (empty when none).",
    )
    effective: dict[str, Any] = Field(
        default_factory=dict,
        description="Merged user ⊕ system config actually applied.",
    )
    system_managed_fields: list[str] = Field(
        default_factory=list,
        description="Field names configured at the system level "
        "and not overridable via this API.",
    )
