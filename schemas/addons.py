"""Addon schemas — request/response models for the admin addon API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AddonResponse(BaseModel):
    """One installed addon with live state."""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="Addon slug (matches addon.toml name).")
    version: str = Field(description="Loaded version.")
    enabled: bool = Field(description="Whether routes/tasks serve traffic.")
    status: str = Field(description="Lifecycle marker.")
    hooks: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class AddonListResponse(BaseModel):
    """Paginated addon listing (install base is small — single page)."""

    data: list[AddonResponse]
    total: int


class AddonHealthResponse(BaseModel):
    """Per-addon health: DB state plus registered probe results."""

    id: str
    enabled: bool
    status: str
    version: str
    checks: dict[str, str] = Field(
        default_factory=dict, description="check name -> ok/error detail."
    )
    degraded: str | None = Field(
        default=None, description="Load failure reason, if any."
    )
