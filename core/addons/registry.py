"""In-process addon registry — the single source of truth for loaded addons.

``app.state.addons`` maps ``addon_id -> AddonRecord``. Every mutation builds
a **new dict and swaps it atomically** (``app.state.addons = new``) so a
concurrent request never observes a half-registered addon during hot
reload. Modules are never removed from ``sys.modules`` — unloading drops
the record (routes 403 via :mod:`dependencies.addons`) while the code
object stays resident, which is what makes reload safe in-process.

Service overrides are opt-in at the call site: core services call
:func:`resolve_service_override` to discover a wrapper/replacement. Addons
never monkey-patch core modules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from fastapi import APIRouter

from core.exceptions import ValidationError

logger = logging.getLogger(__name__)


class AddonProtocol(Protocol):
    """Structural contract every addon ``hooks.py`` must satisfy."""

    def register_router(self) -> APIRouter | None:
        """Return the addon's router, or ``None`` when hook unused."""
        ...

    def wrap_service(self) -> dict[str, Any]:
        """Return ``{(service, method): wrapper}`` decorators."""
        ...

    def replace_service(self) -> dict[str, Any]:
        """Return ``{(service, method): replacement}`` callables."""
        ...

    def register_middleware(self) -> list[Any]:
        """Return ASGI middleware factories (run inside Auth)."""
        ...

    def register_worker(self) -> dict[str, Any]:
        """Return ``{task_name: async (ctx, db, args) callable}``."""
        ...

    def register_graph_backend(self) -> dict[str, Any]:
        """Return ``{backend_name: GraphBackend class}``."""
        ...

    def register_embedder(self) -> dict[str, Any]:
        """Return ``{embedder_name: embedder instance}``."""
        ...

    def register_llm(self) -> dict[str, Any]:
        """Return ``{llm_name: LLMBackend instance}``."""
        ...

    def register_health_check(self) -> dict[str, Any]:
        """Return ``{check_name: async () -> None}`` readiness probes."""
        ...


@dataclass(slots=True)
class AddonRecord:
    """One loaded addon: manifest data plus live registrations."""

    id: str
    version: str
    module_name: str
    hooks: Any  # Addon hooks module (structural AddonProtocol).
    manifest: Any  # AddonManifest.
    enabled: bool = True
    generation: int = 0
    """Mount generation — bumped on every mount so stale route objects 404."""
    mounted_routes: list[Any] = field(default_factory=list)
    """Route objects appended to the app at mount time (for removal)."""
    router: APIRouter | None = None
    service_wraps: dict[tuple[str, str], Any] = field(default_factory=dict)
    service_replacements: dict[tuple[str, str], Any] = field(default_factory=dict)
    middlewares: list[Any] = field(default_factory=list)
    worker_tasks: dict[str, Any] = field(default_factory=dict)
    graph_backends: dict[str, Any] = field(default_factory=dict)
    embedders: dict[str, Any] = field(default_factory=dict)
    llms: dict[str, Any] = field(default_factory=dict)
    health_checks: dict[str, Any] = field(default_factory=dict)


def _store(app: Any) -> dict[str, AddonRecord]:
    """Return the live addon map, creating it on first use."""
    addons = getattr(app.state, "addons", None)
    if addons is None:
        addons = {}
        app.state.addons = addons
    return addons


def _swap(app: Any, mutate: Any) -> dict[str, AddonRecord]:
    """Atomically replace ``app.state.addons`` after applying ``mutate``."""
    current = _store(app)
    updated = dict(current)
    mutate(updated)
    app.state.addons = updated
    return updated


def put_record(app: Any, record: AddonRecord) -> None:
    """Insert or replace one addon record via atomic swap.

    Args:
        app: The FastAPI application.
        record: The fully built record to publish.

    Raises:
        ValidationError: If another addon already overrides the same
            ``(service, method)`` target — first registration wins, the
            conflict is logged and rejected, never silently shadowed.
    """
    for other in _store(app).values():
        if other.id == record.id:
            continue  # same-id upgrade replaces its own overrides
        clash = (set(record.service_wraps) | set(record.service_replacements)) & (
            set(other.service_wraps) | set(other.service_replacements)
        )
        if clash:
            logger.error(
                "addon.override_conflict",
                extra={"addon_id": record.id, "other": other.id, "keys": sorted(clash)},
            )
            raise ValidationError(
                f"Addon {record.id!r} overrides {sorted(clash)} "
                f"already claimed by {other.id!r} — refusing."
            )
    _swap(app, lambda m: m.__setitem__(record.id, record))
    logger.info("addon.registered", extra={"addon_id": record.id})


def drop_record(app: Any, addon_id: str) -> None:
    """Remove one addon record via atomic swap (code stays in sys.modules).

    Args:
        app: The FastAPI application.
        addon_id: The addon to unload.
    """
    _swap(app, lambda m: m.pop(addon_id, None))
    logger.info("addon.unloaded", extra={"addon_id": addon_id})


def set_enabled(app: Any, addon_id: str, enabled: bool) -> None:
    """Flip one addon's enabled flag via atomic swap.

    Args:
        app: The FastAPI application.
        addon_id: The addon to flip.
        enabled: The new flag value.
    """

    def _flip(m: dict[str, AddonRecord]) -> None:
        if addon_id in m:
            m[addon_id] = replace(m[addon_id], enabled=enabled)

    _swap(app, _flip)


def get_addon(app: Any, addon_id: str) -> AddonRecord | None:
    """Return one addon record, or ``None`` when unknown.

    Args:
        app: The FastAPI application.
        addon_id: The addon id.

    Returns:
        The record, or ``None``.
    """
    return _store(app).get(addon_id)


