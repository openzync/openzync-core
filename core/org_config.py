"""Per-organization configuration resolution — cache-first, OpenBao-authoritative.

Every request path and background worker that needs org-level settings (LLM,
embeddings, graph, behaviour) should resolve them through this module.

Resolution order:
1. Redis cache (key ``org_config:{org_id}``, TTL 5 min) — performance
   optimisation only.  Cache failures are logged at ERROR but the request
   continues to OpenBao.
2. OpenBao KV (per-org namespace ``org_<uuid>/config/``) — the authoritative
   source.  OpenBao failures propagate as hard errors.

There is **no** env-var fallback — if a field is not set in OpenBao it is
returned as ``None`` and the caller decides what to do.

On config update the cache is invalidated inline; invalidation failures are
logged at ERROR but do not fail the operation (stale cache expires via TTL).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import redis.asyncio

from core.openbao import OpenBaoClient
from core.openbao_exceptions import OpenBaoConnectionError
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
    redis: redis.asyncio.Redis | None = None,
    bao_client: OpenBaoClient | None = None,
    *,
    skip_cache: bool = False,
) -> OrgConfigBase:
    """Fetch the stored config for an org: cache → OpenBao.

    There is no env-var fallback — every field is returned as stored in
    OpenBao.  Callers that need a default value when a field is ``None``
    must implement that logic themselves.

    Args:
        org_id: The organization UUID.
        redis: An optional async Redis client.  When ``None``, caching is
            skipped.
        bao_client: An authenticated :class:`OpenBaoClient`.  **Required**.
        skip_cache: If ``True``, bypass cache and always fetch from OpenBao.

    Returns:
        An :class:`OrgConfigBase` with only the fields that are explicitly
        set in OpenBao.  Unset fields are ``None``.

    Raises:
        OpenBaoConnectionError: If *bao_client* is ``None``.
    """
    if bao_client is None:
        raise OpenBaoConnectionError("OpenBao client required for org config reads")

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

    # 2. Fetch from OpenBao
    raw = await bao_client.read_org_config(org_id)
    # When no config has been stored yet, raw is {} and **raw would apply
    # Pydantic defaults (e.g. graph_backend → "surrealdb") even though the
    # field was never explicitly set.  We want *every* field to be None
    # when OpenBao has no record, so escalate all fields explicitly.
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
    bao_client: OpenBaoClient,
    redis: redis.asyncio.Redis | None = None,
) -> OrgConfigBase:
    """Update stored org config in OpenBao, invalidate cache, return fresh config.

    Performs a deep merge: provided keys replace existing stored values.
    Keys set to ``None`` are removed from the stored config (returning
    ``None`` on next read).

    OpenBao is the sole authoritative store — there is no database
    dual-write.

    Args:
        org_id: The organization UUID.
        update_data: Fields to update.  Can be a :class:`UpdateOrgConfigRequest`
            or a plain dict.
        bao_client: An authenticated :class:`OpenBaoClient`.
        redis: An optional async Redis client (for cache invalidation).

    Returns:
        The freshly stored config after the update.
    """
    if isinstance(update_data, UpdateOrgConfigRequest):
        update_dict = update_data.model_dump(exclude_unset=True)
    else:
        update_dict = update_data

    # 1. Read existing config from OpenBao
    existing = await bao_client.read_org_config(org_id)

    # 2. Deep merge: provided keys override, None values remove
    for key, value in update_dict.items():
        if value is None:
            existing.pop(key, None)
        else:
            existing[key] = value

    # 3. Write to OpenBao (authoritative store)
    await bao_client.write_org_config(org_id, existing)

    # 4. Invalidate Redis cache
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

    # 5. Re-read from OpenBao (cache is cold — forces fresh read)
    return await get_org_config(org_id, redis=redis, bao_client=bao_client, skip_cache=True)


def build_cache_key(org_id: UUID) -> str:
    """Build the Redis cache key for an org's config.

    Args:
        org_id: The organization UUID.

    Returns:
        A string like ``"org_config:<uuid>"``.
    """
    return f"{CACHE_KEY_PREFIX}:{org_id}"
