"""Project router — CRUD and member management for projects.

All endpoints are scoped to the authenticated user's scope.  Project
management endpoints (create, update, archive, member management) require
the user to be a project owner.  Read endpoints (get, list) are available
to any authenticated user within the user's scope.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import audit_action
from core.exceptions import AuthenticationError
from dependencies.auth import (
    _check_permission,
    _ensure_admin,
    require_permission,
    require_user_id,
)
from dependencies.db import get_db
from dependencies.project_auth import require_project_membership
from dependencies.services import get_invite_service
from repositories.instance_settings_repository import InstanceSettingsRepository
from repositories.project_pin_repository import ProjectPinRepository
from repositories.project_repository import ProjectRepository
from repositories.user_repository import UserRepository
from schemas.projects import (
    AddMemberRequest,
    CreateProjectRequest,
    MemberApprovalRequest,
    ProjectMemberResponse,
    ProjectResponse,
    UpdateProjectRequest,
)
from schemas.sorting import ProjectMemberSortBy, ProjectSortBy, SortDir, SortSpec
from services.invite_service import InviteService
from services.project_membership_service import ProjectMembershipService
from services.project_service import ProjectService

router = APIRouter(prefix="/v1/projects", tags=["projects"])


# ── Helper ──────────────────────────────────────────────────────────────────


async def _get_project_service(db: AsyncSession = Depends(get_db)) -> ProjectService:
    """Factory for request-scoped ProjectService."""
    return ProjectService(repo=ProjectRepository(db), pin_repo=ProjectPinRepository(db))


async def _get_membership_service(
    db: AsyncSession = Depends(get_db),
    invite_service: InviteService = Depends(get_invite_service),
) -> ProjectMembershipService:
    """Factory for request-scoped ProjectMembershipService.

    Shares the request session across repos (FastAPI caches ``get_db``
    per request), so the invite-row insert and the membership insert
    commit atomically via ``get_db``.
    """
    return ProjectMembershipService(
        repo=ProjectRepository(db),
        user_repo=UserRepository(db),
        settings_repo=InstanceSettingsRepository(db),
        invite_service=invite_service,
    )


# ── Create ──────────────────────────────────────────────────────────────────


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
@audit_action("project.create", "project", "Project created")
async def create_project(
    request: Request,
    payload: CreateProjectRequest,
    # ⚠️ BREAKING: non-manager members (no ``project:manage``) now get 403
    # when creating projects — previously any authenticated principal could.
    _: None = Depends(require_permission("project:manage")),
    service: ProjectService = Depends(_get_project_service),
) -> ProjectResponse:
    """Create a new project.

    For JWT-authenticated (dashboard) users, the creator is automatically
    added as the project owner.  For API-key-authenticated requests, the
    project is created without owner attribution — use the dashboard to
    manage members.
    """
    raw_user_id: str | None = getattr(request.state, "user_id", None)
    user_id: UUID | None = UUID(raw_user_id) if raw_user_id else None
    return await service.create_project(

        user_id=user_id,
        payload=payload,
    )


# ── Read ────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    pinned_only: bool = Query(default=False),
    include_archived: bool = Query(default=False),
    sort_by: ProjectSortBy | None = Query(
        default=None,
        description="Sort key (default created_at, pinned_at for pinned_only).",
    ),
    sort_dir: SortDir = Query(
        default="desc",
        description="Sort direction (default desc).",
    ),
    _: None = Depends(require_permission("project:read")),
    service: ProjectService = Depends(_get_project_service),
) -> list[ProjectResponse]:
    """List projects in the platform (archived excluded by default).

    For JWT-authenticated dashboard users, returns only projects the user
    is a member of.  For API-key-authenticated SDK requests, returns all
    projects in the platform.  Pass ``include_archived=True`` to include
    archived projects.  ``pinned_only`` always excludes archived projects.
    """
    raw_user_id: str | None = getattr(request.state, "user_id", None)
    auth_type: str | None = getattr(request.state, "auth_type", None)
    # API key auth: list all platform projects without membership filter.
    # The key's creator UUID (created_by) is available as user_id for
    # attribution on write endpoints, but for listing we show all projects
    # in the platform.  The key's project scope is enforced individually by
    # require_project_membership on project-scoped endpoints.
    user_id: UUID | None = (
        UUID(raw_user_id) if raw_user_id and auth_type != "api_key" else None
    )
    return await service.list_projects(

        user_id=user_id,
        limit=limit,
        offset=offset,
        pinned_only=pinned_only,
        include_archived=include_archived,
        sort=SortSpec(sort_by=sort_by, sort_dir=sort_dir),
    )


@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:read")),
    ],
)
async def get_project(
    project_id: UUID = Path(...),
    request: Request = None,
    service: ProjectService = Depends(_get_project_service),
    user_id: str = Depends(require_user_id),
) -> ProjectResponse:
    """Get a single project by ID."""
    return await service.get_project(
        user_id=UUID(user_id),
        project_id=project_id,
    )


# ── Update ──────────────────────────────────────────────────────────────────


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:manage")),
    ],
)
@audit_action("project.update", "project", "Project updated")
async def update_project(
    payload: UpdateProjectRequest,
    project_id: UUID = Path(...),
    request: Request = None,
    service: ProjectService = Depends(_get_project_service),
    user_id: str = Depends(require_user_id),
) -> ProjectResponse:
    """Update project name and/or description.

    Requires owner role.
    """
    return await service.update_project(
        user_id=UUID(user_id),
        project_id=project_id,
        payload=payload,
    )


# ── Archive ─────────────────────────────────────────────────────────────────


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:manage")),
    ],
)
@audit_action("project.delete", "project", "Project deleted")
async def archive_project(
    project_id: UUID = Path(...),
    request: Request = None,
    service: ProjectService = Depends(_get_project_service),
    user_id: str = Depends(require_user_id),
) -> Response:
    """Archive a project (soft-delete, preserves all data).

    Requires owner role.
    """
    # note: return an explicit empty Response — returning None here makes
    # FastAPI serialize a JSON `null` body on the 204, and uvicorn rejects
    # a non-empty body on 204 (RuntimeError during send). That transport
    # error propagates into get_db teardown and rolls back the archive
    # AFTER 204 headers are flushed — a silent client/server divergence.
    # pin/unpin below already follow this pattern.
    await service.archive_project(
        user_id=UUID(user_id),
        project_id=project_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{project_id}/unarchive",
    response_model=ProjectResponse,
    dependencies=[
        Depends(require_permission("project:manage")),
    ],
)
@audit_action("project.unarchive", "project", "Project unarchived")
async def unarchive_project(
    project_id: UUID = Path(...),
    request: Request = None,
    service: ProjectService = Depends(_get_project_service),
    user_id: str = Depends(require_user_id),
) -> ProjectResponse:
    """Restore an archived project.

    Requires ``project:manage``.  Unlike archive, no project-membership
    check — ``require_project_membership`` 404s archived projects via
    ``get_by_id``, which would make archived projects unrestorable.
    """
    return await service.unarchive_project(
        user_id=UUID(user_id),
        project_id=project_id,
    )


# ── Pins ────────────────────────────────────────────────────────────────────


@router.post(
    "/{project_id}/pin",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:read")),
    ],
)
@audit_action("project.pin", "project", "Project pinned")
async def pin_project(
    request: Request,
    project_id: UUID = Path(...),
    service: ProjectService = Depends(_get_project_service),
) -> Response:
    """Pin a project for the dashboard user (idempotent, max 3 pins)."""
    if getattr(request.state, "auth_type", None) == "api_key":
        raise AuthenticationError("Pins require dashboard auth")
    raw_user_id: str | None = getattr(request.state, "user_id", None)
    if raw_user_id is None:
        raise AuthenticationError("Pins require dashboard auth")
    await service.pin_project(

        user_id=UUID(raw_user_id),
        project_id=project_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{project_id}/pin",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:read")),
    ],
)
@audit_action("project.unpin", "project", "Project unpinned")
async def unpin_project(
    request: Request,
    project_id: UUID = Path(...),
    service: ProjectService = Depends(_get_project_service),
) -> Response:
    """Remove the dashboard user's pin on a project (no-op if absent)."""
    if getattr(request.state, "auth_type", None) == "api_key":
        raise AuthenticationError("Pins require dashboard auth")
    raw_user_id: str | None = getattr(request.state, "user_id", None)
    if raw_user_id is None:
        raise AuthenticationError("Pins require dashboard auth")
    await service.unpin_project(

        user_id=UUID(raw_user_id),
        project_id=project_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Members ─────────────────────────────────────────────────────────────────


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:manage")),
    ],
)
@audit_action("project.member.add", "project_member", "Member added")
async def add_member(
    payload: AddMemberRequest,
    project_id: UUID = Path(...),
    service: ProjectMembershipService = Depends(_get_membership_service),
    user_id: str = Depends(require_user_id),
) -> ProjectMemberResponse:
    """Invite a user to a project by email (always ``pending``).

    # ⚠️ BREAKING: the request body is now ``{email, role}`` — ``user_id``
    was removed. PATCH/DELETE member endpoints remain ``user_id``-keyed.

    # ⚠️ BREAKING: invites no longer activate immediately — every path
    creates a ``pending`` membership (201) the invitee must approve via
    ``POST .../members/approve``. Unknown emails follow the platform
    registration mode: ``open`` creates a pending invite, ``invite_only``
    rejects with 403, ``approval`` rejects with 422.

    Requires owner role.
    """
    return await service.add_by_email(
        project_id=project_id,
        email=payload.email,
        role=payload.role,
        invited_by=UUID(user_id),
    )


