"""Project CRUD and membership endpoints.

Provides:
- ``POST   /v1/admin/projects``                          — Create a project
- ``GET    /v1/projects``                                 — List projects
- ``GET    /v1/projects/{project_id}``                    — Get project details
- ``PATCH  /v1/projects/{project_id}``                    — Update project
- ``DELETE /v1/projects/{project_id}``                    — Soft-delete project
- ``POST   /v1/projects/{project_id}/members``            — Add member
- ``GET    /v1/projects/{project_id}/members``            — List members
- ``PATCH  /v1/projects/{project_id}/members/{user_id}``  — Update member role
- ``DELETE /v1/projects/{project_id}/members/{user_id}``  — Remove member
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import get_dashboard_user, require_org_id, require_scope
from dependencies.db import get_db
from dependencies.services import get_project_service
from schemas.common import PaginatedResponse
from schemas.projects import (
    AddMemberRequest,
    CreateProjectRequest,
    MemberListResponse,
    MemberResponse,
    ProjectResponse,
    UpdateMemberRoleRequest,
    UpdateProjectRequest,
)
from services.project_service import ProjectService

# ── Admin router (org management) ─────────────────────────────────────────────

admin_router = APIRouter(
    prefix="/v1/admin/projects",
    tags=["Projects (Admin)"],
)

# ── User-facing router ───────────────────────────────────────────────────────

router = APIRouter(
    prefix="/v1/projects",
    tags=["Projects"],
)


# ═══════════════════════════════════════════════════════════════════════════════
# Admin: Create project
# ═══════════════════════════════════════════════════════════════════════════════


@admin_router.post(
    "",
    response_model=ProjectResponse,
    status_code=201,
    summary="Create a project",
    description="Create a new project within the authenticated organization.",
)
async def create_project(
    body: CreateProjectRequest,
    service: ProjectService = Depends(get_project_service),
    org_id: str = Depends(require_scope("admin:write")),
    dashboard_user_id: str = Depends(get_dashboard_user),
) -> ProjectResponse:
    """Create a new project within the organization."""
    project = await service.create_project(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        name=body.name,
        description=body.description,
    )
    # Add the creating dashboard user as an admin member
    await service.add_member(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        project_id=UUID(project.id),
        user_id=UUID(dashboard_user_id),
        role="admin",
    )
    return project


# ═══════════════════════════════════════════════════════════════════════════════
# List projects
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "",
    response_model=PaginatedResponse[ProjectResponse],
    summary="List projects",
    description="List all projects for the authenticated organization.",
)
async def list_projects(
    service: ProjectService = Depends(get_project_service),
    org_id: str = Depends(require_org_id),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of projects to return (1-200).",
    ),
    cursor: str | None = Query(
        default=None,
        description="Opaque cursor from a previous list response.",
    ),
) -> PaginatedResponse[ProjectResponse]:
    """List projects for the organization."""
    items, next_cursor = await service.list_projects(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        limit=limit,
        cursor=cursor,
    )
    return PaginatedResponse[ProjectResponse](
        data=items,
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Get project
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
    summary="Get a project",
    description="Get a single project by its UUID.",
)
async def get_project(
    project_id: UUID,
    service: ProjectService = Depends(get_project_service),
    org_id: str = Depends(require_org_id),
) -> ProjectResponse:
    """Get project details."""
    return await service.get_project(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        project_id=project_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Update project
# ═══════════════════════════════════════════════════════════════════════════════


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
    summary="Update a project",
    description="Update one or more fields of a project.",
)
async def update_project(
    project_id: UUID,
    body: UpdateProjectRequest,
    service: ProjectService = Depends(get_project_service),
    org_id: str = Depends(require_org_id),
) -> ProjectResponse:
    """Update project fields."""
    return await service.update_project(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        project_id=project_id,
        name=body.name,
        description=body.description,
        is_active=body.is_active,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Delete project
# ═══════════════════════════════════════════════════════════════════════════════


@router.delete(
    "/{project_id}",
    status_code=204,
    summary="Delete a project",
    description="Soft-delete a project.",
)
async def delete_project(
    project_id: UUID,
    service: ProjectService = Depends(get_project_service),
    org_id: str = Depends(require_org_id),
) -> None:
    """Soft-delete a project."""
    await service.delete_project(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        project_id=project_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Member Management
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/{project_id}/members",
    response_model=MemberResponse,
    status_code=201,
    summary="Add a project member",
    description="Add a user to a project with a specific role.",
)
async def add_project_member(
    project_id: UUID,
    body: AddMemberRequest,
    service: ProjectService = Depends(get_project_service),
    org_id: str = Depends(require_org_id),
) -> MemberResponse:
    """Add a user to a project."""
    return await service.add_member(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        project_id=project_id,
        user_id=body.user_id,
        role=body.role,
    )


@router.get(
    "/{project_id}/members",
    response_model=MemberListResponse,
    summary="List project members",
    description="List all members of a project.",
)
async def list_project_members(
    project_id: UUID,
    service: ProjectService = Depends(get_project_service),
    org_id: str = Depends(require_org_id),
) -> MemberListResponse:
    """List project members."""
    members = await service.list_members(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        project_id=project_id,
    )
    return MemberListResponse(members=members, total=len(members))


@router.patch(
    "/{project_id}/members/{user_id}",
    response_model=MemberResponse,
    summary="Update member role",
    description="Update a member's role within a project.",
)
async def update_project_member_role(
    project_id: UUID,
    user_id: UUID,
    body: UpdateMemberRoleRequest,
    service: ProjectService = Depends(get_project_service),
    org_id: str = Depends(require_org_id),
) -> MemberResponse:
    """Update a member's role."""
    return await service.update_member_role(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        project_id=project_id,
        user_id=user_id,
        role=body.role,
    )


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=204,
    summary="Remove a project member",
    description="Remove a user from a project.",
)
async def remove_project_member(
    project_id: UUID,
    user_id: UUID,
    service: ProjectService = Depends(get_project_service),
    org_id: str = Depends(require_org_id),
) -> None:
    """Remove a user from a project."""
    await service.remove_member(
        organization_id=UUID(org_id) if isinstance(org_id, str) else org_id,
        project_id=project_id,
        user_id=user_id,
    )
