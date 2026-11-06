"""Org-facing addon management — HTTP adapter layer only.

Platform installs stay superadmin-only (``routers/admin_addons.py``); these
endpoints let an org enable/disable installed addons for itself, manage
settings values, and read panel descriptors plus a per-org health view.

Addon data routes mount at ``/v1/ext/{id}`` and are gated by the two-tier
:func:`dependencies.addons.require_addon_enabled` — no passthrough needed.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import audit_action
from dependencies.auth import require_permission
from dependencies.db import get_db
from repositories.addon_org_settings_repository import AddonOrgSettingsRepository
from repositories.addon_repository import AddonRepository
from schemas.addon_org import (
    AddonPanelsResponse,
    AddonSettingsResponse,
    OrgAddonHealthResponse,
    OrgAddonListResponse,
    OrgAddonResponse,
    RevealAddonSecretsRequest,
    RevealAddonSecretsResponse,
    UpdateAddonSettingsRequest,
)
from services.addon_org_service import AddonOrgService

router = APIRouter(prefix="/v1/addons", tags=["Addons"])


def _get_org_service(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> AddonOrgService:
    """Build the per-org addon service for this request.

    Args:
        request: Incoming HTTP request (app-state access).
        db: The org-scoped (RLS) session.

    Returns:
        A request-scoped :class:`AddonOrgService`.
    """
    return AddonOrgService(
        AddonOrgSettingsRepository(db), AddonRepository(db), request.app
    )


@router.get("", response_model=OrgAddonListResponse, summary="List org addons")
async def list_org_addons(
    org_id: str = Depends(require_permission("configuration:read")),  # noqa: B008
    service: AddonOrgService = Depends(_get_org_service),  # noqa: B008
) -> OrgAddonListResponse:
    """List installed addons with this org's enable state and panel metadata.

    Args:
        org_id: The authenticated organization ID.
        service: The injected per-org addon service.

    Returns:
        All installed addons with org flags.
    """
    data = await service.list_for_org(UUID(org_id))
    return OrgAddonListResponse(data=data, total=len(data))


@router.post(
    "/{addon_id}/enable",
    response_model=OrgAddonResponse,
    summary="Enable addon for org",
)
@audit_action("addon.org_enabled", "addon", "Addon enabled for organization")
async def enable_for_org(
    addon_id: str,
    org_id: str = Depends(require_permission("configuration:write")),  # noqa: B008
    service: AddonOrgService = Depends(_get_org_service),  # noqa: B008
) -> OrgAddonResponse:
    """Enable an installed, platform-enabled addon for this org.

    Args:
        addon_id: The addon slug.
        org_id: The authenticated organization ID.
        service: The injected per-org addon service.

    Returns:
        The updated org view.
    """
    return await service.set_org_enabled(addon_id, UUID(org_id), True)


@router.post(
    "/{addon_id}/disable",
    response_model=OrgAddonResponse,
    summary="Disable addon for org",
)
@audit_action("addon.org_disabled", "addon", "Addon disabled for organization")
async def disable_for_org(
    addon_id: str,
    org_id: str = Depends(require_permission("configuration:write")),  # noqa: B008
    service: AddonOrgService = Depends(_get_org_service),  # noqa: B008
) -> OrgAddonResponse:
    """Disable an addon for this org (platform row untouched).

    Args:
        addon_id: The addon slug.
        org_id: The authenticated organization ID.
        service: The injected per-org addon service.

    Returns:
        The updated org view.
    """
    return await service.set_org_enabled(addon_id, UUID(org_id), False)


@router.get(
    "/{addon_id}/settings",
    response_model=AddonSettingsResponse,
    summary="Read addon settings",
)
async def read_settings(
    addon_id: str,
    org_id: str = Depends(require_permission("configuration:read")),  # noqa: B008
    service: AddonOrgService = Depends(_get_org_service),  # noqa: B008
) -> AddonSettingsResponse:
    """Read the settings schema plus masked values (secrets as ``***``).

    Args:
        addon_id: The addon slug.
        org_id: The authenticated organization ID.
        service: The injected per-org addon service.

    Returns:
        Schema and masked values.
    """
    return await service.get_settings(addon_id, UUID(org_id))


@router.put(
    "/{addon_id}/settings",
    response_model=AddonSettingsResponse,
    summary="Write addon settings",
)
@audit_action("addon.org_settings_updated", "addon", "Addon settings updated")
async def write_settings(
    addon_id: str,
    payload: UpdateAddonSettingsRequest,
    org_id: str = Depends(require_permission("configuration:write")),  # noqa: B008
    service: AddonOrgService = Depends(_get_org_service),  # noqa: B008
) -> AddonSettingsResponse:
    """Validate and store settings (secrets go to OpenBao, never Postgres).

    Args:
        addon_id: The addon slug.
        payload: The full-replacement values object.
        org_id: The authenticated organization ID.
        service: The injected per-org addon service.

    Returns:
        The stored schema and masked values.
    """
    return await service.update_settings(addon_id, UUID(org_id), payload.values)


@router.post(
    "/{addon_id}/settings/reveal",
    response_model=RevealAddonSecretsResponse,
    summary="Reveal addon secrets",
)
@audit_action("addon.org_secrets_revealed", "addon", "Addon secrets revealed")
async def reveal_settings(
    addon_id: str,
    payload: RevealAddonSecretsRequest,
    org_id: str = Depends(require_permission("configuration:write")),  # noqa: B008
    service: AddonOrgService = Depends(_get_org_service),  # noqa: B008
) -> RevealAddonSecretsResponse:
    """Reveal plaintext secrets for explicit keys (audited, write-gated).

    Args:
        addon_id: The addon slug.
        payload: The secret names to reveal.
        org_id: The authenticated organization ID.
        service: The injected per-org addon service.

    Returns:
        Plaintext secrets (``None`` for never-set keys).
    """
    secrets = await service.reveal_secrets(addon_id, UUID(org_id), payload.keys)
    return RevealAddonSecretsResponse(id=addon_id, secrets=secrets)


@router.get(
    "/{addon_id}/panels",
    response_model=AddonPanelsResponse,
    summary="Read addon panels",
)
async def read_panels(
    addon_id: str,
    org_id: str = Depends(require_permission("configuration:read")),  # noqa: B008
    service: AddonOrgService = Depends(_get_org_service),  # noqa: B008
) -> AddonPanelsResponse:
    """Read panel descriptors from the manifest snapshot.

    Args:
        addon_id: The addon slug.
        org_id: The authenticated organization ID.
        service: The injected per-org addon service.

    Returns:
        Panels plus enable state.
    """
    return await service.get_panels(addon_id, UUID(org_id))


@router.get(
    "/{addon_id}/health",
    response_model=OrgAddonHealthResponse,
    summary="Org addon health",
)
async def org_addon_health(
    addon_id: str,
    org_id: str = Depends(require_permission("configuration:read")),  # noqa: B008
    service: AddonOrgService = Depends(_get_org_service),  # noqa: B008
) -> OrgAddonHealthResponse:
    """Return the per-org health view (enable state plus load info).

    Args:
        addon_id: The addon slug.
        org_id: The authenticated organization ID.
        service: The injected per-org addon service.

    Returns:
        Enable state plus load/degraded info.
    """
    return await service.health_for_org(addon_id, UUID(org_id))
