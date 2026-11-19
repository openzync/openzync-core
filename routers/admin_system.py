"""Platform super-admin endpoints — HTTP adapter layer only.

Every endpoint requires ``require_superadmin`` (JWT dashboard session,
``org_id == PLATFORM_ORG_ID``, DB-verified ``superadmin`` role) and — for
anything touching org rows — the RLS-bypass session
``get_db_superadmin``.

⚠️ BREAKING: multi-org approval endpoints are removed (single platform
org): ``GET /admin/system/orgs``, ``POST .../approve``,
``POST .../reject`` are gone.

Endpoints:
    GET    /admin/system/config                        — platform system config
    PATCH  /admin/system/config                        — update platform system config
    GET    /admin/system/settings                      — list settings (masked)
    POST   /admin/system/settings/{key}/reveal         — reveal one setting (audited)
    GET    /admin/system/orgs/{org_id}/members         — list an org's dashboard users
    GET    /admin/system/orgs/{org_id}/config          — read any org's config
    PATCH  /admin/system/orgs/{org_id}/config          — update any org's config
    PATCH  /admin/system/orgs/{org_id}/members/{user_id}/role — promote/demote
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 (runtime import — FastAPI resolves annotation names)
)

from core.audit import audit_action
from core.exceptions import NotFoundError
from core.system_config import get_system_config, update_system_config
from core.system_settings import list_system_settings, reveal_system_setting
from dependencies.auth import require_superadmin
from dependencies.db import get_db_superadmin
from repositories.organization_repository import OrganizationRepository
from repositories.user_repository import UserRepository
from schemas.admin_system import (
    MemberRoleResponse,
    SystemMemberListItem,
    SystemOrgMembersResponse,
    SystemSettingRevealResponse,
    SystemSettingsResponse,
    UpdateMemberRoleRequest,
)
from schemas.organization_config import (
    OrgConfigBase,
    OrgConfigResponse,
    UpdateOrgConfigRequest,
)
from schemas.sorting import OrgMemberSortBy, SortDir, SortSpec
from schemas.system_config import SystemConfigResponse, SystemConfigUpdate
from services.org_config_service import OrgConfigService
from services.organization_service import OrganizationService
from services.user_service import UserService

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
    """Build an OrganizationService on the RLS-bypass session.

    Member reads span org rows invisible to tenant sessions — the bypass
    session is mandatory here.
    """
    return OrganizationService(repo=OrganizationRepository(db))


def _get_user_service(
    request: Request,
    db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> UserService:
    """Build a superadmin UserService on the RLS-bypass session.

    Cross-org member-role changes must bypass tenant RLS — the target
    user may live in any organization.
    """
    redis = getattr(request.app.state, "redis", None)
    return UserService(repo=UserRepository(db), redis=redis)


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
# Platform system settings (read-only, masked)
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/settings",
    response_model=SystemSettingsResponse,
    summary="List platform system settings (masked)",
    description=(
        "Returns every known ``OZ_*`` system setting with a masked value — "
        "secrets render as bullets and database/Redis URLs have userinfo "
        "stripped.  Raw values are exposed only by the audited reveal "
        "endpoint.  Requires the platform super-admin role."
    ),
)
async def list_platform_system_settings(
    request: Request,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
) -> SystemSettingsResponse:
    """List all known system settings, masked.

    Args:
        request: Incoming HTTP request (app-state OpenBao access).
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        A :class:`SystemSettingsResponse` with masked values.
    """
    bao_client, _redis = _get_openbao_and_redis(request)
    return await list_system_settings(bao_client)


# ⚠️ BREAKING: reveal moved from GET /settings/{key} to POST
# /settings/{key}/reveal — GET requests are skipped by the audit
# middleware (middleware/audit.py:231), so a GET reveal could never be
# audited. POST is required to guarantee the audit trail.
@router.post(
    "/settings/{key}/reveal",
    response_model=SystemSettingRevealResponse,
    summary="Reveal a system setting",
    description=(
        "Returns the raw stored value of one ``OZ_*`` system setting.  "
        "Audited.  Requires the platform super-admin role."
    ),
)
@audit_action(
    "system.settings.revealed",
    "system",
    "System setting revealed by superadmin",
)
async def reveal_platform_system_setting(
    key: str,
    request: Request,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
) -> SystemSettingRevealResponse:
    """Reveal one system setting's raw value.

    Args:
        key: An ``OZ_*`` setting key.
        request: Incoming HTTP request (app-state OpenBao access).
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        A :class:`SystemSettingRevealResponse` with the raw value.

    Raises:
        HTTPException: 404 if the key is unknown or not set.
    """
    bao_client, _redis = _get_openbao_and_redis(request)
    try:
        return await reveal_system_setting(key, bao_client)
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"System setting '{key}' not found.",
        ) from None


# ═══════════════════════════════════════════════════════════════════════════════
# Org members (platform org, RLS bypass)
# ═══════════════════════════════════════════════════════════════════════════════


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
    sort_by: OrgMemberSortBy | None = None,
    sort_dir: SortDir = "asc",
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: OrganizationService = Depends(_get_org_service),  # noqa: B008
) -> SystemOrgMembersResponse:
    """List a target org's dashboard users for the platform admin.

    Args:
        org_id: The target organization UUID.
        page: 1-based page number.
        limit: Page size (clamped to 1..200).
        sort_by: Whitelisted key (``created_at``, ``name``, ``email``).
        sort_dir: ``"asc"`` or ``"desc"``.
        _org_id: Platform org UUID (superadmin-gated).
        service: Superadmin OrganizationService (bypass session).

    Returns:
        A paginated :class:`SystemOrgMembersResponse`.

    Raises:
        HTTPException: 404 if the organization does not exist.
    """
    try:
        users, total = await service.list_org_members(
            org_id,
            page=page,
            limit=limit,
            sort=SortSpec(sort_by=sort_by, sort_dir=sort_dir),
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Organization {org_id} not found.",
        ) from None
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
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: UserService = Depends(_get_user_service),  # noqa: B008
) -> MemberRoleResponse:
    """Set a member's role in any org.

    Args:
        org_id: The organization UUID.
        user_id: The member's user UUID.
        body: The new role (``admin`` or ``member``).
        service: Superadmin UserService (bypass session).
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The updated member's id/org/role.

    Raises:
        HTTPException: 404 if the user does not exist in the org.
    """
    try:
        user = await service.update_member_role(
            organization_id=org_id,
            user_id=user_id,
            role=body.role,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"User {user_id} not found in organization {org_id}.",
        ) from None
    return MemberRoleResponse(
        id=user.id,
        organization_id=user.organization_id,
        role=user.role,
    )