def list_addons(app: Any) -> list[AddonRecord]:
    """Return all loaded addon records.

    Args:
        app: The FastAPI application.

    Returns:
        The records in registration order.
    """
    return list(_store(app).values())


def is_addon_enabled(app: Any, addon_id: str) -> bool:
    """Whether the addon is loaded **and** enabled (fail-closed).

    Args:
        app: The FastAPI application.
        addon_id: The addon id.

    Returns:
        ``True`` only when a record exists and its flag is set.
    """
    record = _store(app).get(addon_id)
    return record is not None and record.enabled


def _optional(hooks: Any, name: str) -> Any:
    """Call an optional hooks method, returning ``None`` when absent."""
    method = getattr(hooks, name, None)
    if method is None:
        return None
    return method()


_BYPASS_DEPENDENCY_NAMES: frozenset[str] = frozenset(
    {"get_db_superadmin", "set_bypass_rls", "rls_bypass"}
)
"""Dependency names that grant the RLS bypass — forbidden in addon routers."""


def ensure_no_bypass_dependency(record: AddonRecord) -> None:
    """Reject addon routers that smuggle the RLS bypass via dependencies.

    Addon routers mount with :func:`require_addon_enabled`, but a router
    could also declare ``get_db_superadmin`` (or another bypass-granting
    dependency) as its own dependency and escalate without touching the
    wrapped ``core.rls`` functions. Any such dependency fails the record.

    Args:
        record: The record being built or mounted.

    Raises:
        ValidationError: If the router or any route declares a
            bypass-granting dependency.
    """
    if record.router is None:
        return
    candidates: list[Any] = list(record.router.dependencies or [])
    for route in record.router.routes or []:
        candidates.extend(getattr(route, "dependencies", None) or [])
        dependant = getattr(route, "dependant", None)
        for sub in getattr(dependant, "dependencies", None) or []:
            candidates.append(getattr(sub, "call", None))
    for candidate in candidates:
        call = getattr(candidate, "dependency", candidate)
        name = getattr(call, "__name__", "") or ""
        module = getattr(call, "__module__", "") or ""
        if name in _BYPASS_DEPENDENCY_NAMES and (
            "dependencies.db" in module
            or "core.rls" in module
            or name == "get_db_superadmin"
        ):
            raise ValidationError(
                f"Addon {record.id!r} declares bypass dependency {name!r} "
                f"({module}) — RLS bypass is core-only, refusing."
            )


def register_router(record: AddonRecord) -> APIRouter | None:
    """Collect the addon's router into its record.

    Args:
        record: The record being built.

    Returns:
        The router, or ``None``.

    Raises:
        ValidationError: If the router declares a bypass dependency.
    """
    router = _optional(record.hooks, "register_router")
    if router is not None and not isinstance(router, APIRouter):
        raise TypeError(f"Addon {record.id}: register_router must return APIRouter.")
    record.router = router
    ensure_no_bypass_dependency(record)
    return router


def wrap_service(record: AddonRecord) -> None:
    """Collect ``{(service, method): wrapper}`` into the record.

    Args:
        record: The record being built.
    """
    wraps = _optional(record.hooks, "wrap_service") or {}
    for key, fn in wraps.items():
        if not (isinstance(key, tuple) and len(key) == 2):
            raise TypeError(f"Addon {record.id}: wrap key must be (service, method).")
        record.service_wraps[key] = fn


def replace_service(record: AddonRecord) -> None:
    """Collect ``{(service, method): replacement}`` into the record.

    Args:
        record: The record being built.
    """
    replacements = _optional(record.hooks, "replace_service") or {}
    for key, fn in replacements.items():
        if not (isinstance(key, tuple) and len(key) == 2):
            raise TypeError(
                f"Addon {record.id}: replace key must be (service, method)."
            )
        record.service_replacements[key] = fn


def resolve_service_override(app: Any, service: str, method: str, original: Any) -> Any:
    """Resolve the effective callable for a core service method.

    Replacement wins over wrapper; wrapper decorates the core original.
    Addons are consulted in registration order; the first match wins.

    Args:
        app: The FastAPI application.
        service: Core service name (e.g. ``"memory"``).
        method: Method name (e.g. ``"recall"``).
        original: The core bound method to wrap or fall back to.

    Returns:
        The replacement, the wrapped original, or ``original`` unchanged.
    """
    for record in _store(app).values():
        if not record.enabled:
            continue
        key = (service, method)
        if key in record.service_replacements:
            return record.service_replacements[key]
        if key in record.service_wraps:
            return record.service_wraps[key](original)
    return original


def register_middleware(record: AddonRecord) -> None:
    """Collect ASGI middleware factories into the record.

    Args:
        record: The record being built.
    """
    middlewares = _optional(record.hooks, "register_middleware") or []
    record.middlewares.extend(middlewares)


def register_worker_task(record: AddonRecord) -> None:
    """Collect ``{task_name: callable}`` ARQ tasks into the record.

    Args:
        record: The record being built.
    """
    tasks = _optional(record.hooks, "register_worker") or {}
    record.worker_tasks.update(tasks)


def register_graph_backend(record: AddonRecord) -> None:
    """Collect graph backends, embedders, and LLMs into the record.

    Args:
        record: The record being built.
    """
    backends = _optional(record.hooks, "register_graph_backend") or {}
    record.graph_backends.update(backends)
    record.embedders.update(_optional(record.hooks, "register_embedder") or {})
    record.llms.update(_optional(record.hooks, "register_llm") or {})


def register_health_check(record: AddonRecord) -> None:
    """Collect ``{check_name: async probe}`` readiness checks.

    Args:
        record: The record being built.
    """
    record.health_checks.update(_optional(record.hooks, "register_health_check") or {})
