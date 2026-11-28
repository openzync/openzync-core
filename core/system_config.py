"""Platform system configuration — cache-first, DB-authoritative.

Resolution order (mirrors ``core.platform_config``):
1. Redis cache (key ``system_config``, TTL 5 min) — performance
   optimisation only.  Cache failures are logged at ERROR but the request
   continues to the DB.
2. The platform root user's ``users.config_override`` (the authoritative
   store, looked up via ``UserRepository.get_platform_root``).  DB failures
   propagate as hard errors.

Only the non-secret whitelist in :mod:`schemas.system_config` is ever
read or written — the ``OZ_*`` secret env vars are never exposed.

⚠️ BREAKING: org-creation policy keys are gone (single platform org).
Registration is governed by ``instance_settings.registration_mode``.
⚠️ BREAKING: ``enrichment_gate`` is gone too (user-level only now) —
the whitelist is empty, ``GET`` returns ``{}`` and any carried key 422s.
Defaults: all fields ``None`` when the root row has no record.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import redis.asyncio  # noqa: TC002 — runtime import mirrors core/platform_config.py
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.system_config import (
    SYSTEM_CONFIG_WHITELIST,
    SystemConfigResponse,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_CONFIG_CACHE_TTL: int = 300
"""TTL in seconds for the cached system config (default 5 minutes)."""

CACHE_KEY: str = "system_config"
"""Redis key for the cached system config."""
def _request_id() -> str:
    """Return the current request correlation ID (``""`` outside requests)."""
    return structlog.contextvars.get_contextvars().get("request_id", "")


async def _read_root_override(db: AsyncSession) -> tuple[UUID | None, dict[str, Any]]:
    """Read the platform root user's id and ``config_override``.

    Args:
        db: Async DB session.

    Returns:
        A ``(root_id_or_None, override_dict)`` tuple.
    """
    from repositories.user_repository import UserRepository  # noqa: PLC0415 — lazy, avoids import cycle

    root = await UserRepository(db).get_platform_root()
    if root is None:
        return None, {}
    return root.id, dict(root.config_override or {})


# ── Public API ────────────────────────────────────────────────────────────────


async def get_system_config(
    redis: redis.asyncio.Redis | None = None,
    db: AsyncSession | None = None,
    *,
    skip_cache: bool = False,
) -> SystemConfigResponse:
    """Fetch the platform system config: cache → DB.

    Only whitelisted (non-secret) keys are returned.  When the root row has
    no record — or a whitelisted key is absent — the field defaults to
    ``None`` (registration mode lives in ``instance_settings`` now).

    Args:
        redis: An optional async Redis client.  When ``None``, caching is
            skipped.
        db: Async DB session for the root-row read.  Required on a cache miss.
        skip_cache: If ``True``, bypass cache and always fetch from the DB.

    Returns:
        A :class:`SystemConfigResponse` with the whitelisted fields.

    Raises:
        RuntimeError: If *db* is ``None`` on a cache miss.
    """
    # 1. Try cache (unless skip_cache is set)
    # Stale-cache safety: extra="ignore" drops removed keys from cached
    # JSON instead of raising.  TTL self-heals; writers delete the key
    # outright on update.
    if not skip_cache and redis is not None:
        try:
            cached = await redis.get(CACHE_KEY)
            if cached:
                return SystemConfigResponse.model_validate_json(cached)
        except Exception:
            logger.error(
                "system_config.cache_read_failed",
                extra={"request_id": _request_id()},
                exc_info=True,
            )

    # 2. Fetch from DB — the raw override may contain other keys; only
    #    the whitelist is read out of it, never the whole payload.
    if db is None:
        raise RuntimeError("get_system_config requires a DB session on cache miss")
    _, raw = await _read_root_override(db)
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
                extra={"request_id": _request_id()},
                exc_info=True,
            )

    return config
