"""Platform admin endpoints — HTTP adapter layer only.

Every endpoint requires ``require_admin`` (JWT dashboard session,
DB-verified ``admin`` role) and — for member-role changes — the
RLS-bypass session ``get_db_admin``.

⚠️ BREAKING: role collapse — the ``superadmin`` role is gone.  Only the
platform root (``external_id='root'``) holds ``admin`` with full powers;
former ``superadmin`` rows are now ``admin``, former ``admin`` rows are
now permission-scoped ``member``.  Nobody may promote — not even root;
grant permissions instead.

⚠️ BREAKING: multi-org approval endpoints are removed (single platform
org): ``GET /admin/system/orgs``, ``POST .../approve``,
``POST .../reject`` are gone.
⚠️ BREAKING: cross-org ``GET/PATCH /admin/system/orgs/{user_id}/config``
are removed — per-user config is self-serve via
``GET/PATCH /admin/platform-config`` (singleton org made the cross-org
variants meaningless).

Endpoints:
    GET    /admin/system/config                        — platform system config
    PATCH  /admin/system/config                        — update platform system config
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
from core.exceptions import NotFoundError, ValidationError
from core.system_config import get_system_config, update_system_config
from dependencies.auth import require_admin
from dependencies.db import get_db, get_db_admin
from dependencies.services import get_admin_user_service
from repositories.instance_settings_repository import InstanceSettingsRepository
from schemas.admin_system import (
    MemberRoleResponse,
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


# ═══════════════════════════════════════════════════════════════════════════════
# Platform system config
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/config",
    response_model=SystemConfigResponse,
    summary="Get platform system config",
    description=(
        "Returns the platform system config.  No whitelisted fields "
        "remain — the response is ``{}``.  Requires the platform "
        "admin role."
    ),
)
async def get_platform_config(
    request: Request,
    _user_id: str = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> SystemConfigResponse:
    """Return the current platform system config.

    Args:
        request: Incoming HTTP request (app-state Redis access).
        _user_id: Admin user UUID (admin-gated).
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
        "Updates the platform system config.  No whitelisted fields "
        "remain — every key is rejected with 422.  Requires the platform "
        "admin role."
    ),
)
@audit_action("system.config.update", "system", "System configuration updated")
async def update_platform_config(
    body: SystemConfigUpdate,
    request: Request,
    _user_id: str = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db_admin),  # noqa: B008
) -> SystemConfigResponse:
    """Partially update the platform system config.

    Args:
        body: Whitelisted fields to update (``None`` removes a key).
        request: Incoming HTTP request (app-state Redis access).
        _user_id: Admin user UUID (admin-gated).
        db: RLS-bypass session (the root row belongs to the platform
            root, not the acting admin — the tenant policy would
            otherwise match zero rows fail-closed).

    Returns:
        The freshly stored config.
    """
    return await update_system_config(body, _get_redis(request), db)


# ═══════════════════════════════════════════════════════════════════════════════
# Instance registration (admin-only view of the singleton settings row)
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
        "admin role."
    ),
)
async def get_instance_registration(
    _user_id: str = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> InstanceSettingsResponse:
    """Return the singleton instance registration settings.

    Args:
        _user_id: Admin user UUID (admin-gated).
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
        "admin role."
    ),
)
@audit_action(
    "system.registration.update", "system", "Registration mode updated"
)
async def update_instance_registration(
    body: UpdateRegistrationModeRequest,
    _user_id: str = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db_admin),  # noqa: B008
) -> InstanceSettingsResponse:
    """Update the platform registration mode.

    Args:
        body: New registration mode (``open``/``invite_only``/``approval``).
        _user_id: Admin user UUID (admin-gated).
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
    summary="Demote a member (promotion disabled)",
    description=(
        "Demotes an admin to member.  Role promotion is disabled (422) — "
        "the ``admin`` role is frozen, even for root; grant permissions "
        "via ``PATCH /v1/users/{user_id}`` instead.  Requires the admin "
        "role.  The root user's role is frozen. "
        # ⚠️ BREAKING: admin promotion removed — ``role`` accepts only
        # ``member`` (schema 422 otherwise); the service raises 422 for
        # ``admin``, even from root.
    ),
)
@audit_action("org.member.role_changed", "user", "Member role changed by admin")
async def update_member_role(
    user_id: UUID,
    body: UpdateMemberRoleRequest,
    _admin_id: str = Depends(require_admin),  # noqa: B008
    service: UserService = Depends(get_admin_user_service),  # noqa: B008
) -> MemberRoleResponse:
    """Set a member's role (demotion only — promotion is frozen).

    Args:
        user_id: The member's user UUID.
        body: The new role (only ``member``).
        service: Admin UserService (bypass session).
        _admin_id: Admin's user ID (admin-gated).

    Returns:
        The updated member's id/role.

    Raises:
        HTTPException: 404 if the user does not exist.
        HTTPException: 422 if the target is root, ``admin`` is requested
            (frozen, even for root), or the last admin would be demoted.
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
    except ValidationError:
        raise HTTPException(
            status_code=422,
            detail="Role change not allowed for this user and actor.",
        ) from None
    return MemberRoleResponse(
        id=user.id,
        user_id=user.id,
        role=user.role,
    )
