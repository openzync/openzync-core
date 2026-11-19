"""Addon hook resolvers — bridge the ``register_*()`` function contract.

Addon modules expose hooks as functions (per the template contract), not as
plain attributes. These resolvers hide that distinction from callers.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any


def resolve_addon_router(module: ModuleType) -> Any | None:
    """Return the addon's router, or ``None`` when the hook is absent.

    Prefers a plain ``router`` attribute, else calls ``register_router()``.
    A missing hook is not an error — routers are optional. A *broken*
    factory propagates loudly so ``enable()`` fails visibly.

    Args:
        module: The loaded addon module.

    Returns:
        The router object, or ``None`` when the addon exposes no router.
    """
    router = getattr(module, "router", None)
    if router is not None:
        return router
    factory = getattr(module, "register_router", None)
    if not callable(factory):
        return None
    return factory()


def resolve_worker_ops(module: ModuleType) -> dict[str, Any]:
    """Return the addon's worker-op mapping (empty when the hook is absent).

    Prefers a plain ``WORKER_OPS`` dict attribute, else calls
    ``register_worker()``.

    Args:
        module: The loaded addon module.

    Returns:
        The op-name-to-handler mapping, or an empty dict.
    """
    ops = getattr(module, "WORKER_OPS", None)
    if isinstance(ops, dict):
        return dict(ops)
    factory = getattr(module, "register_worker", None)
    if not callable(factory):
        return {}
    result = factory()
    return dict(result) if isinstance(result, dict) else {}


__all__ = ["resolve_addon_router", "resolve_worker_ops"]
