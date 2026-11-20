"""Per-user configuration resolution — cache-first, DB-authoritative.

Every request path and background worker that needs user-level settings (LLM,
embeddings, graph, behaviour) should resolve them through this module.

Resolution order:
1. Redis cache (key ``org_config:{user_id}``, TTL 5 min) — performance
   optimisation only.  Cache failures are logged at ERROR but the request
   continues to the DB.
2. ``users.config_override`` (the authoritative store).  DB failures
   propagate as hard errors.

There is **no** env-var fallback — if a field is not set in the DB it is
returned as ``None`` and the caller decides what to do.

On config update the cache is invalidated inline; invalidation failures are
logged at ERROR but do not fail the operation (stale cache expires via TTL).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import redis.asyncio
import structlog
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ValidationError as OrgConfigValidationError
from schemas.organization_config import (
    OrgConfigBase,
    UpdateOrgConfigRequest,
)

logger = logging.getLogger(__name__)


def _request_id() -> str:
    """Return the current request correlation ID (``""`` outside requests)."""
    return structlog.contextvars.get_contextvars().get("request_id", "")

# ── Constants ─────────────────────────────────────────────────────────────────

ORG_CONFIG_CACHE_TTL: int = 300
"""TTL in seconds for cached org config (default 5 minutes)."""
CACHE_KEY_PREFIX: str = "org_config"
"""Redis key prefix for cached org config."""


# ── Public API ────────────────────────────────────────────────────────────────


def _invalid_field_names(exc: PydanticValidationError) -> list[str]:
    """Extract stored field names from a pydantic error — names only.

    ``exc.errors()`` carries the rejected ``input`` values, which may be
    secrets (API keys, connection strings).  Only the ``loc`` field names
    are ever surfaced — never values.

    Args:
        exc: The pydantic ``ValidationError`` from config parsing.

    Returns:
        Sorted stored field names that failed validation.
    """
    fields: set[str] = set()
    for err in exc.errors():
        loc = err.get("loc")
        if loc:
            fields.add(str(loc[0]))
    return sorted(fields)


def _with_graph_default(raw: dict[str, Any]) -> OrgConfigBase:
    """Build an :class:`OrgConfigBase` defaulting an unset graph backend.

    Args:
        raw: The stored override dict (may be empty).

    Returns:
        The validated config with ``graph_backend="falkordb"`` when unset.

    Raises:
        OrgConfigValidationError: If stored values fail schema validation.
    """
    if not raw.get("graph_backend"):
        raw = {**raw, "graph_backend": "falkordb"}
    try:
        return OrgConfigBase(**raw)
    except PydanticValidationError as exc:
        fields = _invalid_field_names(exc)
        logger.warning(
            "org_config.stored_config_invalid",
            extra={"fields": fields},
        )
        raise OrgConfigValidationError(
            message=(
                "Stored org config failed validation in field(s): "
                f"{', '.join(fields) or 'unknown'}. "
                "Fix via PATCH /admin/org/config."
            ),
            detail={"fields": fields},
        ) from None


async def get_org_config(
    user_id: UUID,
    redis: redis.asyncio.Redis | None = None,
    db: AsyncSession | None = None,
    *,
    skip_cache: bool = False,
) -> OrgConfigBase:
    """Fetch the stored config for a user: cache → DB.

    There is no env-var fallback — every field is returned as stored in
    ``users.config_override``.  Callers that need a default value when a
    field is ``None`` must implement that logic themselves.

    Args:
        user_id: The user UUID.
        redis: An optional async Redis client.  When ``None``, caching is
            skipped.
        db: Async DB session for the ``users.config_override`` read.
            Required when the cache misses.
        skip_cache: If ``True``, bypass cache and always fetch from the DB.

    Returns:
        An :class:`OrgConfigBase` with only the fields that are explicitly
        set.  Unset fields are ``None`` (except ``graph_backend``, which
        defaults to ``"falkordb"`` for new users).

    Raises:
        ValidationError: If the stored config fails schema validation
            (poisoned field values written before stricter constraints).
        RuntimeError: If *db* is ``None`` on a cache miss.
    """
    cache_key = f"{CACHE_KEY_PREFIX}:{user_id}"

    # 1. Try cache (unless skip_cache is set)
    if not skip_cache and redis is not None:
        try:
            cached = await redis.get(cache_key)
            if cached:
                try:
                    return OrgConfigBase.model_validate_json(cached)
                except PydanticValidationError as exc:
                    # Poisoned cache entry — drop it and fall through to
                    # the DB (authoritative).  Field names only, never values.
                    logger.warning(
                        "org_config.cache_invalid",
                        extra={
                            "user_id": str(user_id),
                            "fields": _invalid_field_names(exc),
                        },
                    )
                    try:
                        await redis.delete(cache_key)
                    except Exception:
                        logger.error(
                            "org_config.cache_invalidation_failed",
                            extra={
                                "user_id": str(user_id),
                                "request_id": _request_id(),
                            },
                            exc_info=True,
                        )
        except Exception:
            logger.error(
                "org_config.cache_read_failed",
                extra={"user_id": str(user_id), "request_id": _request_id()},
                exc_info=True,
            )

    # 2. Fetch from DB (authoritative)
    if db is None:
        raise RuntimeError("get_org_config requires a DB session on cache miss")
    from repositories.user_repository import UserRepository  # noqa: PLC0415 — lazy, avoids import cycle

    raw = await UserRepository(db).get_config_override(user_id)
    # New users have {} — default only graph_backend to falkordb
    # (not disabled); all other fields stay None until explicitly set.
    org_config = _with_graph_default(dict(raw))

    # 3. Write to cache (best-effort)
    if not skip_cache and redis is not None:
        try:
            await redis.setex(
                cache_key, ORG_CONFIG_CACHE_TTL, org_config.model_dump_json()
            )
        except Exception:
            logger.error(
                "org_config.cache_write_failed",
                extra={"user_id": str(user_id), "request_id": _request_id()},
                exc_info=True,
            )

    return org_config


async def update_org_config(
    user_id: UUID,
    update_data: UpdateOrgConfigRequest | dict[str, Any],
    redis: redis.asyncio.Redis | None = None,
    db: AsyncSession | None = None,
) -> OrgConfigBase:
    """Update stored user config in the DB, invalidate cache, return fresh config.

    Performs a shallow merge: provided keys replace existing stored values.
    Keys set to ``None`` are removed from the stored config (returning
    ``None`` on next read).

    The DB (``users.config_override``) is the sole authoritative store —
    there is no dual-write.

    Args:
        user_id: The user UUID.
        update_data: Fields to update.  Can be a :class:`UpdateOrgConfigRequest`
            or a plain dict.
        redis: An optional async Redis client (for cache invalidation).
        db: Async DB session for the read/write.  Required.

    Returns:
        The freshly stored config after the update.
    """
    if isinstance(update_data, UpdateOrgConfigRequest):
        update_dict = update_data.model_dump(exclude_unset=True)
    else:
        update_dict = update_data

    if db is None:
        raise RuntimeError("update_org_config requires a DB session")
    from repositories.user_repository import UserRepository  # noqa: PLC0415 — lazy, avoids import cycle

    repo = UserRepository(db)

    # 1. Read existing config from DB
    existing = dict(await repo.get_config_override(user_id))

    # 2. Shallow merge: provided keys override; None values remove the key.
    for key, value in update_dict.items():
        if value is None:
            existing.pop(key, None)
        else:
            existing[key] = value

    # 3. Write to DB (authoritative store)
    await repo.set_config_override(user_id, existing)

    # 4. Invalidate Redis cache
    if redis is not None:
        cache_key = f"{CACHE_KEY_PREFIX}:{user_id}"
        try:
            await redis.delete(cache_key)
        except Exception:
            logger.error(
                "org_config.cache_invalidation_failed",
                extra={"user_id": str(user_id), "request_id": _request_id()},
                exc_info=True,
            )

    # 5. Re-read from DB (cache is cold — forces fresh read)
    return await get_org_config(user_id, redis=redis, db=db, skip_cache=True)


def build_cache_key(user_id: UUID) -> str:
    """Build the Redis cache key for a user's config.

    Args:
        user_id: The user UUID.

    Returns:
        A string like ``"org_config:<uuid>"``.
    """
    return f"{CACHE_KEY_PREFIX}:{user_id}"
