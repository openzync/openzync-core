"""Addon runtime — manifest, install, load, registry, guard, mount."""

from core.addons.guard import install_rls_guard
from core.addons.registry import AddonRecord, get_registry, resolve_service_override

__all__ = ["AddonRecord", "get_registry", "install_rls_guard", "resolve_service_override"]