@router.get(
    "/{project_id}/members",
    response_model=list[ProjectMemberResponse],
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:read")),
    ],
)
async def list_members(
    request: Request,
    project_id: UUID = Path(...),
    sort_by: ProjectMemberSortBy | None = Query(
        default=None,
        description="Sort key (default created_at).",
    ),
    sort_dir: SortDir = Query(
        default="asc",
        description="Sort direction (default asc).",
    ),
    service: ProjectService = Depends(_get_project_service),
    user_id: str = Depends(require_user_id),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectMemberResponse]:
    """List all members of a project (``pending`` invites included).

    Requires membership. Managers (``project:manage``) get populated
    ``user`` profiles; other readers get IDs-only (``user`` null except
    self per the users RLS policy).
    """
    try:
        await _check_permission(request, user_id, db, "project:manage")
        can_see_profiles = True
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        can_see_profiles = False
    return await service.list_members(
        project_id=project_id,
        sort=SortSpec(sort_by=sort_by, sort_dir=sort_dir),
        can_see_profiles=can_see_profiles,
    )


async def _resolve_is_admin(
    request: Request, user_id: str, db: AsyncSession
) -> bool:
    """Return whether the caller is a DB-verified platform admin.

    Only 403 (non-admin) is absorbed; anything else fails closed.
    """
    try:
        await _ensure_admin(request, user_id, db)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_403_FORBIDDEN:
            raise
        return False
    return True


