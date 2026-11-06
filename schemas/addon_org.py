"""Org-facing addon schemas — availability, settings, panels, health."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AddonPanelResponse(BaseModel):
    """One management panel descriptor from the manifest snapshot."""

    id: str = Field(description="Slug-like panel id.")
    title: str = Field(description="Human-readable panel title.")
    path: str = Field(description="Mount path under /ui/.")


class OrgAddonResponse(BaseModel):
    """One addon as seen by an org: platform state + org flag + metadata."""

    id: str = Field(description="Addon slug.")
    version: str = Field(description="Loaded version.")
    platform_enabled: bool = Field(description="Platform kill-switch state.")
    org_enabled: bool = Field(description="This org's flag (default False).")
    status: str = Field(description="Lifecycle marker.")
    degraded: str | None = Field(default=None, description="Load failure reason.")
    has_settings: bool = Field(description="Whether settings endpoints serve.")
    panels: list[AddonPanelResponse] = Field(default_factory=list)


class OrgAddonListResponse(BaseModel):
    """Available addons for one org (install base is small — single page)."""

    data: list[OrgAddonResponse]
    total: int


class AddonSettingsResponse(BaseModel):
    """Settings schema plus masked values (secrets read as ``***``)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    schema_: dict[str, Any] = Field(
        alias="schema", description="The snapshotted settings.schema.json."
    )
    values: dict[str, Any] = Field(description="Masked org values.")


class UpdateAddonSettingsRequest(BaseModel):
    """Full-replacement settings values (``***`` keeps a stored secret)."""

    # Any: values are schemaless JSON validated server-side against the schema.
    values: dict[str, Any] = Field(description="Complete new values object.")


class RevealAddonSecretsRequest(BaseModel):
    """Explicit secret names to reveal in plaintext (audited)."""

    keys: list[str] = Field(description="Secret setting names to reveal.")


class RevealAddonSecretsResponse(BaseModel):
    """Plaintext secrets (``None`` for never-set keys)."""

    id: str
    secrets: dict[str, Any | None]


class AddonPanelsResponse(BaseModel):
    """Panel descriptors plus enable state."""

    id: str
    platform_enabled: bool
    org_enabled: bool
    panels: list[AddonPanelResponse] = Field(default_factory=list)


class OrgAddonHealthResponse(BaseModel):
    """Per-org health view: enable state plus load/degraded info."""

    id: str
    platform_enabled: bool
    org_enabled: bool
    status: str
    loaded: bool = Field(description="Whether a live record is mounted.")
    degraded: str | None = Field(default=None)
