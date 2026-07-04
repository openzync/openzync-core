"""Pydantic schemas for project-scoped API key management.

Used by project settings for listing, creating, and revoking API keys
scoped to a specific project.  The raw key value is returned exactly
once at creation time.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateApiKeyRequest(BaseModel):
    """Request body for ``POST /v1/projects/{project_id}/api-keys``."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable label for the new API key.",
        examples=["Production Key", "CI/CD Key"],
    )


class ApiKeyResponse(BaseModel):
    """Response body for a single API key.

    The ``raw_key`` field is only populated on creation and is
    ``None`` in list/get responses.
    """

    id: UUID = Field(..., description="API key UUID.")
    name: str = Field(..., description="Human-readable label.")
    prefix: str = Field(..., description="Key prefix (e.g. ``oz_live_``).")
    project_id: UUID = Field(
        ..., description="Project UUID this key is scoped to."
    )
    created_by: UUID | None = Field(
        default=None,
        description="UUID of the user who created this key. "
        "``None`` for keys created before this field was added.",
    )
    scopes: list[str] = Field(
        ..., description="Permission scopes.", examples=[["read", "write"]]
    )
    is_revoked: bool = Field(
        ..., description="Whether the key has been revoked."
    )
    last_used_at: datetime | None = Field(
        default=None, description="Last usage timestamp."
    )
    created_at: datetime = Field(
        ..., description="Key creation timestamp (UTC)."
    )
    raw_key: str | None = Field(
        default=None,
        description="Full API key string — only populated on creation.",
    )

    model_config = ConfigDict(from_attributes=True)


class ApiKeyCreatedResponse(ApiKeyResponse):
    """Response for key creation — includes the raw key (shown once).

    Attributes:
        raw_key: The full API key string. **Shown only once** — not
            retrievable later.
        message: Warning to save the key.
    """

    raw_key: str = Field(
        ..., description="Full API key string — save this, it won't be shown again."
    )
    message: str = Field(
        default="Save this API key — it will not be shown again.",
        description="Warning that the key will not be retrievable later.",
    )


class ApiKeyListResponse(BaseModel):
    """Paginated response for ``GET /v1/projects/{project_id}/api-keys``."""

    data: list[ApiKeyResponse] = Field(
        ..., description="List of API keys."
    )
    total: int = Field(..., description="Total number of keys (excluding revoked).")