@router.post(
    "/{project_id}/members/approve",
    response_model=ProjectMemberResponse,
)
@audit_action("project.member.approve", "project_member", "Member invite approved")
async def approve_member(
    request: Request,
    project_id: UUID = Path(...),
    payload: MemberApprovalRequest | None = None,
    service: ProjectMembershipService = Depends(_get_membership_service),
    user_id: str = Depends(require_user_id),
    db: AsyncSession = Depends(get_db),
) -> ProjectMemberResponse:
    """Approve a pending invite, activating the membership.

    Only the invitee (``user_id`` omitted or equal to the caller) or a
    platform admin may approve. No project-membership gate here — a
    pending invitee is not an active member yet.

    Already-active memberships reject with 422 ``already_member``.
    """
    target = payload.user_id if payload and payload.user_id is not None else UUID(user_id)
    is_admin = await _resolve_is_admin(request, user_id, db)
    return await service.approve(
        project_id=project_id,
        user_id=target,
        actor=UUID(user_id),
        is_admin=is_admin,
    )


@router.post(
    "/{project_id}/members/reject",
    status_code=status.HTTP_204_NO_CONTENT,
)
@audit_action("project.member.reject", "project_member", "Member invite rejected")
async def reject_member(
    request: Request,
    project_id: UUID = Path(...),
    payload: MemberApprovalRequest | None = None,
    service: ProjectMembershipService = Depends(_get_membership_service),
    user_id: str = Depends(require_user_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Reject a pending invite, deleting the membership row.

    Only the invitee (``user_id`` omitted or equal to the caller) or a
    platform admin may reject. Active memberships reject with 422 —
    remove them via ``DELETE`` instead.
    """
    target = payload.user_id if payload and payload.user_id is not None else UUID(user_id)
    is_admin = await _resolve_is_admin(request, user_id, db)
    await service.reject(
        project_id=project_id,
        user_id=target,
        actor=UUID(user_id),
        is_admin=is_admin,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:manage")),
    ],
)
@audit_action("project.member.remove", "project_member", "Member removed")
async def remove_member(
    project_id: UUID = Path(...),
    user_id: UUID = Path(...),
    service: ProjectService = Depends(_get_project_service),
) -> Response:
    """Remove a user from a project, or revoke their pending invite.

    Requires owner role.  Cannot remove the last active owner.
    Pending invites revoke unconditionally.
    """
    # note: explicit empty Response — implicit None makes FastAPI serialize
    # a JSON `null` body on the 204, which uvicorn rejects and rolls back
    # the removal in get_db teardown after 204 headers flush.
    await service.remove_member(
        project_id=project_id,
        user_id=user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{project_id}/members/{user_id}",
    response_model=ProjectMemberResponse,
    dependencies=[
        Depends(require_project_membership),
        Depends(require_permission("project:manage")),
    ],
)
@audit_action("project.member.update", "project_member", "Member role updated")
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
