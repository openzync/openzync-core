"""Pydantic models for the project domain."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    """Request body for ``POST /v1/projects``."""

    name: str = Field(..., min_length=1, max_length=255, description="Project display name.")
    description: str | None = Field(default=None, max_length=2000, description="Optional description.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary metadata.")


class UpdateProjectRequest(BaseModel):
    """Request body for ``PUT /v1/projects/{project_id}``."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] | None = None
    is_archived: bool | None = None


class ProjectResponse(BaseModel):
    """Response from project CRUD endpoints."""

    id: str = Field(..., description="Internal UUID.")
    name: str = Field(..., description="Project display name.")
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_archived: bool = Field(default=False)
    member_count: int = Field(default=0, ge=0)
    created_by: str | None = Field(default=None, description="User UUID who created the project.")
    created_at: str = Field(..., description="ISO-8601 creation timestamp.")
    updated_at: str = Field(..., description="ISO-8601 update timestamp.")


class AddMemberRequest(BaseModel):
    """Request body for ``POST /v1/projects/{project_id}/members``."""

    user_id: str = Field(..., description="UUID of the user to add.")
    role: str = Field(default="member", pattern=r"^(owner|member)$", description="Project role.")


class ProjectMemberResponse(BaseModel):
    """Response for project member endpoints."""

    id: str = Field(..., description="Internal UUID.")
    project_id: str = Field(..., description="Project UUID.")
    user_id: str = Field(..., description="User UUID.")
    role: str = Field(..., description="Project role: owner or member.")
    created_at: str = Field(..., description="ISO-8601 timestamp.")
