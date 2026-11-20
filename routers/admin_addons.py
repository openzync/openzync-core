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
    PanelTicketRequest,
    PanelTicketResponse,
)
from services.addon_service import AddonService, to_addon_response

router = APIRouter(prefix="/admin/system/addons", tags=["Admin - Addons"])


def _service(
    request: Request, db: AsyncSession = Depends(get_db_superadmin),  # noqa: B008
) -> AddonService:
    return AddonService(repo=AddonRepository(db), app=request.app)


@router.post("", response_model=AddonResponse, status_code=201)
async def install_addon(
    body: AddonInstallRequest,
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonResponse:
    """Install an addon from ``local_path`` or ``github_url`` (disabled by default)."""
    from core.exceptions import ValidationError

    if bool(body.local_path) == bool(body.github_url):
        raise ValidationError("Provide exactly one of local_path|github_url.")
    source_type = "local" if body.local_path else "github"
    row = await service.install(source_type, body.local_path or body.github_url or "", body.tag)
    return to_addon_response(row)


@router.get("", response_model=AddonListResponse)
async def list_addons(
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonListResponse:
    """List all installed addons."""
    rows = await service.list()
    return AddonListResponse(
        data=[to_addon_response(r) for r in rows], total=len(rows)
    )


@router.post("/check-updates", response_model=AddonListResponse)
async def check_addon_updates(
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonListResponse:
    """Manually refresh ``latest_seen``/``update_available`` for all addons."""
    rows = await service.check_updates()
    return AddonListResponse(
        data=[to_addon_response(r) for r in rows], total=len(rows)
    )


@router.post(
    "/{addon_id}/panels/ticket", response_model=PanelTicketResponse, status_code=201
)
async def issue_panel_ticket(
    addon_id: str,
    body: PanelTicketRequest,
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> PanelTicketResponse:
    """Issue a 5-min ``?ticket=`` for one addon panel.

    The caller is the authenticated viewer (dashboard superadmin — same
    gate as every other route here); the ticket is bound to the addon id
    + panel path in Redis and presented by the iframe as ``?ticket=``.
    """
    ticket = await service.issue_panel_ticket(addon_id, body.panel_path, _user_id)
    return PanelTicketResponse(ticket=ticket)


# Route-order invariant: ``POST /check-updates`` is declared above and
# matches on method+path, so ``/{addon_id}`` below can never shadow it.
# (A ``{addon_id}`` regex excluding that literal needs a look-ahead,
# which pydantic-core's regex engine rejects — order is the constraint.)
# ``GET .../check-updates`` (undefined) falls into ``GET /{addon_id}``
# and 404s via ``_require`` — correct status either way.


@router.get("/{addon_id}", response_model=AddonResponse)
async def get_addon(
    addon_id: str,
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonResponse:
    """Fetch one addon with panels attached."""
    return to_addon_response(await service.get_one(addon_id))


@router.post("/{addon_id}/enable", response_model=AddonResponse)
async def enable_addon(
    addon_id: str,
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonResponse:
    """Enable (hot-load + mount) an addon platform-wide."""
    return to_addon_response(await service.enable(addon_id))


@router.post("/{addon_id}/disable", response_model=AddonResponse)
async def disable_addon(
    addon_id: str,
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonResponse:
    """Disable an addon platform-wide (routes 403, jobs fail closed)."""
    return to_addon_response(await service.disable(addon_id))


@router.post("/{addon_id}/upgrade", response_model=AddonResponse)
async def upgrade_addon(
    addon_id: str,
    tag: str | None = None,
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonResponse:
    """Manual upgrade — reinstall from source (github tag or latest)."""
    return to_addon_response(await service.upgrade(addon_id, tag))


@router.delete("/{addon_id}", status_code=204)
async def uninstall_addon(
    addon_id: str,
    remove_data: bool = Query(default=False),
    confirm: bool = Query(default=False),
    _user_id: str = Depends(require_superadmin),  # noqa: B008
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
    _user_id: str = Depends(require_superadmin),  # noqa: B008
    service: AddonService = Depends(_service),  # noqa: B008
) -> AddonHealthResponse:
    """Health snapshot for one addon."""
    return AddonHealthResponse(**await service.health(addon_id))  # type: ignore[arg-type]


__all__ = ["router"]
