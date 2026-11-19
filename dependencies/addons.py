"""Addon gating dependencies — generation-checked enablement."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends, Request

from core.addons.request_scope import enter_addon_request, exit_addon_request
from core.exceptions import AuthorizationError, NotFoundError


def require_addon_enabled(addon_id: str, generation: int | None = None):  # type: ignore[no-untyped-def]
    """Dependency factory gating ``/v1/ext/{id}`` routes on enablement.

    A yield-dependency: marks the addon-request scope on entry (so
    :mod:`core.rls` denies bypass for the whole request, including
    ``asyncio`` tasks spawned in-request) and resets it on exit.
    Honored in ``include_router(dependencies=[...])`` — FastAPI resolves
    yield-dependencies there exactly like per-route ones.

    Args:
        addon_id: Manifest id of the addon.
        generation: Optional pinned generation; stale pins 404.

    Returns:
        A dependency raising 403 when disabled/missing, 404 on stale gen.
    """

    async def _gate(request: Request) -> AsyncGenerator[str, None]:
        from core.addons.registry import get_registry

        token = enter_addon_request()
        try:
            record = get_registry(request.app).get(addon_id)
            if record is None or not record.enabled:
                raise AuthorizationError(f"Addon {addon_id!r} is not enabled.")
            if generation is not None and record.generation != generation:
                raise NotFoundError(f"Addon {addon_id!r} generation stale.")
            yield addon_id
        finally:
            exit_addon_request(token)

    return _gate


__all__ = ["require_addon_enabled"]
