"""Platform system configuration — cache-first, OpenBao-authoritative.

Resolution order (mirrors ``core.org_config``):
1. Redis cache (key ``system_config``, TTL 5 min) — performance
   optimisation only.  Cache failures are logged at ERROR but the request
   continues to OpenBao.
2. OpenBao KV (``system/`` namespace, ``config/data/system``) — the
   authoritative source.  OpenBao failures propagate as hard errors.

Only the non-secret whitelist in :mod:`schemas.system_config` is ever
read or written — the ``OZ_*`` secret keys sharing the same OpenBao path
are never exposed.

Defaults (backward compatible — existing installs keep working):
``org_creation_policy=allow_all``, ``approval_scope=both`` when OpenBao
has no record.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio  # noqa: TC002 — runtime import mirrors core/org_config.py

from core.openbao import (
    OpenBaoClient,  # noqa: TC001 — runtime import mirrors core/org_config.py
)
from core.openbao_exceptions import OpenBaoConnectionError
from schemas.system_config import (
    SYSTEM_CONFIG_WHITELIST,
    SystemConfigResponse,
    SystemConfigUpdate,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_CONFIG_CACHE_TTL: int = 300
"""TTL in seconds for the cached system config (default 5 minutes)."""

CACHE_KEY: str = "system_config"
"""Redis key for the cached system config."""


# ── Public API ────────────────────────────────────────────────────────────────


async def get_system_config(
    redis: redis.asyncio.Redis | None = None,
    bao_client: OpenBaoClient | None = None,
    *,
    skip_cache: bool = False,
) -> SystemConfigResponse:
    """Fetch the platform system config: cache → OpenBao.

    Only whitelisted (non-secret) keys are returned.  When OpenBao has no
    record — or a whitelisted key is absent — the backward-compatible
    defaults apply: ``org_creation_policy=allow_all``,
    ``approval_scope=both``, other fields ``None``.

    Args:
        redis: An optional async Redis client.  When ``None``, caching is
            skipped.
        bao_client: An authenticated :class:`OpenBaoClient`.  **Required**.
        skip_cache: If ``True``, bypass cache and always fetch from OpenBao.

    Returns:
        A :class:`SystemConfigResponse` with the whitelisted fields.

    Raises:
        OpenBaoConnectionError: If *bao_client* is ``None``.
    """
    if bao_client is None:
        raise OpenBaoConnectionError("OpenBao client required for system config reads")

    # 1. Try cache (unless skip_cache is set)
    if not skip_cache and redis is not None:
        try:
            cached = await redis.get(CACHE_KEY)
            if cached:
                return SystemConfigResponse.model_validate_json(cached)
        except Exception:
            logger.error(
                "system_config.cache_read_failed",
                exc_info=True,
            )

    # 2. Fetch from OpenBao — the raw dict may contain OZ_* secrets; only
    #    the whitelist is read out of it, never the whole payload.
    raw = await bao_client.read_system_config()
    config = SystemConfigResponse(
        **{key: raw[key] for key in SYSTEM_CONFIG_WHITELIST if key in raw}
    )

    # 3. Write to cache (best-effort)
    if not skip_cache and redis is not None:
        try:
            await redis.setex(
                CACHE_KEY,
                SYSTEM_CONFIG_CACHE_TTL,
                config.model_dump_json(),
            )
        except Exception:
            logger.error(
                "system_config.cache_write_failed",
                exc_info=True,
            )

    return config


async def update_system_config(
    update_data: SystemConfigUpdate | dict[str, Any],
    bao_client: OpenBaoClient,
    redis: redis.asyncio.Redis | None = None,
) -> SystemConfigResponse:
    """Update the platform system config in OpenBao, invalidate cache, re-read.

    Performs a deep merge onto the existing system secret.  Keys set to
    ``None`` are removed from the stored config (returning ``None`` on
    next read).  **Any key outside the whitelist is rejected** — a
    defensive second line behind the schema's ``extra="forbid"``, so a
    plain-dict caller can never write a secret key.

    Args:
        update_data: Fields to update.  Can be a
            :class:`SystemConfigUpdate` or a plain dict.
        bao_client: An authenticated :class:`OpenBaoClient`.
        redis: An optional async Redis client (for cache invalidation).

    Returns:
        The freshly stored config after the update.

    Raises:
        ValidationError: If a key outside the whitelist is present.
    """
    from core.exceptions import ValidationError

    if isinstance(update_data, SystemConfigUpdate):
        update_dict = update_data.model_dump(exclude_unset=True)
    else:
        update_dict = update_data

    unknown = set(update_dict) - SYSTEM_CONFIG_WHITELIST
    if unknown:
        raise ValidationError(
            "These keys are not editable system config: "
            f"{', '.join(sorted(unknown))}."
        )

    # 1. Read existing system secret (contains OZ_* keys — merge, never return)
    existing = await bao_client.read_system_config()

    # 2. Deep merge: provided keys override, None values remove
    for key, value in update_dict.items():
        if value is None:
            existing.pop(key, None)
        else:
            existing[key] = value

    # 3. Write to OpenBao (authoritative store)
    await bao_client.write_system_config(existing)

    # 4. Invalidate Redis cache
    if redis is not None:
        try:
            await redis.delete(CACHE_KEY)
        except Exception:
            logger.error(
                "system_config.cache_invalidation_failed",
                exc_info=True,
            )

    # 5. Re-read from OpenBao (cache is cold — forces fresh read)
    return await get_system_config(
        redis=redis,
        bao_client=bao_client,
        skip_cache=True,
    )
