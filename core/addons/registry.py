"""Addon registry — atomic dict swap with generation-checked gating."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from core.addons.manifest import AddonManifest
from core.exceptions import AuthorizationError, NotFoundError
from fastapi import FastAPI

_counter = itertools.count(1)


@dataclass(slots=True)
class AddonRecord:
    """One loaded addon revision."""

    manifest: AddonManifest
    module: ModuleType
    version: str
    enabled: bool
    generation: int
    service_overrides: dict[str, Any] = field(default_factory=dict)
    router: Any = None
    """Resolved router (attr, or ``register_router()`` result); ``None`` = no routes."""
    worker_ops: dict[str, Any] = field(default_factory=dict)
    """Resolved worker ops (attr, or ``register_worker()`` result)."""


def get_registry(app: FastAPI) -> dict[str, AddonRecord]:
    """Return the live addon registry (empty dict when uninitialised)."""
    return getattr(app.state, "addons", {})


def publish_registry(app: FastAPI, records: dict[str, AddonRecord]) -> None:
    """Atomically swap the live registry (GIL-atomic ref assignment)."""
    app.state.addons = dict(records)


def next_generation() -> int:
    """Return the next registry generation number."""
    return next(_counter)


def resolve_service_override(
    app: FastAPI, service_name: str, default: Any
) -> Any:
    """Return the first enabled addon's service override, else ``default``."""
    for record in get_registry(app).values():
        if record.enabled and service_name in record.service_overrides:
            return record.service_overrides[service_name]
    return default


def get_enabled(
    app: FastAPI, addon_id: str, generation: int | None = None
) -> AddonRecord:
    """Fetch an enabled addon, enforcing the generation gate.

    Raises:
        NotFoundError: Unknown id or stale generation (410-style 404).
        AuthorizationError: Known but disabled (403).
    """
    record = get_registry(app).get(addon_id)
    if record is None:
        raise NotFoundError(f"Addon {addon_id!r} not found.")
    if not record.enabled:
        raise AuthorizationError(f"Addon {addon_id!r} is disabled.")
    if generation is not None and record.generation != generation:
        raise NotFoundError(
            f"Addon {addon_id!r} generation stale: {generation} != live.",
            {"live_generation": record.generation},
        )
    return record


__all__ = [
    "AddonRecord",
    "get_enabled",
    "get_registry",
    "next_generation",
    "publish_registry",
    "resolve_service_override",
]
