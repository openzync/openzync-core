"""Addon service — install/enable/disable/upgrade/uninstall + version checks."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI

from core.addons.installer import (
    ADDON_ROOT,
    install_source,
    latest_github_tag,
    local_source_version,
)
from core.addons.hooks import resolve_addon_router, resolve_worker_ops
from core.addons.loader import load_addon
from core.addons.manifest import parse_manifest
from core.addons.registry import (
    AddonRecord,
    get_registry,
    next_generation,
    publish_registry,
)
from core.exceptions import ConflictError, NotFoundError, ValidationError
from models.addon_state import AddonState
from repositories.addon_repository import AddonRepository
from schemas.addons import AddonPanelResponse, AddonResponse

logger = logging.getLogger(__name__)


def _panels_for(row: AddonState) -> list[AddonPanelResponse]:
    """Read panel descriptors from the installed ``addon.toml`` on disk.

    The toml is the source of truth (no registry dependence — works for
    disabled addons and before boot-load). Unreadable toml yields ``[]``
    with a loud log; the panels field degrades, never the endpoint.
    """
    try:
        manifest = parse_manifest(ADDON_ROOT / _slug(row, row.id))
    except Exception as exc:
        logger.warning(
            "addon.panels_unreadable", extra={"addon": row.id, "e": str(exc)}
        )
        return []
    return [
        AddonPanelResponse(id=p.id, title=p.title, path=p.path)
        for p in manifest.panels
    ]


def to_addon_response(row: AddonState) -> AddonResponse:
    """Build an ``AddonResponse`` with panels attached.

    ``panels`` is not a model column, so ``model_validate`` alone would
    always leave it empty — validate the row, then attach explicitly.
    """
    response = AddonResponse.model_validate(row)
    response.panels = _panels_for(row)
    return response


def _db_url() -> str:
    from core.config import get_settings

    return str(get_settings().DATABASE_URL)


class AddonService:
    """Orchestrates addon lifecycle against ``addon_states`` + the registry."""

    def __init__(self, repo: AddonRepository, app: FastAPI) -> None:
        self._repo = repo
        self._app = app

    async def list(self) -> list[AddonState]:
        """Return every addon row."""
        return await self._repo.get_all()

    async def install(
        self, source_type: str, source_ref: str, tag: str | None
    ) -> AddonState:
        """Install a new addon (disabled by default).

        Hooks resolve loudly before persist; a gated placeholder mounts
        so never-enabled ids 403 instead of 404; then the bundle chain
        migrates to head. Migration failure keeps the disabled row.

        Raises:
            ConflictError: If the id is already installed.
        """
        if source_type == "local" and tag is not None:
            raise ValidationError("tag is only valid for github sources.")
        installed = await install_source(source_type, source_ref, tag)
        if await self._repo.get_by_id(installed.addon_id) is not None:
            raise ConflictError(f"Addon {installed.addon_id!r} already installed.")
        manifest = parse_manifest(installed.path)
        module = load_addon(manifest, installed.path, installed.version)
        resolve_addon_router(module)
        resolve_worker_ops(module)
        row = AddonState(
            id=installed.addon_id,
            version=installed.version,
            source_type=source_type,
            source_ref=source_ref,
            installed_sha=installed.installed_sha,
            latest_seen=installed.version if source_type == "local" else None,
            enabled=False,
        )
        await self._repo.create(row)
        from core.addons.mount import mount_placeholder

        mount_placeholder(self._app, installed.addon_id)
        try:
            from core.addons.alembic_runner import migrate_up

            await migrate_up(installed.addon_id, installed.path, manifest, _db_url())
        except Exception:
            logger.error(
                "addon.migrations_failed", extra={"addon": installed.addon_id}
            )
            raise
        logger.info("addon.installed", extra={"addon": installed.addon_id})
        return row

    async def enable(self, addon_id: str) -> AddonState:
        """Load the addon module and publish it enabled (atomic swap)."""
        row = await self._require(addon_id)
        manifest = parse_manifest(ADDON_ROOT / _slug(row, addon_id))
        module = load_addon(manifest, ADDON_ROOT / _slug(row, addon_id), row.version)
        overrides = dict(getattr(module, "SERVICE_OVERRIDES", {}))
        router = resolve_addon_router(module)
        if router is not None and getattr(module, "router", None) is None:
            module.router = router
        records = dict(get_registry(self._app))
        records[addon_id] = AddonRecord(
            manifest=manifest,
            module=module,
            version=row.version,
            enabled=True,
            generation=next_generation(),
            service_overrides=overrides,
            router=router,
            worker_ops=resolve_worker_ops(module),
        )
        publish_registry(self._app, records)
        row.enabled = True
        row.generation = records[addon_id].generation
        from core.addons.mount import mount_addon

        mount_addon(self._app, records[addon_id], ADDON_ROOT / _slug(row, addon_id))
        logger.info("addon.enabled", extra={"addon": addon_id})
        return row

    async def disable(self, addon_id: str) -> AddonState:
        """Mark disabled and republish the registry (routes gate on flag)."""
        row = await self._require(addon_id)
        row.enabled = False
        records = dict(get_registry(self._app))
        if addon_id in records:
            records[addon_id].enabled = False
            publish_registry(self._app, records)
        logger.info("addon.disabled", extra={"addon": addon_id})
        return row

    async def upgrade(self, addon_id: str, tag: str | None = None) -> AddonState:
        """Re-install from source and reload when enabled.

        Local sources re-copy ``source_ref`` and 422 when the
        ``pyproject.toml`` version is unchanged. Migration failure
        disables the row + record and raises.
        """
        row = await self._require(addon_id)
        if row.source_type == "github":
            installed = await install_source("github", row.source_ref, tag)
            new_version = installed.version
            new_seen: str | None = installed.installed_sha
        elif row.source_type == "local":
            if tag is not None:
                raise ValidationError("tag is only valid for github sources.")
            installed = await install_source("local", row.source_ref, None)
            new_version = installed.version
            if new_version == row.version:
                raise ValidationError(
                    f"Addon {addon_id!r} already at latest (v{row.version})."
                )
            new_seen = new_version
        else:
            raise ValidationError(f"Unknown source_type: {row.source_type!r}.")
        manifest = parse_manifest(installed.path)
        try:
            from core.addons.alembic_runner import migrate_up

            await migrate_up(row.id, installed.path, manifest, _db_url())
        except Exception:
            row.enabled = False
            records = dict(get_registry(self._app))
            if row.id in records:
                records[row.id].enabled = False
                publish_registry(self._app, records)
            logger.error("addon.migrations_failed", extra={"addon": row.id})
            raise
        row.version = new_version
        row.installed_sha = installed.installed_sha
        row.latest_seen = new_seen
        row.update_available = False
        if row.enabled:
            await self._reload(row)
        logger.info("addon.upgraded", extra={"addon": addon_id})
        return row

    async def uninstall(self, addon_id: str, remove_data: bool) -> None:
        """Remove the addon row; ``remove_data`` downgrades + drops tables.

        With ``remove_data`` the bundle chain downgrades to base and exactly
        the allowlisted tables drop. Without it tables stay; only the
        record and row go.
        """
        row = await self._require(addon_id)
        if remove_data:
            from core.addons.alembic_runner import drop_addon_tables, migrate_down

            src = ADDON_ROOT / _slug(row, addon_id)
            manifest = parse_manifest(src)
            try:
                await migrate_down(row.id, src, manifest, _db_url())
                await drop_addon_tables(self._repo.session, row.id, manifest)
            except Exception:
                row.enabled = False
                records = dict(get_registry(self._app))
                if row.id in records:
                    records[row.id].enabled = False
                    publish_registry(self._app, records)
                logger.error("addon.migrations_failed", extra={"addon": row.id})
                raise
        records = dict(get_registry(self._app))
        records.pop(addon_id, None)
        publish_registry(self._app, records)
        await self._repo.delete(row)
        logger.info("addon.uninstalled", extra={"addon": addon_id})

    async def check_updates(self) -> list[AddonState]:
        """Hourly cron: refresh ``latest_seen``/``update_available``."""
        rows = await self._repo.get_all()
        for row in rows:
            if row.source_type == "github":
                try:
                    latest = await latest_github_tag(
                        f"https://github.com/{row.source_ref}.git"
                    )
                except Exception as exc:  # never fail the cron loudly
                    logger.warning(
                        "addon.check_failed", extra={"addon": row.id, "e": str(exc)}
                    )
                    continue
                row.latest_seen = latest
                row.update_available = latest != row.installed_sha
            elif row.source_type == "local":
                try:
                    current = local_source_version(Path(row.source_ref))
                except Exception as exc:
                    logger.warning(
                        "addon.check_failed", extra={"addon": row.id, "e": str(exc)}
                    )
                    continue
                row.latest_seen = current
                row.update_available = current != row.version
        return rows

    async def get_one(self, addon_id: str) -> AddonState:
        """Return one addon row; 404 via ``_require`` when unknown."""
        return await self._require(addon_id)

    async def health(self, addon_id: str) -> dict[str, object]:
        """Return the health snapshot for one addon."""
        row = await self._require(addon_id)
        loaded = addon_id in get_registry(self._app)
        return {
            "id": row.id,
            "enabled": row.enabled,
            "loaded": loaded,
            "generation": row.generation,
            "version": row.version,
        }

    async def _require(self, addon_id: str) -> AddonState:
        row = await self._repo.get_by_id(addon_id)
        if row is None:
            raise NotFoundError(f"Addon {addon_id!r} not found.")
        return row

    async def _reload(self, row: AddonState) -> None:
        manifest = parse_manifest(ADDON_ROOT / _slug(row, row.id))
        module = load_addon(manifest, ADDON_ROOT / _slug(row, row.id), row.version)
        router = resolve_addon_router(module)
        if router is not None and getattr(module, "router", None) is None:
            module.router = router
        records = dict(get_registry(self._app))
        records[row.id] = AddonRecord(
            manifest=manifest,
            module=module,
            version=row.version,
            enabled=True,
            generation=next_generation(),
            service_overrides=dict(getattr(module, "SERVICE_OVERRIDES", {})),
            router=router,
            worker_ops=resolve_worker_ops(module),
        )
        publish_registry(self._app, records)
        row.generation = records[row.id].generation
        # Swap routes wholesale: the new-major module loads beside the old
        # one, so the old revision's routes must be stripped first.
        from core.addons.mount import mount_addon

        mount_addon(self._app, records[row.id], ADDON_ROOT / _slug(row, row.id))


def _slug(row: AddonState, addon_id: str) -> str:
    if row.source_type == "github":
        return row.source_ref.replace("/", "__")
    return addon_id


__all__ = ["AddonService", "to_addon_response"]
