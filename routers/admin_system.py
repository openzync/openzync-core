"""Platform super-admin endpoints — HTTP adapter layer only.

Every endpoint requires ``require_superadmin`` (JWT dashboard session,
``org_id == PLATFORM_ORG_ID``, DB-verified ``superadmin`` role) and — for
anything touching org rows — the RLS-bypass session
``get_db_superadmin``.  The bypass is never granted from the org_id/JWT
alone; it is only set after the superadmin role check passes.

Endpoints:
    GET    /admin/system/config                        — platform system config
    PATCH  /admin/system/config                        — update platform system config
    GET    /admin/system/orgs                          — list ALL orgs (incl. pending)
    GET    /admin/system/orgs/{org_id}/members         — list an org's dashboard users
    GET    /admin/system/orgs/{org_id}/config          — read any org's config
    PATCH  /admin/system/orgs/{org_id}/config          — update any org's config
    POST   /admin/system/orgs/{org_id}/approve         — approve a pending org
    POST   /admin/system/orgs/{org_id}/reject          — reject a pending org
    PATCH  /admin/system/orgs/{org_id}/members/{user_id}/role — promote/demote
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 (runtime import — FastAPI resolves annotation names)
)

from core.audit import audit_action
from core.config import get_settings
from core.email import EmailConfig
from core.system_config import get_system_config, update_system_config
from dependencies.auth import require_superadmin
from dependencies.db import get_db_superadmin
from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository
from schemas.admin_system import (
    MemberRoleResponse,
    OrgApprovalResponse,
    SystemMemberListItem,
    SystemOrgListItem,
    SystemOrgListResponse,
    SystemOrgMembersResponse,
    UpdateMemberRoleRequest,
)
from schemas.organization_config import (
    OrgConfigBase,
    OrgConfigResponse,
    UpdateOrgConfigRequest,
)
from schemas.system_config import SystemConfigResponse, SystemConfigUpdate
from services.email_service import EmailService
from services.org_config_service import OrgConfigService
from services.organization_service import OrganizationService

router = APIRouter(
    prefix="/admin/system",
    tags=["Admin - System"],
)


# ── Dependency factories ─────────────────────────────────────────────────────


def _get_openbao_and_redis(request: Request) -> tuple[object, object]:
    """Return ``(openbao_client, redis)`` from app state, fail-fast on missing."""
    bao_client = getattr(request.app.state, "openbao_client", None)
    if bao_client is None:
        raise HTTPException(
            status_code=503,
            detail="OpenBao client not available — secrets backend not initialised",
        )
    redis = getattr(request.app.state, "redis", None)
    return bao_client, redis


def _get_config_service(
    request: Request,
) -> OrgConfigService:
    """Build a request-scoped OrgConfigService for cross-org config access."""
    bao_client, redis = _get_openbao_and_redis(request)
    return OrgConfigService(bao_client=bao_client, redis=redis)


def _get_org_service(
    request: Request,
    db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> OrganizationService:
    """Build a superadmin OrganizationService on the RLS-bypass session.

    Approve/reject operate on pending orgs invisible to tenant sessions —
    the bypass session is mandatory here.
    """
    bao_client, _redis = _get_openbao_and_redis(request)
    email_config = EmailConfig.from_settings(get_settings())
    return OrganizationService(
        repo=OrganizationRepository(db),
        bao_client=bao_client,
        email_service=EmailService(email_config),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Platform system config
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/config",
    response_model=SystemConfigResponse,
    summary="Get platform system config",
    description=(
        "Returns the platform-level policies (org creation, approval "
        "scope) and non-secret system defaults.  Secrets are never "
        "returned.  Requires the platform super-admin role."
    ),
)
async def get_platform_config(
    request: Request,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
) -> SystemConfigResponse:
    """Return the current platform system config.

    Args:
        request: Incoming HTTP request (app-state Redis/OpenBao access).
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        A :class:`SystemConfigResponse` with the whitelisted fields.
    """
    bao_client, redis = _get_openbao_and_redis(request)
    return await get_system_config(redis, bao_client)


@router.patch(
    "/config",
    response_model=SystemConfigResponse,
    summary="Update platform system config",
    description=(
        "Updates whitelisted platform policies and non-secret defaults. "
        "Any key outside the whitelist — including all secrets — is "
        "rejected with 422.  Requires the platform super-admin role."
    ),
)
@audit_action("system.config.update", "system", "System configuration updated")
async def update_platform_config(
    body: SystemConfigUpdate,
    request: Request,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
) -> SystemConfigResponse:
    """Partially update the platform system config.

    Args:
        body: Whitelisted fields to update (``None`` removes a key).
        request: Incoming HTTP request (app-state Redis/OpenBao access).
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The freshly stored config.
    """
    bao_client, redis = _get_openbao_and_redis(request)
    return await update_system_config(body, bao_client, redis)


# ═══════════════════════════════════════════════════════════════════════════════
# Org administration (cross-org, RLS bypass)
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/orgs",
    response_model=SystemOrgListResponse,
    summary="List all organizations",
    description=(
        "Returns every organization including pending and rejected ones — "
        "the tenant-facing surfaces exclude those.  Requires the platform "
        "super-admin role."
    ),
)
async def list_all_orgs(
    page: int = 1,
    limit: int = 50,
    status: str | None = None,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> SystemOrgListResponse:
    """List all orgs for the platform admin.

    Args:
        page: 1-based page number.
        limit: Page size (clamped to 1..200).
        status: Optional lifecycle filter (pending/approved/rejected).
        db: RLS-bypass superadmin session.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        A paginated :class:`SystemOrgListResponse`.
    """
    repo = OrganizationRepository(db)
    orgs, total = await repo.list_all(status=status, page=page, limit=limit)
    return SystemOrgListResponse(
        data=[
            SystemOrgListItem(
                id=org.id,
                name=org.name,
                status=org.status,
                created_at=org.created_at,
            )
            for org in orgs
        ],
        total=total,
        page=max(page, 1),
        limit=min(max(limit, 1), 200),
    )


@router.get(
    "/orgs/{org_id}/members",
    response_model=SystemOrgMembersResponse,
    summary="List an organization's dashboard users",
    description=(
        "Returns the dashboard users of the given organization, paginated. "
        "Soft-deleted users are excluded.  Requires the platform "
        "super-admin role (cross-org access)."
    ),
)
async def list_org_members(
    org_id: UUID,
    page: int = 1,
    limit: int = 50,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> SystemOrgMembersResponse:
    """List a target org's dashboard users for the platform admin.

    Args:
        org_id: The target organization UUID.
        page: 1-based page number.
        limit: Page size (clamped to 1..200).
        _org_id: Platform org UUID (superadmin-gated).
        db: RLS-bypass superadmin session.

    Returns:
        A paginated :class:`SystemOrgMembersResponse`.

    Raises:
        HTTPException: 404 if the organization does not exist.
    """
    org = await OrganizationRepository(db).get_by_id(org_id)
    if org is None:
        raise HTTPException(
            status_code=404,
            detail=f"Organization {org_id} not found.",
        )

    users, total = await UserRepository(db).list_by_org(
        org_id, page=page, limit=limit
    )
    return SystemOrgMembersResponse(
        data=[
            SystemMemberListItem(
                id=user.id,
                email=user.email or user.external_id,
                name=user.name,
                role=user.role if user.role is not None else "member",
                is_active=bool(user.is_active),
            )
            for user in users
        ],
        total=total,
        page=max(page, 1),
        limit=min(max(limit, 1), 200),
    )


@router.get(
    "/orgs/{org_id}/config",
    response_model=OrgConfigResponse,
    summary="Read any organization's config",
    description=(
        "Returns the stored per-org config for the given organization. "
        "Requires the platform super-admin role (cross-org access)."
    ),
)
async def get_org_config(
    org_id: UUID,
    service: OrgConfigService = Depends(_get_config_service),  # noqa: B008
    _org_id: str = Depends(require_superadmin),  # noqa: B008
) -> OrgConfigResponse:
    """Read another org's stored config.

    Args:
        org_id: The target organization UUID.
        service: Injected org-config service.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The stored config wrapped in an :class:`OrgConfigResponse`.
    """
    return await service.get_config_response(org_id)


@router.patch(
    "/orgs/{org_id}/config",
    response_model=OrgConfigBase,
    summary="Update any organization's config",
    description=(
        "Partially updates another org's stored config.  Requires the "
        "platform super-admin role (cross-org access)."
    ),
)
@audit_action(
    "system.org_config.update",
    "organization",
    "Org config updated by superadmin",
)
async def update_org_config(
    org_id: UUID,
    body: UpdateOrgConfigRequest,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: OrgConfigService = Depends(_get_config_service),  # noqa: B008
) -> OrgConfigBase:
    """Partially update another org's config.

    Args:
        org_id: The target organization UUID.
        body: Fields to update (``None`` removes a key).
        service: Injected org-config service.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The freshly stored config.
    """
    return await service.update_config(org_id, body)


@router.post(
    "/orgs/{org_id}/approve",
    response_model=OrgApprovalResponse,
    summary="Approve a pending organization",
    description=(
        "Makes a pending org live: flips status to approved, bootstraps "
        "its OpenBao namespace, and emails the pending admin a magic-link "
        "invite to set their password.  Requires the platform super-admin "
        "role."
    ),
)
@audit_action("org.approved", "organization", "Organization approved")
async def approve_org(
    org_id: UUID,
    request: Request,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: OrganizationService = Depends(_get_org_service),  # noqa: B008
) -> OrgApprovalResponse:
    """Approve a pending org and invite its admin.

    Args:
        org_id: The pending organization UUID.
        request: Incoming HTTP request (superadmin identity).
        service: Superadmin OrganizationService (bypass session).
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The approved org's id/name/status.

    Raises:
        ConflictError: 409 if the org is not pending.
    """
    actor_id = UUID(request.state.user_id)
    org = await service.approve_org(org_id, actor_id)
    return OrgApprovalResponse(
        id=org.id,
        name=org.name,
        status=org.status,
    )


@router.post(
    "/orgs/{org_id}/reject",
    response_model=OrgApprovalResponse,
    summary="Reject a pending organization",
    description=(
        "Flips a pending org to rejected.  No email is sent.  Requires "
        "the platform super-admin role."
    ),
)
@audit_action("org.rejected", "organization", "Organization rejected")
async def reject_org(
    org_id: UUID,
    request: Request,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: OrganizationService = Depends(_get_org_service),  # noqa: B008
) -> OrgApprovalResponse:
    """Reject a pending org.

    Args:
        org_id: The pending organization UUID.
        request: Incoming HTTP request (superadmin identity).
        service: Superadmin OrganizationService (bypass session).
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The rejected org's id/name/status.

    Raises:
        ConflictError: 409 if the org is not pending.
    """
    actor_id = UUID(request.state.user_id)
    org = await service.reject_org(org_id, actor_id)
    return OrgApprovalResponse(
        id=org.id,
        name=org.name,
        status=org.status,
    )


@router.patch(
    "/orgs/{org_id}/members/{user_id}/role",
    response_model=MemberRoleResponse,
    summary="Change a member's role",
    description=(
        "Promotes a member to admin or demotes an admin to member in the "
        "given org.  Requires the platform super-admin role."
    ),
)
@audit_action("org.member.role_changed", "user", "Member role changed by superadmin")
async def update_member_role(
    org_id: UUID,
    user_id: UUID,
    body: UpdateMemberRoleRequest,
    request: Request,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> MemberRoleResponse:
    """Set a member's role in any org.

    Args:
        org_id: The organization UUID.
        user_id: The member's user UUID.
        body: The new role (``admin`` or ``member``).
        request: Incoming HTTP request (app-state Redis for cache invalidation).
        db: RLS-bypass superadmin session.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The updated member's id/org/role.

    Raises:
        NotFoundError: 404 if the user does not exist in the org.
    """
    user = await UserRepository(db).update(
        organization_id=org_id,
        user_id=user_id,
        update_fields={"role": body.role},
    )
    if user is None:
        raise HTTPException(
            status_code=404,
            detail=f"User {user_id} not found in organization {org_id}.",
        )
    # ⚠️ ROLE CACHE: the demoted/promoted user's cached role (60s TTL) is
    # still valid until it expires or is invalidated — subsequent requests
    # may use the stale role for up to a minute.  Invalidate here.
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        from core.rbac import invalidate_role

        await invalidate_role(redis, user_id)
    return MemberRoleResponse(
        id=user.id,
        organization_id=user.organization_id,
        role=user.role,
    )
