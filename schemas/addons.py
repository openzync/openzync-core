"""Pydantic schemas for addon admin endpoints."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — pydantic resolves at runtime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AddonInstallRequest(BaseModel):
    """Install an addon from a local path or a github repo."""

    local_path: str | None = Field(default=None, description="Local dir to copy.")
    github_url: str | None = Field(default=None, description="owner/repo or URL.")
    tag: str | None = Field(default=None, description="Git tag (default latest).")


class AddonPanelResponse(BaseModel):
    """One admin-ui panel descriptor (iframe nav entry)."""

    id: str = Field(..., description="Panel id.")
    title: str = Field(..., description="Panel display title.")
    path: str = Field(..., description="Panel path relative to the addon ui dir.")


class AddonResponse(BaseModel):
    """One addon row."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Addon id.")
    version: str = Field(..., description="Installed version.")
    source_type: str = Field(..., description="local|github.")
    source_ref: str = Field(..., description="Path or owner/repo.")
    installed_sha: str | None = Field(default=None)
    latest_seen: str | None = Field(default=None)
    enabled: bool = Field(...)
    update_available: bool = Field(...)
    generation: int = Field(...)
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)
    panels: list[AddonPanelResponse] = Field(
        default_factory=list,
        description="Panel descriptors from the installed addon.toml.",
    )


class AddonListResponse(BaseModel):
    """List response for ``GET /admin/system/addons``."""

    data: list[AddonResponse] = Field(...)
    total: int = Field(...)


class AddonHealthResponse(BaseModel):
    """Health snapshot for one addon."""

    id: str = Field(...)
    enabled: bool = Field(...)
    loaded: bool = Field(...)
    generation: int = Field(...)
    version: str = Field(...)


class PanelTicketRequest(BaseModel):
    """Request a short-lived ticket for one addon panel."""

    panel_path: str = Field(
        ..., description="Manifest panel path (e.g. /ui/index.html)."
    )


class PanelTicketResponse(BaseModel):
    """Issued panel ticket (``?ticket=`` for the gated UI route)."""

    ticket: str = Field(..., description="Opaque ticket, 5-minute TTL.")


__all__ = [
    "AddonHealthResponse",
    "AddonInstallRequest",
    "AddonListResponse",
    "AddonPanelResponse",
    "AddonResponse",
    "PanelTicketRequest",
    "PanelTicketResponse",
]
