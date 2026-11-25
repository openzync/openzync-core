"""Personal config endpoints — HTTP adapter layer only.

Canonical (and only) paths (match the frontend ``api-client.ts``)::

    GET   /v1/me/config
    PATCH /v1/me/config
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from core.audit import audit_action
from dependencies.auth import get_dashboard_user
from dependencies.services import get_user_config_service
from schemas.user_config import UserConfigResponse, UserConfigUpdate
from services.user_config_service import UserConfigService

router = APIRouter(
    prefix="/v1/me",
    tags=["Me"],
)


@router.get(
    "/config",
    response_model=UserConfigResponse,
    summary="Get own effective config",
    description=(
        "Returns the caller's stored override plus the merged "
        "``user ⊕ system`` effective config.  Requires JWT."
    ),
)
async def get_my_config(
    service: UserConfigService = Depends(get_user_config_service),  # noqa: B008
    user_id: str = Depends(get_dashboard_user),
) -> UserConfigResponse:
    """Get the caller's own config override and effective config.

    Args:
        service: Injected user-config service.
        user_id: Authenticated user UUID from JWT claims.

    Returns:
        The stored override and the merged effective config.
    """
    override = await service.get_override(UUID(user_id))
    effective = await service.get_effective_config(UUID(user_id))
    return UserConfigResponse(
        override=override,
        effective=effective,
        system_managed_fields=service.get_system_managed_fields(),
    )


@router.patch(
    "/config",
    response_model=UserConfigResponse,
    summary="Update own config override",
    description=(
        "Deep-merges the given keys onto the caller's own config override "
        "(``null`` removes a key).  Requires JWT."
    ),
)
@audit_action("auth.config.update", "user", "Own config override updated")
async def update_my_config(
    payload: UserConfigUpdate,
    service: UserConfigService = Depends(get_user_config_service),  # noqa: B008
    user_id: str = Depends(get_dashboard_user),
) -> UserConfigResponse:
    """Update the caller's own config override.

    Args:
        payload: Partial keys to merge (``None`` removes a key).
        service: Injected user-config service.
        user_id: Authenticated user UUID from JWT claims.

    Returns:
        The updated override and the merged effective config.
    """
    override = await service.update_override(UUID(user_id), payload.config)
    effective = await service.get_effective_config(UUID(user_id))
    return UserConfigResponse(
        override=override,
        effective=effective,
        system_managed_fields=service.get_system_managed_fields(),
    )
