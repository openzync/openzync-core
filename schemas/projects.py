"""Pydantic schemas for project and project-member CRUD operations.

Corresponds to the ``/v1/projects`` and ``/v1/projects/{id}/members`` endpoints.
Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    """Request body for POST /v1/admin/projects.

    Attributes:
        name: Human-readable project name. Unique within the org.
        description: Optional longer description.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable project name. Unique within the org.",
        examples=["Customer Support", "Research & Development"],
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional project description.",
    )


class UpdateProjectRequest(BaseModel):
    """Request body for PATCH /v1/projects/{project_id}."""

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="New project name.",
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="New project description.",
    )
    is_active: bool | None = Field(
        default=None,
        description="Toggle project active state.",
    )


class ProjectResponse(BaseModel):
    """Response body for single-project GET and POST endpoints."""

    id: UUID = Field(..., description="Internal project UUID.")
    organization_id: UUID = Field(..., description="Owning organization UUID.")
    name: str = Field(..., description="Project name.")
    description: str | None = Field(default=None, description="Project description.")
    is_active: bool = Field(default=True, description="Whether the project is active.")
    created_at: datetime = Field(..., description="Creation timestamp (UTC).")
    updated_at: datetime = Field(..., description="Last update timestamp (UTC).")

    model_config = {"from_attributes": True}


class ProjectListResponse(BaseModel):
    """Lightweight representation for project list endpoints."""

    id: UUID = Field(..., description="Internal project UUID.")
    name: str = Field(..., description="Project name.")
    description: str | None = Field(default=None, description="Project description.")
    is_active: bool = Field(default=True, description="Whether the project is active.")
    created_at: datetime = Field(..., description="Creation timestamp (UTC).")

    model_config = {"from_attributes": True}


class AddMemberRequest(BaseModel):
    """Request body for POST /v1/projects/{project_id}/members.

    Attributes:
        user_id: UUID of the user to add to the project.
        role: Project role — ``admin``, ``member``, or ``viewer``.
    """

    user_id: UUID = Field(
        ...,
        description="UUID of the user to add to the project.",
    )
    role: str = Field(
        default="member",
        pattern=r"^(admin|member|viewer)$",
        description="Project role: admin, member, or viewer.",
    )


class UpdateMemberRoleRequest(BaseModel):
    """Request body for PATCH /v1/projects/{project_id}/members/{user_id}."""

    role: str = Field(
        ...,
        pattern=r"^(admin|member|viewer)$",
        description="New project role: admin, member, or viewer.",
    )


class MemberResponse(BaseModel):
    """Response body for project member data."""

    user_id: UUID = Field(..., description="User UUID.")
    project_id: UUID = Field(..., description="Project UUID.")
    role: str = Field(..., description="Role within the project: admin/member/viewer.")
    created_at: datetime = Field(..., description="Membership creation timestamp (UTC).")

    model_config = {"from_attributes": True}


class MemberListResponse(BaseModel):
    """List of project members."""

    members: list[MemberResponse] = Field(
        default_factory=list, description="Project member list."
    )
    total: int = Field(default=0, description="Total member count.")
