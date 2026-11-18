"""Addon gating dependencies — generation-checked enablement."""

from __future__ import annotations

from fastapi import Depends, Request

from core.exceptions import AuthorizationError, NotFoundError


def require_addon_enabled(addon_id: str, generation: int | None = None):  # type: ignore[no-untyped-def]
    """Dependency factory gating ``/v1/ext/{id}`` routes on enablement.

    Args:
        addon_id: Manifest id of the addon.
        generation: Optional pinned generation; stale pins 404.

    Returns:
        A dependency raising 403 when disabled/missing, 404 on stale gen.
    """

    async def _gate(request: Request) -> str:
        from core.addons.registry import get_registry

        record = get_registry(request.app).get(addon_id)
        if record is None or not record.enabled:
            raise AuthorizationError(f"Addon {addon_id!r} is not enabled.")
        if generation is not None and record.generation != generation:
            raise NotFoundError(f"Addon {addon_id!r} generation stale.")
        return addon_id

    return _gate


__all__ = ["require_addon_enabled"]
