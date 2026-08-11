"""Org-level RBAC — role lookup with fail-closed caching.

``get_org_role`` is the single source of truth for a user's role within
their organization.  Roles are cached in Redis for 60 seconds (cache-aside)
and the lookup **fails closed**: any infrastructure error (Redis or DB)
returns ``"member"`` so a transient outage can never elevate a user.

Roles are invalidated on role change / user deletion via
:func:`invalidate_role` so a stale cache entry cannot outlive the
authorization decision that produced it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from repositories.user_repository import UserRepository

if TYPE_CHECKING:
    from uuid import UUID

    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

RBAC_ROLE_CACHE_PREFIX: str = "rbac:role:"
"""Redis key prefix for cached user roles."""

RBAC_ROLE_CACHE_TTL: int = 60
"""TTL in seconds for cached role lookups."""

PWD_FLAG_CACHE_PREFIX: str = "rbac:pwd:flag:"  # noqa: S105  — a Redis key prefix, not a credential
"""Redis key prefix for the cached must-change-password flag."""

PWD_FLAG_CACHE_TTL: int = 60
"""TTL in seconds for cached must-change-password lookups."""


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _role_cache_key(user_id: UUID) -> str:
    """Build the Redis key for a user's cached role.

    Args:
        user_id: The user's UUID.

    Returns:
        The namespaced Redis key string.
    """
    return f"{RBAC_ROLE_CACHE_PREFIX}{user_id}"


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


async def get_org_role(
    redis: Redis,
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID,
) -> str:
    """Return the user's role within their organization (fail-closed).

    Cache-aside with a 60 s TTL.  On any Redis or DB error the function
    returns ``"member"`` — a deny, never a grant — and logs the error with
    enough context to diagnose it.

    A cache-write failure after a successful DB read does NOT downgrade to
    ``"member"``: the role was already verified against the source of truth,
    and the write is only an optimization.  Only a *read* failure (Redis or
    DB) degrades to ``"member"``.

    Args:
        redis: Async Redis client (from ``request.app.state.redis``).
        db: Request-scoped async DB session.
        org_id: The organization UUID the user belongs to.
        user_id: The user's UUID.

    Returns:
        ``"admin"`` or ``"member"``.  ``"member"`` for unknown,
        inactive, or soft-deleted users, and on read-path infrastructure
        failures.
    """
    cache_key = _role_cache_key(user_id)
    try:
        cached = await redis.get(cache_key)
        if cached is not None:
            return cached.decode() if isinstance(cached, bytes) else cached
    except Exception:
        logger.error(
            "rbac.role_cache_read_failed",
            extra={"org_id": str(org_id), "user_id": str(user_id)},
            exc_info=True,
        )

    try:
        user = await UserRepository(db).get_by_uuid(org_id, user_id)
    except Exception:
        logger.error(
            "rbac.role_db_lookup_failed",
            extra={"org_id": str(org_id), "user_id": str(user_id)},
            exc_info=True,
        )
        return "member"

    if user is None or not user.is_active or user.is_deleted:
        return "member"

    role = user.role if user.role is not None else "member"
    try:
        await redis.setex(cache_key, RBAC_ROLE_CACHE_TTL, role)
    except Exception:
        logger.error(
            "rbac.role_cache_write_failed",
            extra={"org_id": str(org_id), "user_id": str(user_id), "role": role},
            exc_info=True,
        )
    return role


async def invalidate_role(redis: Redis, user_id: UUID) -> None:
    """Delete the cached role for a user.

    Called after a role change or user deletion so the next lookup
    re-reads the source of truth.  Failures are logged, never swallowed.

    Args:
        redis: Async Redis client.
        user_id: The user's UUID.
    """
    try:
        await redis.delete(_role_cache_key(user_id))
    except Exception:
        logger.error(
            "rbac.role_cache_invalidate_failed",
            extra={"user_id": str(user_id)},
            exc_info=True,
        )


async def get_must_change_password(
    redis: Redis,
    db: AsyncSession,
    org_id: UUID,
    user_id: UUID,
) -> bool:
    """Return the user's must-change-password flag (fail-closed).

    Cache-aside with a 60 s TTL, mirroring :func:`get_org_role`.  **Fails
    closed**: any Redis or DB read error returns ``True`` — the user is
    treated as requiring a password change and denied — so an outage can
    never clear the gate.

    Args:
        redis: Async Redis client (from ``request.app.state.redis``).
        db: Request-scoped async DB session.
        org_id: The organization UUID the user belongs to.
        user_id: The user's UUID.

    Returns:
        ``True`` when the user must change their password before using
        dashboard routes, ``False`` otherwise.
    """
    cache_key = f"{PWD_FLAG_CACHE_PREFIX}{user_id}"
    try:
        cached = await redis.get(cache_key)
        if cached is not None:
            return cached == b"1" if isinstance(cached, bytes) else cached == "1"
    except Exception:
        logger.error(
            "rbac.pwd_flag_cache_read_failed",
            extra={"org_id": str(org_id), "user_id": str(user_id)},
            exc_info=True,
        )

    try:
        user = await UserRepository(db).get_by_uuid(org_id, user_id)
    except Exception:
        logger.error(
            "rbac.pwd_flag_db_lookup_failed",
            extra={"org_id": str(org_id), "user_id": str(user_id)},
            exc_info=True,
        )
        return True

    if user is None or not user.is_active or user.is_deleted:
        return True

    try:
        await redis.setex(
            cache_key,
            PWD_FLAG_CACHE_TTL,
            "1" if user.must_change_password else "0",
        )
    except Exception:
        logger.error(
            "rbac.pwd_flag_cache_write_failed",
            extra={"org_id": str(org_id), "user_id": str(user_id)},
            exc_info=True,
        )
    return bool(user.must_change_password)


async def invalidate_must_change_password(redis: Redis, user_id: UUID) -> None:
    """Delete the cached must-change-password flag for a user.

    Called after a password change clears the flag, so the next lookup
    re-reads the source of truth.  Failures are logged, never swallowed.

    Args:
        redis: Async Redis client.
        user_id: The user's UUID.
    """
    try:
        await redis.delete(f"{PWD_FLAG_CACHE_PREFIX}{user_id}")
    except Exception:
        logger.error(
            "rbac.pwd_flag_cache_invalidate_failed",
            extra={"user_id": str(user_id)},
            exc_info=True,
        )
