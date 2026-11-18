"""Mount helpers — ``/v1/ext/{id}`` routers plus ``/ui/`` static panels."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from core.addons.hooks import resolve_addon_router
from core.addons.registry import AddonRecord
from core.exceptions import NotFoundError

logger = logging.getLogger(__name__)


def _addon_gate(addon_id: str, generation: int):  # type: ignore[no-untyped-def]
    from dependencies.addons import require_addon_enabled

    return require_addon_enabled(addon_id, generation)


def _tracked_mounts(app: FastAPI) -> dict[str, list[object]]:
    """Registry of route objects mounted per ``/v1/ext/{id}`` prefix.

    FastAPI ≥0.163 appends a lazy ``_IncludedRouter`` wrapper (no
    ``.path``) instead of concrete routes, so ``unmount_prefix`` cannot
    find wrappers by path. Track them by identity at mount time.
    """
    tracked = getattr(app.state, "_oz_ext_mounts", None)
    if tracked is None:
        tracked = {}
        app.state._oz_ext_mounts = tracked
    return tracked


def _track_new_routes(app: FastAPI, prefix: str, before: int) -> None:
    """Record routes appended since ``before`` under ``prefix``."""
    tracked = _tracked_mounts(app).setdefault(prefix, [])
    tracked.extend(app.router.routes[before:])


def _matches(route: object, prefix: str) -> bool:
    path = getattr(route, "path", "") or ""
    return path == prefix or path.startswith(prefix + "/")


def unmount_prefix(app: FastAPI, prefix: str) -> None:
    """Remove every route/mount under ``prefix`` (placeholder swap).

    Tracked ``_IncludedRouter`` wrappers are removed by identity (they
    have no ``.path``); anything else falls back to path matching.
    Stripping the wrapper suffices — lazy resolution reads the original
    router live, so the original is left untouched (safe in-flight).
    """
    tracked = _tracked_mounts(app).pop(prefix, [])
    routes = app.router.routes
    for route in tracked:
        if route in routes:
            routes.remove(route)
    for route in [r for r in routes if _matches(r, prefix)]:
        routes.remove(route)


def mount_placeholder(app: FastAPI, addon_id: str) -> None:
    """Mount a gated placeholder for a never-enabled addon (no routes).

    The catch-all carries the same ``require_addon_enabled`` gate, so a
    never-enabled id 403s instead of falling through to Starlette 404.
    ``mount_addon`` strips it wholesale on enable/upgrade.
    """
    from dependencies.addons import require_addon_enabled

    prefix = f"/v1/ext/{addon_id}"
    unmount_prefix(app, prefix)
    router = APIRouter()
    gate = require_addon_enabled(addon_id)

    @router.api_route(
        "",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    @router.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def _ext_placeholder(path: str = "", _a: str = Depends(gate)) -> None:
        raise NotFoundError(f"Addon {addon_id!r} has no routes.")

    before = len(app.router.routes)
    app.include_router(router, prefix=prefix)
    _track_new_routes(app, prefix, before)
    logger.info("addon.placeholder_mounted", extra={"addon": addon_id})


def mount_addon(app: FastAPI, record: AddonRecord, source_path: Path) -> None:
    """Mount one addon's router and panel static dir on the app.

    Args:
        app: The FastAPI application.
        record: The loaded addon record.
        source_path: Installed source dir (for ``ui/`` static files).
    """
    from dependencies.addons import require_addon_enabled

    addon_id = record.manifest.id
    # Strip-then-mount: replaces the install-time placeholder (or a stale
    # revision's routes) wholesale, so enable/upgrade never double-mount.
    unmount_prefix(app, f"/v1/ext/{addon_id}")
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
        prefix = f"/v1/ext/{addon_id}"
        before = len(app.router.routes)
        app.include_router(
            router,
            prefix=prefix,
            dependencies=[Depends(require_addon_enabled(addon_id))],
        )
        _track_new_routes(app, prefix, before)
    ui_dir = source_path / "ui"
    if ui_dir.is_dir():
        before = len(app.router.routes)
        app.mount(
            f"/v1/ext/{addon_id}/ui",
            StaticFiles(directory=str(ui_dir), html=True),
            name=f"addon-ui-{addon_id}",
        )
        _track_new_routes(app, f"/v1/ext/{addon_id}", before)
    logger.info("addon.mounted", extra={"addon": addon_id})


def mount_all(
    app: FastAPI, records: dict[str, AddonRecord], paths: dict[str, Path]
) -> None:
    """Mount every enabled addon record (routers + panels)."""
    for addon_id, record in records.items():
        if record.enabled:
            mount_addon(app, record, paths[addon_id])


__all__ = ["mount_addon", "mount_all", "mount_placeholder", "unmount_prefix"]
