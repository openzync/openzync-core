"""Addon gating dependency — 403s disabled addons, hides them from OpenAPI.

Mounted per-addon via ``include_router(..., dependencies=[...])`` at
``/v1/ext/{id}`` (see :mod:`core.addons.loader`). Two tiers, both
fail-closed:

1. **Platform tier** — the in-process record plus the Redis mirror flag
   (cross-replica visibility): disabled in either source denies.
2. **Org tier** — when ``request.state.org_id`` is set (authenticated
   traffic), the org's own flag must also be on. No org context (public
   routes) skips this tier — the platform gate already applied.

Route objects from a superseded generation 404: ``mount_record`` captures
the generation at mount time, and the gate compares it against the live
record — so an upgrade that leaves a stale route object behind can never
serve traffic through it. In-flight requests hold their own scope.

Outage semantics: Redis or Postgres failures never 403 enabled traffic —
a missing mirror flag falls through to Postgres, and a Postgres error
allows the request (logged) instead of denying it.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.addons.loader import ORG_ENABLED_KEY_PREFIX, REDIS_ENABLED_KEY_PREFIX
from dependencies.db import get_db
from repositories.addon_org_settings_repository import AddonOrgSettingsRepository

logger = logging.getLogger(__name__)


def require_addon_enabled(addon_id: str, expected_generation: int | None = None):  # noqa: ANN201, ANN202
    """Dependency factory gating one addon's routes on both enable tiers.

    Args:
        addon_id: The addon slug mounted at ``/v1/ext/{addon_id}``.
        expected_generation: Mount generation captured at mount time —
            a live record from another generation 404s (stale route).

    Returns:
        A dependency callable usable in ``include_router(dependencies=...)``.
    """

    async def _gate(
        request: Request,
        db: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> None:
        """Raise 403/404 unless the current generation is enabled twice over.

        Args:
            request: The incoming HTTP request.
            db: The org-scoped session for the per-org flag lookup.

        Raises:
            HTTPException: 403 when the record is missing/disabled at either
                tier, or the Redis mirror flags deny; 404 when the route
                object belongs to a superseded generation.
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
        await _gate_org_tier(request, db, addon_id)

    return _gate


async def _gate_org_tier(request: Request, db: AsyncSession, addon_id: str) -> None:
    """Deny when the request's org disabled the addon (second tier).

    Mirror-first like the platform tier: an explicit ``"0"`` per-org key
    403s immediately, ``"1"`` passes, and a missing key falls through to
    Postgres. Any lookup failure allows (outage never 403s enabled
    traffic). Requests without org context skip the tier entirely.

    Args:
        request: The incoming HTTP request.
        db: The org-scoped session for the flag lookup.
        addon_id: The addon slug.

    Raises:
        HTTPException: 403 when the org's flag is off (distinct detail).
    """
    org_id = getattr(request.state, "org_id", None)
    if not org_id:
        return
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            flag = await redis.get(f"{ORG_ENABLED_KEY_PREFIX}{org_id}:{addon_id}")
        except Exception:
            flag = None  # Redis outage must not 403 enabled traffic
        if flag == "0":
            raise _org_disabled(addon_id)
        if flag == "1":
            return
    try:
        enabled = await AddonOrgSettingsRepository(db).is_enabled(
            addon_id, UUID(org_id)
        )
    except Exception:
        logger.warning("addon.org_gate_fallback_allow", extra={"addon_id": addon_id})
        return  # Postgres outage must not 403 enabled traffic
    if not enabled:
        raise _org_disabled(addon_id)


def _org_disabled(addon_id: str) -> HTTPException:
    """Build the org-tier 403 (distinct detail from the platform tier)."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "type": "https://errors.openzync.tech/authorization_error",
            "title": "Addon Disabled For Organization",
            "status": 403,
            "detail": f"Addon {addon_id!r} is not enabled for this organization.",
        },
    )
