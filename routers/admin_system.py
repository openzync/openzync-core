"""Platform super-admin endpoints — HTTP adapter layer only.

Every endpoint requires ``require_superadmin`` (JWT dashboard session,
DB-verified ``superadmin`` role) and — for member-role changes — the
RLS-bypass session ``get_db_superadmin``.

⚠️ BREAKING: multi-org approval endpoints are removed (single platform
org): ``GET /admin/system/orgs``, ``POST .../approve``,
``POST .../reject`` are gone.
⚠️ BREAKING: cross-org ``GET/PATCH /admin/system/orgs/{user_id}/config``
are removed — per-user config is self-serve via
``GET/PATCH /admin/org/config`` (singleton org made the cross-org
variants meaningless).

Endpoints:
    GET    /admin/system/config                        — platform system config
    PATCH  /admin/system/config                        — update platform system config
    GET    /admin/system/settings                      — list settings (masked)
    POST   /admin/system/settings/{key}/reveal         — reveal one setting (audited)
    GET    /admin/system/registration                  — instance registration mode
    PATCH  /admin/system/registration                  — update registration mode
    PATCH  /admin/system/users/{user_id}/role          — promote/demote
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
from dependencies.db import get_db, get_db_superadmin
from repositories.instance_settings_repository import InstanceSettingsRepository
from repositories.user_repository import UserRepository
from schemas.admin_system import (
    MemberRoleResponse,
    SystemSettingRevealResponse,
    SystemSettingsResponse,
    UpdateMemberRoleRequest,
)
from schemas.instance_settings import (
    DefaultBackends,
    InstanceSettingsResponse,
    UpdateRegistrationModeRequest,
)
from schemas.system_config import SystemConfigResponse, SystemConfigUpdate
from services.user_service import UserService

router = APIRouter(
    prefix="/admin/system",
    tags=["Admin - System"],
)


# ── Dependency factories ─────────────────────────────────────────────────────


def _get_redis(request: Request) -> object:
    """Return the Redis client from app state (``None`` when unavailable)."""
    return getattr(request.app.state, "redis", None)


def _get_user_service(
    request: Request,
    db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> UserService:
    """Build a superadmin UserService on the RLS-bypass session.

    Role changes run on the bypass session because the target row belongs
    to the member, not the acting superadmin (the tenant policy would
    otherwise match zero rows).
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
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> SystemConfigResponse:
    """Return the current platform system config.

    Args:
        request: Incoming HTTP request (app-state Redis access).
        _user_id: Superadmin user UUID (superadmin-gated).
        db: Async DB session (platform root-row read).

    Returns:
        A :class:`SystemConfigResponse` with the whitelisted fields.
    """
    return await get_system_config(_get_redis(request), db)


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
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> SystemConfigResponse:
    """Partially update the platform system config.

    Args:
        body: Whitelisted fields to update (``None`` removes a key).
        request: Incoming HTTP request (app-state Redis access).
        _user_id: Superadmin user UUID (superadmin-gated).
        db: RLS-bypass session (the root row belongs to the platform
            root, not the acting superadmin — the tenant policy would
            otherwise match zero rows fail-closed).

    Returns:
        The freshly stored config.
    """
    return await update_system_config(body, _get_redis(request), db)


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
    _user_id: str = Depends(require_superadmin),  # noqa: B008
) -> SystemSettingsResponse:
    """List all known system settings, masked.

    Args:
        request: Incoming HTTP request (unused — kept for signature parity).
        _user_id: Superadmin user UUID (superadmin-gated).

    Returns:
        A :class:`SystemSettingsResponse` with masked values.
    """
    return await list_system_settings()


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
    _user_id: str = Depends(require_superadmin),  # noqa: B008
) -> SystemSettingRevealResponse:
    """Reveal one system setting's raw value.

    Args:
        key: An ``OZ_*`` setting key.
        request: Incoming HTTP request (unused — kept for signature parity).
        _user_id: Superadmin user UUID (superadmin-gated).

    Returns:
        A :class:`SystemSettingRevealResponse` with the raw value.

    Raises:
        HTTPException: 404 if the key is unknown or not set.
    """
    try:
        return await reveal_system_setting(key)
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"System setting '{key}' not found.",
        ) from None


