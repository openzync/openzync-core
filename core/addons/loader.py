"""Addon loader — entry-points, local dir, versioned namespaces, hot reload.

Sources (checked in this order):

1. ``importlib.metadata`` entry-points group ``openzync.addons`` — installed
   distributions advertising ``{id} = "pkg.hooks"``.
2. The local ``addons/`` directory (project root, overridable via
   ``OPENZYNC_ADDONS_DIR``) — each subdirectory with ``addon.toml`` +
   ``hooks.py`` is one addon.

Every addon loads under a **versioned namespace**
``addons.<id_slug>.v<major>`` via ``importlib.spec_from_file_location``.
Two majors coexist (upgrade loads v2 beside v1, then swaps the record);
code objects are never removed from ``sys.modules`` — unload only drops
the registry record.

Every load is atomic: the record is fully built (manifest, signature,
hooks) before :func:`put_record` swaps it into ``app.state.addons``.
Any failure records ``app.state.degraded["addon:{id}"]`` and raises —
the broken addon never serves traffic.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import os
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any

from fastapi import Depends

from core.addons import registry as _registry
from core.addons.manifest import (
    AddonManifest,
    parse_manifest,
    verify_manifest_signature,
)
from core.exceptions import ValidationError

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "openzync.addons"
"""Entry-point group addon distributions register under."""

ADDONS_DIR: Path = Path(
    os.environ.get(
        "OPENZYNC_ADDONS_DIR",
        str(Path(__file__).resolve().parents[2] / "addons"),
    )
)
"""Local addon source directory (env-overridable, no secret)."""

REDIS_ENABLED_KEY_PREFIX = "addon:enabled:"
"""Redis mirror of each addon's enabled flag (cross-replica visibility)."""

REDIS_EVENTS_CHANNEL = "openzync:addons"
"""Pub/sub channel for addon enable/disable events."""


def module_name_for(addon_id: str, version: str) -> str:
    """Build the versioned namespace for an addon.

    Args:
        addon_id: The validated addon slug.
        version: The validated version string.

    Returns:
        E.g. ``"addons.my_addon.v1"`` for version ``"1.2.0"``.
    """
    major = version.split(".")[0]
    slug = addon_id.replace("-", "_")
    return f"addons.{slug}.v{major}"


def _ensure_addon_parent_packages(module_name: str, addon_dir: Path) -> None:
    """Create ``addons`` / ``addons.<slug>`` packages so relative imports work.

    Args:
        module_name: The versioned namespace (e.g. ``"addons.my_addon.v0"``).
        addon_dir: The addon directory (becomes the slug package ``__path__``).
    """
    parent = module_name.rpartition(".")[0]
    # ⚠️ v0/v1 share addons.<slug>.routers.* submodule names — second major
    # reuses the first's cached submodules; isolate per-major if addons diverge.
    for pkg_name in ("addons", parent):
        if pkg_name in sys.modules:
            continue
        path = [str(addon_dir)] if pkg_name == parent else []
        spec = ModuleSpec(pkg_name, loader=None, is_package=True)
        spec.submodule_search_locations = path
        sys.modules[pkg_name] = importlib.util.module_from_spec(spec)


