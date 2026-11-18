"""Mount helpers — ``/v1/ext/{id}`` routers plus ``/ui/`` static panels."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from core.addons.hooks import resolve_addon_router
from core.addons.registry import AddonRecord

logger = logging.getLogger(__name__)


def _addon_gate(addon_id: str, generation: int):  # type: ignore[no-untyped-def]
    from dependencies.addons import require_addon_enabled

    return require_addon_enabled(addon_id, generation)


def mount_addon(app: FastAPI, record: AddonRecord, source_path: Path) -> None:
    """Mount one addon's router and panel static dir on the app.

    Args:
        app: The FastAPI application.
        record: The loaded addon record.
        source_path: Installed source dir (for ``ui/`` static files).
    """
    from dependencies.addons import require_addon_enabled

    addon_id = record.manifest.id
    router = record.router or getattr(record.module, "router", None)
    if router is None:
        try:
            router = resolve_addon_router(record.module)
        except Exception:
            # Loud log, graceful mount: a broken factory must not take down
            # boot or turn enable() into a silent skip. enable() itself
            # already resolved loudly; this fallback is belt-and-braces.
            logger.error("addon.register_router_failed", extra={"addon": addon_id})
            router = None
    if router is not None:
        app.include_router(
            router,
            prefix=f"/v1/ext/{addon_id}",
            dependencies=[Depends(require_addon_enabled(addon_id))],
        )
    ui_dir = source_path / "ui"
    if ui_dir.is_dir():
        app.mount(
            f"/v1/ext/{addon_id}/ui",
            StaticFiles(directory=str(ui_dir), html=True),
            name=f"addon-ui-{addon_id}",
        )
    logger.info("addon.mounted", extra={"addon": addon_id})


def mount_all(
    app: FastAPI, records: dict[str, AddonRecord], paths: dict[str, Path]
) -> None:
    """Mount every enabled addon record (routers + panels)."""
    for addon_id, record in records.items():
        if record.enabled:
            mount_addon(app, record, paths[addon_id])


__all__ = ["mount_addon", "mount_all"]
