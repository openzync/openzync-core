"""Audit metadata helpers — attach (action, resource, display_name) to route handlers.

Decorators in this module mark FastAPI endpoint functions with audit metadata
that the AuditMiddleware reads at runtime.  Every non-GET endpoint should be
annotated so the audit log captures meaningful action names instead of generic
``http.{method}`` fallbacks.

Usage::

    from core.audit import audit_action

    @router.post("/items")
    @audit_action("item.create", "item", "Item created")
    async def create_item(): ...
"""

from __future__ import annotations

from collections.abc import Callable


def audit_action(action: str, resource: str, display: str | None = None) -> Callable:
    """Decorator that attaches audit metadata to a route handler.

    Args:
        action: Machine-readable action name (e.g. ``"session.create"``).
        resource: Resource type (e.g. ``"session"``).
        display: Human-readable label (e.g. ``"Session created"``).
            If omitted, auto-generated from *action* by replacing ``_`` and
            ``.`` with spaces and title-casing.

    The metadata is read at runtime by :class:`AuditMiddleware` via
    :func:`get_audit_metadata`.
    """
    if display is None:
        display = action.replace("_", " ").replace(".", " ").title()

    def decorator(func: Callable) -> Callable:
        func._audit_action = action      # type: ignore[attr-defined]
        func._audit_resource = resource  # type: ignore[attr-defined]
        func._audit_display = display    # type: ignore[attr-defined]
        return func

    return decorator


def get_audit_metadata(func: Callable) -> dict[str, str] | None:
    """Read audit metadata from an endpoint function.

    Returns ``{ "action": ..., "resource": ..., "display": ... }``
    or ``None`` if no metadata is attached.
    """
    action = getattr(func, "_audit_action", None)
    resource = getattr(func, "_audit_resource", None)
    display = getattr(func, "_audit_display", None)
    if action is not None and resource is not None:
        return {"action": action, "resource": resource, "display": display or action}
    return None