def load_hooks_module(addon_id: str, version: str, hooks_path: Path) -> Any:
    """Load ``hooks.py`` under its versioned namespace.

    Ensures the ``addons`` / ``addons.<slug>`` parent packages exist first
    so relative imports inside ``hooks.py`` (e.g. ``from .routers...``)
    resolve; the version module itself keeps the existing never-delete
    ``sys.modules`` policy.

    Args:
        addon_id: The validated addon slug.
        version: The validated version string.
        hooks_path: Absolute path to the addon's ``hooks.py``.

    Returns:
        The loaded module.

    Raises:
        ValidationError: If the file is missing or fails to load.
    """
    name = module_name_for(addon_id, version)
    if name in sys.modules:
        return sys.modules[name]
    if not hooks_path.is_file():
        raise ValidationError(f"Addon {addon_id!r} has no hooks.py.")
    _ensure_addon_parent_packages(name, hooks_path.parent)
    spec = importlib.util.spec_from_file_location(
        name,
        str(hooks_path),
        submodule_search_locations=[str(hooks_path.parent)],
    )
    if spec is None or spec.loader is None:
        raise ValidationError(f"Cannot create import spec for {hooks_path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # never deleted — unload drops the record only
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise ValidationError(f"Addon {addon_id!r} hooks failed: {exc}") from exc
    return module


def build_record(manifest: AddonManifest, hooks: Any) -> _registry.AddonRecord:
    """Collect every hook into a record (not yet published).

    Args:
        manifest: The validated manifest.
        hooks: The loaded hooks module.

    Returns:
        A complete, enabled :class:`AddonRecord`.
    """
    record = _registry.AddonRecord(
        id=manifest.id,
        version=manifest.version,
        module_name=module_name_for(manifest.id, manifest.version),
        hooks=hooks,
        manifest=manifest,
        enabled=True,
    )
    _registry.register_router(record)
    _registry.wrap_service(record)
    _registry.replace_service(record)
    _registry.register_middleware(record)
    _registry.register_worker_task(record)
    _registry.register_graph_backend(record)
    _registry.register_health_check(record)
    return record


async def _signing_pubkey(app: Any) -> str:
    """Fetch the addon signing public key from OpenBao (hex).

    Args:
        app: The FastAPI application (``app.state.openbao_client``).

    Returns:
        The hex public key, or ``""`` when unavailable (fail-closed
        downstream — unsigned verification always raises).
    """
    bao = getattr(app.state, "openbao_client", None)
    if bao is None:
        return ""
    try:
        secret = await bao.read_secret("addon/signing-pubkey")
    except Exception:
        logger.warning("addon.signing_pubkey_unreadable")
        return ""
    if isinstance(secret, dict):
        return str(secret.get("public_key", "") or secret.get("value", ""))
    return str(secret or "")


async def load_record_from_dir(addon_dir: Path, pubkey: str) -> _registry.AddonRecord:
    """Validate, verify, and build one addon record (no app side effects).

    Shared by the API lifespan (which then mounts + publishes) and the
    ARQ worker startup (which only needs tasks + state snapshots).

    Args:
        addon_dir: Directory containing ``addon.toml`` + ``hooks.py``.
        pubkey: Hex platform ed25519 public key (fail-closed when empty).

    Returns:
        The built record (not published).

    Raises:
        ValidationError: On any manifest, signature, or hook failure.
    """
    raw = (addon_dir / "addon.toml").read_bytes()
    manifest = parse_manifest(raw)
    verify_manifest_signature(raw, manifest.signature, pubkey)
    hooks = load_hooks_module(manifest.id, manifest.version, addon_dir / "hooks.py")
    return build_record(manifest, hooks)


async def load_addon_from_dir(app: Any, addon_dir: Path) -> _registry.AddonRecord:
    """Validate, verify, load, and atomically publish one local addon.

    Args:
        app: The FastAPI application.
        addon_dir: Directory containing ``addon.toml`` + ``hooks.py``.

    Returns:
        The published record.

    Raises:
        ValidationError: On any manifest, signature, or hook failure.
            ``app.state.degraded["addon:{id}"]`` records the reason.
    """
    manifest_id = addon_dir.name
    try:
        raw = (addon_dir / "addon.toml").read_bytes()
        manifest_id = parse_manifest(raw).id
        pubkey = await _signing_pubkey(app)
        record = await load_record_from_dir(addon_dir, pubkey)
        mount_record(app, record)
        _registry.put_record(app, record)
        await publish_state(app, record.id, True)
        return record
    except Exception as exc:
        degraded: dict[str, str] = getattr(app.state, "degraded", {})
        degraded[f"addon:{manifest_id}"] = str(exc)
        app.state.degraded = degraded
        logger.error(
            "addon.load_failed", extra={"addon_id": manifest_id}, exc_info=True
        )
        raise


def mount_record(app: Any, record: _registry.AddonRecord) -> None:
    """Mount one record's router + graph backends into the live app.

    Bumps the record generation (stale generations 404 via
    :func:`dependencies.addons.require_addon_enabled`) and tracks the
    appended route objects so :func:`unmount_record` can remove them on
    upgrade/uninstall. In-flight requests hold their own scope and drain.

    Args:
        app: The FastAPI application.
        record: The built (not yet published) record.

    Raises:
        ValidationError: If the router declares a bypass dependency.
    """
    from dependencies.addons import require_addon_enabled

    _registry.ensure_no_bypass_dependency(record)
    live = _registry.get_addon(app, record.id)
    record.generation = (live.generation + 1) if live is not None else 0
    if record.router is not None:
        before = len(app.router.routes)
        app.include_router(
            record.router,
            prefix=f"/v1/ext/{record.id}",
            dependencies=[Depends(require_addon_enabled(record.id, record.generation))],
        )
        # Track the exact appended objects — FastAPI ≥0.133 may append
        # lazy ``_IncludedRouter`` wrappers instead of flattened routes;
        # identity removal works for either shape.
        record.mounted_routes = list(app.router.routes[before:])
    dispatcher = getattr(app.state, "graph_backend_dispatcher", None)
    if dispatcher is not None:
        register = getattr(dispatcher, "register", None)
        if register is not None:
            for backend_name, cls in record.graph_backends.items():
                register(backend_name, cls)


def unmount_record(app: Any, addon_id: str) -> None:
    """Remove one record's tracked routes from the live app.

    Identity-removes the objects :func:`mount_record` appended, so an
    upgrade never leaves the old generation serving beside the new one.
    Unknown ids and untracked shapes are no-ops (fail-closed: the gate
    still 403s/404s anything left behind).

    Args:
        app: The FastAPI application.
        addon_id: The addon whose routes to remove.
    """
    record = _registry.get_addon(app, addon_id)
    if record is None or not record.mounted_routes:
        return
    tracked = set(map(id, record.mounted_routes))
    app.router.routes[:] = [r for r in app.router.routes if id(r) not in tracked]
    record.mounted_routes = []
    logger.info("addon.routes_removed", extra={"addon_id": addon_id})


async def publish_state(app: Any, addon_id: str, enabled: bool) -> None:
    """Mirror the enabled flag to Redis and publish the state event.

    Args:
        app: The FastAPI application.
        addon_id: The addon id.
        enabled: The new flag value.
    """
    redis = getattr(app.state, "redis", None)
    if redis is None:
        return
    try:
        await redis.set(
            f"{REDIS_ENABLED_KEY_PREFIX}{addon_id}", "1" if enabled else "0"
        )
        await redis.publish(
            REDIS_EVENTS_CHANNEL, f"{addon_id}:{'enabled' if enabled else 'disabled'}"
        )
    except Exception:
        logger.warning("addon.state_publish_failed", extra={"addon_id": addon_id})


async def discover_and_load(app: Any) -> list[str]:
    """Load all addons from entry-points and the local dir (startup path).

    Each addon fails independently into ``app.state.degraded`` — one bad
    bundle never blocks boot or its neighbours.

    Args:
        app: The FastAPI application.

    Returns:
        The ids that loaded successfully.
    """
    loaded: list[str] = []
    for dist_ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            hooks = dist_ep.load()
            raw_toml = getattr(hooks, "manifest_toml", None)
            if raw_toml is None:
                raise ValidationError(
                    f"Entry-point {dist_ep.name!r} exposes no manifest_toml — "
                    "unsigned distributions never load (trust boundary: the "
                    "platform verifies every addon against its OpenBao key)."
                )
            if isinstance(raw_toml, str):
                raw_toml = raw_toml.encode("utf-8")
            manifest = parse_manifest(bytes(raw_toml))
            pubkey = await _signing_pubkey(app)
            verify_manifest_signature(bytes(raw_toml), manifest.signature, pubkey)
            record = build_record(manifest, hooks)
            mount_record(app, record)
            _registry.put_record(app, record)
            await publish_state(app, record.id, record.enabled)
            loaded.append(record.id)
        except Exception as exc:
            degraded: dict[str, str] = getattr(app.state, "degraded", {})
            degraded[f"addon:{dist_ep.name}"] = str(exc)
            app.state.degraded = degraded
            logger.error(
                "addon.entrypoint_failed", extra={"addon": dist_ep.name}, exc_info=True
            )
    if ADDONS_DIR.is_dir():
        for child in sorted(ADDONS_DIR.iterdir()):
            if not child.is_dir() or not (child / "addon.toml").is_file():
                continue
            try:
                record = await load_addon_from_dir(app, child)
                loaded.append(record.id)
            except Exception:
                logger.exception("addon.dir_skipped", extra={"dir": str(child)})
                continue  # already recorded in degraded
    return loaded
