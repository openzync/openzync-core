"""Per-organization configuration resolution — cache-first, DB-authoritative.

Every request path and background worker that needs org-level settings (LLM,
embeddings, graph, behaviour) should resolve them through this module.

Resolution order:
1. Redis cache (key ``org_config:{org_id}``, TTL 5 min) — performance
   optimisation only.  Cache failures are logged at ERROR but the request
   continues to the DB.
2. Database (``organizations.config`` JSONB column) — the authoritative
   source.  DB failures propagate as hard errors.

There is **no env-var fallback** — if a field is not set in the DB config
it is returned as ``None`` and the caller decides what to do.

On config update the cache is invalidated inline; invalidation failures are
logged at ERROR but do not fail the operation (stale cache expires via TTL).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import CacheUnavailableError
from repositories.organization_repository import OrganizationRepository
from schemas.organization_config import (
    OrgConfigBase,
    UpdateOrgConfigRequest,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

ORG_CONFIG_CACHE_TTL: int = 300
"""TTL in seconds for cached org config (default 5 minutes)."""
CACHE_KEY_PREFIX: str = "org_config"
"""Redis key prefix for cached org config."""


# ── Public API ────────────────────────────────────────────────────────────────


async def get_org_config(
    org_id: UUID,
    db: AsyncSession,
    redis: Any | None = None,
    *,
    skip_cache: bool = False,
) -> OrgConfigBase:
    """Fetch the raw stored config for an org: cache → DB.

    There is no env-var fallback — every field is returned as stored in
    the DB.  Callers that need a default value when a field is ``None``
    must implement that logic themselves.

    Args:
        org_id: The organization UUID.
        db: An async SQLAlchemy session.
        redis: An optional async Redis client.  When ``None``, caching is
            skipped.
        skip_cache: If ``True``, bypass cache and always fetch from DB.

    Returns:
        An :class:`OrgConfigBase` with only the fields that are explicitly
        set in the DB.  Unset fields are ``None``.
    """
    cache_key = f"{CACHE_KEY_PREFIX}:{org_id}"

    # 1. Try cache (unless skip_cache is set)
    if not skip_cache and redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached:
                return OrgConfigBase.model_validate_json(cached)
        except Exception:
            logger.error(
                "org_config.cache_read_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )

    # 2. Fetch from DB
    repo = OrganizationRepository(db)
    raw = await repo.get_config(org_id)
    # When no config has been stored yet, raw is {} and **raw would apply
    # Pydantic defaults (e.g. graph_backend → "surrealdb") even though the
    # field was never explicitly set.  We want *every* field to be None
    # when the DB has no record, so escalate all fields explicitly.
    if not raw:
        org_config = OrgConfigBase(**{name: None for name in OrgConfigBase.model_fields})
    else:
        org_config = OrgConfigBase(**raw)

    # 3. Write to cache (best-effort)
    if not skip_cache and redis is not None:
        try:
            await redis.setex(cache_key, ORG_CONFIG_CACHE_TTL, org_config.model_dump_json())
        except Exception:
            logger.error(
                "org_config.cache_write_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )

    return org_config


async def update_org_config(
    org_id: UUID,
    update_data: UpdateOrgConfigRequest | dict[str, Any],
    db: AsyncSession,
    redis: Any | None = None,
) -> OrgConfigBase:
    """Update stored org config, invalidate cache, and return fresh config.

    Performs a deep merge: provided keys replace existing DB values.
    Keys set to ``None`` are removed from the stored config (returning
    ``None`` on next read).

    Args:
        org_id: The organization UUID.
        update_data: Fields to update.  Can be a :class:`UpdateOrgConfigRequest`
            or a plain dict.
        db: An async SQLAlchemy session.
        redis: An optional async Redis client (for cache invalidation).

    Returns:
        The freshly stored config after the update.
    """
    if isinstance(update_data, UpdateOrgConfigRequest):
        update_dict = update_data.model_dump(exclude_unset=True)
    else:
        update_dict = update_data

    repo = OrganizationRepository(db)
    existing = await repo.get_config(org_id)

    # Deep merge: provided keys override, None values remove
    for key, value in update_dict.items():
        if value is None:
            existing.pop(key, None)
        else:
            existing[key] = value

    await repo.update_config(org_id, existing)

    # Invalidate cache
    if redis is not None:
        cache_key = f"{CACHE_KEY_PREFIX}:{org_id}"
        try:
            await redis.delete(cache_key)
        except Exception:
            logger.error(
                "org_config.cache_invalidation_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )

    # Re-read from DB (cache is cold)
    return await get_org_config(org_id, db, redis=redis, skip_cache=True)


def build_cache_key(org_id: UUID) -> str:
    """Build the Redis cache key for an org's config.

    Args:
        org_id: The organization UUID.

    Returns:
        A string like ``"org_config:<uuid>"``.
    """
    return f"{CACHE_KEY_PREFIX}:{org_id}"
