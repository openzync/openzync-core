"""Admin addon endpoints — install/list/enable/disable/upgrade/delete/health."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from dependencies.auth import require_superadmin
from dependencies.db import get_db_superadmin
from repositories.addon_repository import AddonRepository
from schemas.addons import (
    AddonHealthResponse,
    AddonInstallRequest,
    AddonListResponse,
    AddonResponse,
)
from services.addon_service import AddonService

router = APIRouter(prefix="/admin/system/addons", tags=["Admin - Addons"])


def _service(
    request: Request, db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> AddonService:
    return AddonService(repo=AddonRepository(db), app=request.app)


@router.post("", response_model=AddonResponse, status_code=201)
async def install_addon(
    body: AddonInstallRequest,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonResponse:
    """Install an addon from ``local_path`` or ``github_url`` (disabled by default)."""
    from core.exceptions import ValidationError

    if bool(body.local_path) == bool(body.github_url):
        raise ValidationError("Provide exactly one of local_path|github_url.")
    source_type = "local" if body.local_path else "github"
    row = await service.install(source_type, body.local_path or body.github_url or "", body.tag)
    return AddonResponse.model_validate(row)


@router.get("", response_model=AddonListResponse)
async def list_addons(
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonListResponse:
    """List all installed addons."""
    rows = await service.list()
    return AddonListResponse(
        data=[AddonResponse.model_validate(r) for r in rows], total=len(rows)
    )


@router.post("/{addon_id}/enable", response_model=AddonResponse)
async def enable_addon(
    addon_id: str,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonResponse:
    """Enable (hot-load + mount) an addon platform-wide."""
    return AddonResponse.model_validate(await service.enable(addon_id))


@router.post("/{addon_id}/disable", response_model=AddonResponse)
async def disable_addon(
    addon_id: str,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonResponse:
    """Disable an addon platform-wide (routes 403, jobs fail closed)."""
    return AddonResponse.model_validate(await service.disable(addon_id))


@router.post("/{addon_id}/upgrade", response_model=AddonResponse)
async def upgrade_addon(
    addon_id: str,
    tag: str | None = None,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonResponse:
    """Manual upgrade — reinstall from source (github tag or latest)."""
    return AddonResponse.model_validate(await service.upgrade(addon_id, tag))


@router.delete("/{addon_id}", status_code=204)
async def uninstall_addon(
    addon_id: str,
    remove_data: bool = Query(default=False),
    confirm: bool = Query(default=False),
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> None:
    """Uninstall; ``?remove_data=true&confirm=true`` downgrades + drops tables."""
    from core.exceptions import ValidationError

    if remove_data and not confirm:
        raise ValidationError("remove_data=true requires confirm=true.")
    await service.uninstall(addon_id, remove_data)
    return None


@router.get("/{addon_id}/health", response_model=AddonHealthResponse)
async def addon_health(
    addon_id: str,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonHealthResponse:
    """Health snapshot for one addon."""
    return AddonHealthResponse(**await service.health(addon_id))  # type: ignore[arg-type]


__all__ = ["router"]
