"""Addon runtime — manifest, install, load, registry, sessions, mount."""

from core.addons.registry import AddonRecord, get_registry, resolve_service_override
from core.addons.sessions import (
    AddonSession,
    addon_session_factory,
    resolve_addon_factory,
)

__all__ = [
    "AddonRecord",
    "AddonSession",
    "addon_session_factory",
    "get_registry",
    "resolve_addon_factory",
    "resolve_service_override",
]
