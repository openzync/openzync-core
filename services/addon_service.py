"""Addon lifecycle service — enable/disable/list/health over installed addons.

Bundle intake (upload validation, extraction, signature) lives in
:mod:`services.addon_bundle_service`; this module owns runtime state.
"""

from __future__ import annotations

import logging
from typing import Any

from core.addons import registry as _registry
from core.addons.loader import publish_state
from core.exceptions import ConflictError, NotFoundError
from models.addon_state import AddonState
from repositories.addon_repository import AddonRepository
from schemas.addons import AddonHealthResponse, AddonResponse

logger = logging.getLogger(__name__)


class AddonService:
    """Enable, disable, list, and probe installed addons."""

    def __init__(self, repo: AddonRepository, app: Any) -> None:
        """Store collaborators.

        Args:
            repo: Addon state repository (superadmin session).
            app: The FastAPI application (live addon map + Redis).
        """
        self._repo = repo
        self._app = app

    async def list_addons(self) -> list[AddonResponse]:
        """Return every installed addon merged with live registry state.

        Returns:
            One response per installed row.
        """
        rows = await self._repo.get_all()
        return [self._to_response(row) for row in rows]

    async def get_addon(self, addon_id: str) -> AddonResponse:
        """Return one addon.

        Args:
            addon_id: The addon slug.

        Returns:
            The addon response.

        Raises:
            NotFoundError: If not installed.
        """
        row = await self._repo.get_by_id(addon_id)
        if row is None:
            raise NotFoundError(f"Addon {addon_id!r} is not installed.")
        return self._to_response(row)

    async def enable(self, addon_id: str) -> AddonResponse:
        """Enable an addon: DB flag, atomic record swap, route unhide.

        Args:
            addon_id: The addon slug.

        Returns:
            The updated addon response.

        Raises:
            NotFoundError: If not installed.
            ConflictError: If the addon failed to load (degraded).
        """
        degraded: dict[str, str] = getattr(self._app.state, "degraded", {})
        if f"addon:{addon_id}" in degraded:
            raise ConflictError(
                f"Addon {addon_id!r} failed to load: {degraded[f'addon:{addon_id}']}."
            )
        row = await self._repo.get_by_id(addon_id)
        if row is None:
            raise NotFoundError(f"Addon {addon_id!r} is not installed.")
        if _registry.get_addon(self._app, addon_id) is None:
            raise ConflictError(f"Addon {addon_id!r} has no loaded record.")
        row = await self._repo.update(addon_id, enabled=True, status="enabled")
        _registry.set_enabled(self._app, addon_id, True)
        self._unhide_routes(addon_id, hide=False)
        await publish_state(self._app, addon_id, True)
        logger.info("addon.enabled", extra={"addon_id": addon_id})
        return self._to_response(row)

    async def disable(self, addon_id: str) -> AddonResponse:
        """Disable an addon: running tasks drain, new traffic 403s.

        Routes stay mounted (no removal) but hide from OpenAPI; the
        dependency rejects with 403. In-flight ARQ tasks observe the flag
        at dispatch and drain without side effects.

        Args:
            addon_id: The addon slug.

        Returns:
            The updated addon response.

        Raises:
            NotFoundError: If not installed.
        """
        row = await self._repo.get_by_id(addon_id)
        if row is None:
            raise NotFoundError(f"Addon {addon_id!r} is not installed.")
        row = await self._repo.update(addon_id, enabled=False, status="disabled")
        _registry.set_enabled(self._app, addon_id, False)
        self._unhide_routes(addon_id, hide=True)
        await publish_state(self._app, addon_id, False)
        logger.info("addon.disabled", extra={"addon_id": addon_id})
        return self._to_response(row)

    async def health(self, addon_id: str) -> AddonHealthResponse:
        """Run the addon's registered probes plus DB/state checks.

        Args:
            addon_id: The addon slug.

        Returns:
            Probe results per check name.

        Raises:
            NotFoundError: If not installed.
        """
        row = await self._repo.get_by_id(addon_id)
        if row is None:
            raise NotFoundError(f"Addon {addon_id!r} is not installed.")
        degraded: dict[str, str] = getattr(self._app.state, "degraded", {})
        checks: dict[str, str] = {}
        record = _registry.get_addon(self._app, addon_id)
        if record is None:
            checks["loaded"] = "error: no live record"
        else:
            checks["loaded"] = "ok"
            for name, probe in record.health_checks.items():
                try:
                    await probe()
                    checks[name] = "ok"
                except Exception as exc:
                    checks[name] = f"error: {exc}"
                    logger.warning(
                        "addon.probe_failed",
                        extra={"addon_id": addon_id, "probe": name, "error": str(exc)},
                    )
        return AddonHealthResponse(
            id=row.id,
            enabled=row.enabled,
            status=row.status,
            version=row.version,
            checks=checks,
            degraded=degraded.get(f"addon:{addon_id}"),
        )

    def _unhide_routes(self, addon_id: str, hide: bool) -> None:
        """Toggle OpenAPI visibility of the addon's routes (no removal).

        FastAPI ≥0.133 mounts routers lazily (``_IncludedRouter``), so the
        walk covers both flattened ``APIRoute``s and included routers
        whose prefix matches ``/v1/ext/{id}`` — toggling the original
        routes, which the effective context ANDs into ``include_in_schema``.

        Args:
            addon_id: The addon slug.
            hide: Whether to hide (disable) or show (enable).
        """
        prefix = f"/v1/ext/{addon_id}"
        for route in self._app.routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                context = getattr(route, "include_context", None)
                if context is not None and getattr(context, "prefix", "") == prefix:
                    for child in original.routes:
                        if hasattr(child, "include_in_schema"):
                            child.include_in_schema = not hide
                continue
            path = getattr(route, "path", "")
            if path == prefix or path.startswith(prefix + "/"):
                route.include_in_schema = not hide

    def _to_response(self, row: AddonState) -> AddonResponse:
        """Merge a DB row with live manifest data.

        Args:
            row: The addon state row.

        Returns:
            The API response.
        """
        manifest = row.manifest or {}
        return AddonResponse(
            id=row.id,
            version=row.version,
            enabled=row.enabled,
            status=row.status,
            hooks=list(manifest.get("hooks", [])),
            permissions=list(manifest.get("permissions", [])),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
