"""Addon gating dependency — 403s disabled addons, hides them from OpenAPI.

Mounted per-addon via ``include_router(..., dependencies=[...])`` at
``/v1/ext/{id}`` (see :mod:`core.addons.loader`). Reads the in-process
record first, then the Redis mirror flag (cross-replica visibility):
disabled in either source denies. Fail-closed throughout.

Route objects from a superseded generation 404: ``mount_record`` captures
the generation at mount time, and the gate compares it against the live
record — so an upgrade that leaves a stale route object behind can never
serve traffic through it. In-flight requests hold their own scope.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from core.addons.loader import REDIS_ENABLED_KEY_PREFIX


def require_addon_enabled(addon_id: str, expected_generation: int | None = None):  # noqa: ANN201, ANN202
    """Dependency factory gating one addon's routes on its enabled flag.

    Args:
        addon_id: The addon slug mounted at ``/v1/ext/{addon_id}``.
        expected_generation: Mount generation captured at mount time —
            a live record from another generation 404s (stale route).

    Returns:
        A dependency callable usable in ``include_router(dependencies=...)``.
    """

    async def _gate(request: Request) -> None:
        """Raise 403/404 unless the current generation is loaded and enabled.

        Args:
            request: The incoming HTTP request.

        Raises:
            HTTPException: 403 when the record is missing/disabled, or the
                Redis mirror flag denies; 404 when the route object belongs
                to a superseded generation.
        """
        record = (getattr(request.app.state, "addons", {}) or {}).get(addon_id)
        if record is None or not record.enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "type": "https://errors.openzync.tech/authorization_error",
                    "title": "Addon Disabled",
                    "status": 403,
                    "detail": f"Addon {addon_id!r} is not enabled.",
                },
            )
        if expected_generation is not None and (
            record.generation != expected_generation
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "type": "https://errors.openzync.tech/not_found",
                    "title": "Not Found",
                    "status": 404,
                    "detail": f"Addon {addon_id!r} route is stale.",
                },
            )
        redis = getattr(request.app.state, "redis", None)
        if redis is not None:
            try:
                flag = await redis.get(f"{REDIS_ENABLED_KEY_PREFIX}{addon_id}")
            except Exception:
                flag = None  # Redis outage must not 403 enabled traffic
            if flag == "0":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "type": "https://errors.openzync.tech/authorization_error",
                        "title": "Addon Disabled",
                        "status": 403,
                        "detail": f"Addon {addon_id!r} is disabled cluster-wide.",
                    },
                )

    return _gate
