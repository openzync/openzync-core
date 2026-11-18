"""Addon loader — versioned hot-load with entry-points fallback."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
from pathlib import Path
from types import ModuleType

from core.addons.manifest import AddonManifest
from core.exceptions import ValidationError

logger = logging.getLogger(__name__)


def module_name(addon_id: str, version: str) -> str:
    """Return the versioned namespace for an addon.

    Args:
        addon_id: Manifest id.
        version: Installed semver; only the major pins the namespace.

    Returns:
        E.g. ``addons.my_addon.v2``.
    """
    return f"addons.{addon_id}.v{version.split('.')[0]}"


def load_addon(
    manifest: AddonManifest, source_path: Path, version: str
) -> ModuleType:
    """Import an addon module under its versioned namespace.

    Primary: ``source_path/entrypoint`` via ``spec_from_file_location``.
    Secondary: ``openzync.addons`` entry-points group.

    Args:
        manifest: Validated manifest.
        source_path: Installed source dir.
        version: Installed version string.

    Returns:
        The imported module.

    Raises:
        ValidationError: If neither source yields a module.
    """
    name = module_name(manifest.id, version)
    entry = source_path / manifest.entrypoint
    if entry.is_file():
        spec = importlib.util.spec_from_file_location(name, entry)
        if spec is None or spec.loader is None:
            raise ValidationError(f"Cannot load addon entrypoint: {entry}.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        logger.info("addon.loaded", extra={"addon": manifest.id, "ns": name})
        return module
    try:
        eps = importlib.metadata.entry_points(group="openzync.addons")
    except TypeError:  # old Python: entry_points() takes no group kwarg
        eps = importlib.metadata.entry_points().get("openzync.addons", ())  # type: ignore[attr-defined]
    for ep in eps:  # type: ignore[union-attr]
        if ep.name == manifest.id:
            logger.info("addon.loaded_entrypoint", extra={"addon": manifest.id})
            return ep.load()
    raise ValidationError(f"Addon {manifest.id!r} has no loadable entrypoint.")


__all__ = ["load_addon", "module_name"]
