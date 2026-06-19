"""Project router — CRUD and member management for projects.

All endpoints are scoped to the authenticated user's organization.  Project
management endpoints (create, update, archive, member management) require
the user to be a project owner.  Read endpoints (get, list) are available
to any authenticated user within the organization.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, status

from core.exceptions import NotFoundError, ValidationError
from dependencies.db import get_db
from dependencies.project_auth import require_project_membership, require_project_owner
from repositories.project_repository import ProjectRepository
from schemas.projects import (
    AddMemberRequest,
    CreateProjectRequest,
    ProjectMemberResponse,
    ProjectResponse,
    UpdateProjectRequest,
)
from services.project_service import ProjectService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/v1/projects", tags=["projects"])


# ── Helper ──────────────────────────────────────────────────────────────────


async def _get_project_service(db: AsyncSession = Depends(get_db)) -> ProjectService:
    """Factory for request-scoped ProjectService."""
    return ProjectService(repo=ProjectRepository(db))


# ── Create ──────────────────────────────────────────────────────────────────


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    request: Request,
    payload: CreateProjectRequest,
    service: ProjectService = Depends(_get_project_service),
) -> ProjectResponse:
    """Create a new project.

    The authenticated user is automatically added as the project owner.
    """
    user_id = UUID(request.state.user_id)
    return await service.create_project(
        organization_id=request.state.org_id,
        user_id=user_id,
        payload=payload,
    )


# ── Read ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: ProjectService = Depends(_get_project_service),
) -> list[ProjectResponse]:
    """List non-archived projects the authenticated user is a member of."""
    return await service.list_projects(
        organization_id=request.state.org_id,
        user_id=UUID(request.state.user_id),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
    dependencies=[Depends(require_project_membership)],
)
async def get_project(
    project_id: UUID = Path(...),
    request: Request = None,
    service: ProjectService = Depends(_get_project_service),
) -> ProjectResponse:
    """Get a single project by ID."""
    return await service.get_project(
        organization_id=request.state.org_id,
        project_id=project_id,
    )


# ── Update ──────────────────────────────────────────────────────────────────


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
    dependencies=[Depends(require_project_owner)],
)
async def update_project(
    payload: UpdateProjectRequest,
    project_id: UUID = Path(...),
    request: Request = None,
    service: ProjectService = Depends(_get_project_service),
) -> ProjectResponse:
    """Update project name and/or description.

    Requires owner role.
    """
    return await service.update_project(
        organization_id=request.state.org_id,
        project_id=project_id,
        payload=payload,
    )


# ── Archive ─────────────────────────────────────────────────────────────────


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_project_owner)],
)
async def archive_project(
    project_id: UUID = Path(...),
    request: Request = None,
    service: ProjectService = Depends(_get_project_service),
) -> None:
    """Archive a project (soft-delete, preserves all data).

    Requires owner role.
    """
    await service.archive_project(
        organization_id=request.state.org_id,
        project_id=project_id,
    )


# ── Members ─────────────────────────────────────────────────────────────────


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_project_owner)],
)
async def add_member(
    payload: AddMemberRequest,
    project_id: UUID = Path(...),
    service: ProjectService = Depends(_get_project_service),
) -> ProjectMemberResponse:
    """Add a user to a project.

    Requires owner role.
    """
    return await service.add_member(
        project_id=project_id,
        payload=payload,
    )


@router.get(
    "/{project_id}/members",
    response_model=list[ProjectMemberResponse],
    dependencies=[Depends(require_project_membership)],
)
async def list_members(
    project_id: UUID = Path(...),
    service: ProjectService = Depends(_get_project_service),
) -> list[ProjectMemberResponse]:
    """List all members of a project.

    Requires membership.
    """
    return await service.list_members(project_id=project_id)


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_project_owner)],
)
async def remove_member(
    project_id: UUID = Path(...),
    user_id: UUID = Path(...),
    service: ProjectService = Depends(_get_project_service),
) -> None:
    """Remove a user from a project.

    Requires owner role.  Cannot remove the last owner.
    """
    await service.remove_member(
        project_id=project_id,
        user_id=user_id,
    )


@router.patch(
    "/{project_id}/members/{user_id}",
    response_model=ProjectMemberResponse,
    dependencies=[Depends(require_project_owner)],
)
async def update_member_role(
    role: str = Query(..., pattern="^(owner|member)$"),
    project_id: UUID = Path(...),
    user_id: UUID = Path(...),
    service: ProjectService = Depends(_get_project_service),
) -> ProjectMemberResponse:
    """Change a member's role within a project.

    Requires owner role.  Cannot downgrade the last owner.
    """
    return await service.update_member_role(
        project_id=project_id,
        user_id=user_id,
        role=role,
    )
