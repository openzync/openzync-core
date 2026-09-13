"""Pydantic schemas for project and project-member CRUD operations.

Corresponds to the ``/v1/projects`` and ``/v1/projects/{project_id}/members``
endpoints.  Schemas must never import from ``models/``, ``services/``, or
``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ── Type alias ─────────────────────────────────────────────────────────────────

ProjectRole = Literal["owner", "member"]
"""Valid project membership roles.

- ``owner`` — can manage project settings and members.
- ``member`` — read/write access to project data.
"""


# ── Request schemas ────────────────────────────────────────────────────────────


class CreateProjectRequest(BaseModel):
    """Request body for ``POST /v1/projects``.

    Attributes:
        name: Human-readable project name. Must be unique within the
            organization. 1–255 characters.
        description: Optional longer description of the project's purpose.
        metadata: Optional key-value metadata for extensible configuration.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable project name. Unique within the organization.",
        examples=["Customer Support Bot", "Research Workspace", "Playground"],
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional description of the project's purpose.",
        examples=["Memory and graph data for our customer support analysis pipeline."],
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional key-value metadata for extensible configuration.",
        examples=[{"department": "support", "environment": "staging"}],
    )


class UpdateProjectRequest(BaseModel):
    """Request body for ``PUT /v1/projects/{project_id}``.

    All fields are optional (PATCH semantics).  Only provided fields are
    updated; omitted fields retain their existing values.

    Attributes:
        name: New project name (1–255 chars, unique within org).
        description: New description. Set to ``None`` to clear.
        metadata: New metadata. Deep-merged with existing metadata when
            provided as a partial dict.
        is_archived: Set ``True`` to archive the project, ``False`` to
            un-archive. Archived projects are hidden from default listing
            but data is preserved.
    """

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="New project name (1–255, unique within org).",
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="New description. Set to explicit ``None`` to clear.",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="New metadata. Deep-merged with existing when partial.",
    )
    is_archived: bool | None = Field(
        default=None,
        description="Archive (``True``) or un-archive (``False``) the project.",
    )


class AddMemberRequest(BaseModel):
    """Request body for ``POST /v1/projects/{project_id}/members``.

    Attributes:
        user_id: The UUID of the user to add to the project.
        role: Project role — ``owner`` or ``member``. Defaults to
            ``"member"`` if omitted.
    """

    user_id: UUID = Field(
        ...,
        description="UUID of the user to add to the project.",
    )
    role: ProjectRole = Field(
        default="member",
        description="Project role. 'owner' or 'member' (default: 'member').",
    )


# ── Response schemas ───────────────────────────────────────────────────────────


class ProjectResponse(BaseModel):
    """Full project representation returned from single-item endpoints.

    Includes computed ``member_count`` populated by the service layer.
    Use ``ProjectListResponse`` for compact list views.

    Attributes:
        id: Project UUID.
        name: Human-readable project name.
        description: Optional project description.
        metadata: Key-value metadata (maps from the ORM's ``metadata_``
            attribute via ``from_attributes``).
        is_archived: Whether the project is archived (soft-hidden).
        member_count: Number of members in this project (computed).
        created_by: UUID of the user who created the project, or ``None``
            if the creator was deleted.
        created_at: Project creation timestamp (UTC).
        updated_at: Last modification timestamp (UTC).
    """

    id: UUID = Field(..., description="Project UUID.")
    name: str = Field(..., description="Human-readable project name.")
    description: str | None = Field(
        default=None, description="Optional project description."
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_",
        description="Key-value metadata for extensible configuration.",
    )
    is_archived: bool = Field(
        default=False, description="Whether the project is archived."
    )
    member_count: int = Field(
        default=0, ge=0, description="Number of members in this project."
    )
    created_by: UUID | None = Field(
        default=None,
        description="UUID of the creating user, or ``None`` if deleted.",
    )
    created_at: datetime = Field(
        ..., description="Project creation timestamp (UTC)."
    )
    updated_at: datetime = Field(
        ..., description="Last modification timestamp (UTC)."
    )

    model_config = ConfigDict(from_attributes=True)


class ProjectListResponse(BaseModel):
    """Lightweight project representation for list endpoints.

    Omits ``metadata`` and ``description`` to keep list responses compact.
    Use the individual ``GET /v1/projects/{project_id}`` endpoint for full
    details.

    Attributes:
        id: Project UUID.
        name: Human-readable project name.
        is_archived: Whether the project is archived.
        member_count: Number of members in this project.
        created_by: UUID of the creating user, or ``None``.
        created_at: Project creation timestamp (UTC).
        updated_at: Last modification timestamp (UTC).
    """

    id: UUID = Field(..., description="Project UUID.")
    name: str = Field(..., description="Human-readable project name.")
    is_archived: bool = Field(
        default=False, description="Whether the project is archived."
    )
    member_count: int = Field(
        default=0, ge=0, description="Number of members in this project."
    )
    created_by: UUID | None = Field(
        default=None,
        description="UUID of the creating user, or ``None`` if deleted.",
    )
    created_at: datetime = Field(
        ..., description="Project creation timestamp (UTC)."
    )
    updated_at: datetime = Field(
        ..., description="Last modification timestamp (UTC)."
    )

    model_config = ConfigDict(from_attributes=True)


class ProjectMemberResponse(BaseModel):
    """A single project membership record.

    Returned from member list and management endpoints.

    Attributes:
        id: Membership record UUID.
        user_id: UUID of the user.
        role: Project role — ``"owner"`` or ``"member"``.
        created_at: Membership creation timestamp (UTC).
    """

    id: UUID = Field(..., description="Membership record UUID.")
    user_id: UUID = Field(..., description="UUID of the user.")
    role: ProjectRole = Field(..., description="Project role: 'owner' or 'member'.")
    created_at: datetime = Field(
        ..., description="Membership creation timestamp (UTC)."
    )

    model_config = ConfigDict(from_attributes=True)
