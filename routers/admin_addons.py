"""Platform super-admin addon management — HTTP adapter layer only.

Every endpoint requires ``require_superadmin`` (JWT-only, platform org
DB-verified) plus the RLS-bypass session ``get_db_superadmin`` — addon
state is platform-global.

Endpoints:
    GET    /admin/system/addons                 — list installed addons
    POST   /admin/system/addons                 — install (bundle + name + version)
    POST   /admin/system/addons/{id}/enable     — enable (unhide routes)
    POST   /admin/system/addons/{id}/disable    — disable (403 + hide OpenAPI)
    POST   /admin/system/addons/{id}/upgrade    — upgrade from new bundle
    DELETE /admin/system/addons/{id}            — uninstall (?purge_data + confirm)
    GET    /admin/system/addons/{id}/health     — probe results
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 (runtime import — FastAPI resolves annotation names)
)

from core.audit import audit_action
from dependencies.auth import require_superadmin
from dependencies.db import get_db_superadmin
from repositories.addon_repository import AddonRepository
from schemas.addons import AddonHealthResponse, AddonListResponse, AddonResponse
from services.addon_bundle_service import AddonBundleService
from services.addon_service import AddonService

router = APIRouter(
    prefix="/admin/system/addons",
    tags=["Admin - System - Addons"],
)


def _get_services(
    request: Request,
    db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> tuple[AddonService, AddonBundleService]:
    """Build both addon services sharing one repository.

    Args:
        request: Incoming HTTP request (app-state access).
        db: The superadmin RLS-bypass session.

    Returns:
        ``(lifecycle service, bundle service)``.
    """
    repo = AddonRepository(db)
    return AddonService(repo, request.app), AddonBundleService(repo, request.app)


@router.get("", response_model=AddonListResponse, summary="List installed addons")
async def list_addons(
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    services: tuple[AddonService, AddonBundleService] = Depends(_get_services),  # noqa: B008
) -> AddonListResponse:
    """List every installed addon with live state.

    Args:
        services: Injected addon services.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        All installed addons.
    """
    lifecycle, _ = services
    data = await lifecycle.list_addons()
    return AddonListResponse(data=data, total=len(data))


@router.post(
    "", response_model=AddonResponse, status_code=201, summary="Install an addon"
)
@audit_action("addon.installed", "addon", "Addon installed")
async def install_addon(
    bundle: UploadFile = File(description="Addon .zip / .tar.gz bundle."),
    name: str = Form(description="Expected addon slug (must match addon.toml)."),
    version: str = Form(description="Expected version (must match addon.toml)."),
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    services: tuple[AddonService, AddonBundleService] = Depends(_get_services),  # noqa: B008
) -> AddonResponse:
    """Install an addon from an uploaded bundle (signed, hot-loaded).

    Args:
        bundle: The addon bundle file.
        name: Expected addon slug (fail-closed on mismatch).
        version: Expected version (fail-closed on mismatch).
        services: Injected addon services.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The installed addon.
    """
    lifecycle, bundles = services
    row = await bundles.install(bundle, name, version)
    return await lifecycle.get_addon(row.id)


@router.post(
    "/{addon_id}/enable", response_model=AddonResponse, summary="Enable an addon"
)
@audit_action("addon.enabled", "addon", "Addon enabled")
async def enable_addon(
    addon_id: str,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    services: tuple[AddonService, AddonBundleService] = Depends(_get_services),  # noqa: B008
) -> AddonResponse:
    """Enable an addon (routes serve, tasks dispatch).

    Args:
        addon_id: The addon slug.
        services: Injected addon services.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The updated addon.
    """
    lifecycle, _ = services
    return await lifecycle.enable(addon_id)


@router.post(
    "/{addon_id}/disable", response_model=AddonResponse, summary="Disable an addon"
)
@audit_action("addon.disabled", "addon", "Addon disabled")
async def disable_addon(
    addon_id: str,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    services: tuple[AddonService, AddonBundleService] = Depends(_get_services),  # noqa: B008
) -> AddonResponse:
    """Disable an addon (routes 403 + hidden, tasks drain, no removal).

    Args:
        addon_id: The addon slug.
        services: Injected addon services.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The updated addon.
    """
    lifecycle, _ = services
    return await lifecycle.disable(addon_id)


@router.post(
    "/{addon_id}/upgrade", response_model=AddonResponse, summary="Upgrade an addon"
)
@audit_action("addon.upgraded", "addon", "Addon upgraded")
async def upgrade_addon(
    addon_id: str,
    bundle: UploadFile = File(description="New-version addon bundle."),
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    services: tuple[AddonService, AddonBundleService] = Depends(_get_services),  # noqa: B008
) -> AddonResponse:
    """Upgrade an addon from a new bundle (atomic swap).

    Args:
        addon_id: The addon slug.
        bundle: The new-version bundle.
        services: Injected addon services.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        The upgraded addon.
    """
    lifecycle, bundles = services
    await bundles.upgrade(addon_id, bundle)
    return await lifecycle.get_addon(addon_id)


@router.delete("/{addon_id}", status_code=204, summary="Uninstall an addon")
@audit_action("addon.uninstalled", "addon", "Addon uninstalled")
async def uninstall_addon(
    addon_id: str,
    purge_data: bool = False,
    confirm: str = "",
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    services: tuple[AddonService, AddonBundleService] = Depends(_get_services),  # noqa: B008
) -> None:
    """Uninstall an addon (tables kept unless purged with confirmation).

    Args:
        addon_id: The addon slug.
        purge_data: Also drop manifest-declared tables.
        confirm: Must equal ``addon_id`` when purging.
        services: Injected addon services.
        _org_id: Platform org UUID (superadmin-gated).
    """
    _, bundles = services
    await bundles.uninstall(addon_id, purge_data=purge_data, confirm=confirm)


@router.get(
    "/{addon_id}/health",
    response_model=AddonHealthResponse,
    summary="Probe an addon",
)
async def addon_health(
    addon_id: str,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    services: tuple[AddonService, AddonBundleService] = Depends(_get_services),  # noqa: B008
) -> AddonHealthResponse:
    """Run an addon's registered health probes.

    Args:
        addon_id: The addon slug.
        services: Injected addon services.
        _org_id: Platform org UUID (superadmin-gated).

    Returns:
        Probe results per check name.
    """
    lifecycle, _ = services
    return await lifecycle.health(addon_id)