# ═══════════════════════════════════════════════════════════════════════════════
# Instance registration (superadmin-only view of the singleton settings row)
# ═══════════════════════════════════════════════════════════════════════════════


def _to_registration_response(row: object) -> InstanceSettingsResponse:
    """Map an InstanceSettings row to its secrets-free response shape."""
    return InstanceSettingsResponse(
        registration_mode=getattr(row, "registration_mode") or "invite_only",
        initialized=bool(getattr(row, "initialized")),
        default_backends=DefaultBackends.model_validate(
            getattr(row, "default_backends") or {}
        ),
    )


@router.get(
    "/registration",
    response_model=InstanceSettingsResponse,
    summary="Get instance registration mode",
    description=(
        "Returns the platform registration mode, setup-wizard state, and "
        "default backends.  Secrets are never returned.  Requires the "
        "platform super-admin role."
    ),
)
async def get_instance_registration(
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> InstanceSettingsResponse:
    """Return the singleton instance registration settings.

    Args:
        _user_id: Superadmin user UUID (superadmin-gated).
        db: Async DB session (``instance_settings`` has no RLS).

    Returns:
        Secrets-free :class:`InstanceSettingsResponse`.
    """
    row = await InstanceSettingsRepository(db).get()
    return _to_registration_response(row)


@router.patch(
    "/registration",
    response_model=InstanceSettingsResponse,
    summary="Update instance registration mode",
    description=(
        "Updates the platform registration mode.  Invalid modes are "
        "rejected with 422 at the schema boundary.  Requires the platform "
        "super-admin role."
    ),
)
@audit_action(
    "system.registration.update", "system", "Registration mode updated"
)
async def update_instance_registration(
    body: UpdateRegistrationModeRequest,
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> InstanceSettingsResponse:
    """Update the platform registration mode.

    Args:
        body: New registration mode (``open``/``invite_only``/``approval``).
        _user_id: Superadmin user UUID (superadmin-gated).
        db: RLS-bypass session (singleton-row write).

    Returns:
        Secrets-free :class:`InstanceSettingsResponse` with the new mode.
    """
    row = await InstanceSettingsRepository(db).update(
        registration_mode=body.registration_mode
    )
    return _to_registration_response(row)


# ═══════════════════════════════════════════════════════════════════════════════
# Member roles (platform-wide — the singleton org has no per-org membership)
# ═══════════════════════════════════════════════════════════════════════════════


@router.patch(
    "/users/{user_id}/role",
    response_model=MemberRoleResponse,
    summary="Change a member's role",
    description=(
        "Promotes a member to admin or demotes an admin to member. "
        "Requires the super-admin role."
    ),
)
@audit_action("org.member.role_changed", "user", "Member role changed by superadmin")
async def update_member_role(
    user_id: UUID,
    body: UpdateMemberRoleRequest,
    _admin_id: str = Depends(require_superadmin),  # noqa: B008
    service: UserService = Depends(_get_user_service),  # noqa: B008
) -> MemberRoleResponse:
    """Set a member's role.

    Args:
        user_id: The member's user UUID.
        body: The new role (``admin`` or ``member``).
        service: Superadmin UserService (bypass session).
        _admin_id: Superadmin's user ID (superadmin-gated).

    Returns:
        The updated member's id/role.

    Raises:
        HTTPException: 404 if the user does not exist.
    """
    try:
        user = await service.update_member_role(

            user_id=user_id,
            role=body.role,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"User {user_id} not found.",
        ) from None
    return MemberRoleResponse(
        id=user.id,
        user_id=user.id,
        role=user.role,
    )
