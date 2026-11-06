"""Enterprise addon system — in-process, hot-reloadable core extensions.

Addons extend the monolith without forking it: routers mount at
``/v1/ext/{id}``, services can be wrapped or replaced, middleware slots in
inside Auth, ARQ tasks dispatch through one stable function, and graph /
embedder / LLM backends register into the core dispatchers.

Execution is core-only: addon code runs in-process (no subprocesses, no
sidecars) under the same lifespan, middleware order, and RLS regime as
core code — with one extra guard: addon modules can never enable the RLS
bypass (see :mod:`core.addons.guard`).
"""

from core.addons.registry import (
    AddonProtocol,
    AddonRecord,
    get_addon,
    is_addon_enabled,
    list_addons,
    register_graph_backend,
    register_health_check,
    register_middleware,
    register_router,
    register_worker_task,
    replace_service,
    resolve_service_override,
    wrap_service,
)

__all__: list[str] = [
    "AddonProtocol",
    "AddonRecord",
    "get_addon",
    "is_addon_enabled",
    "list_addons",
    "register_graph_backend",
    "register_health_check",
    "register_middleware",
    "register_router",
    "register_worker_task",
    "replace_service",
    "resolve_service_override",
    "wrap_service",
]
