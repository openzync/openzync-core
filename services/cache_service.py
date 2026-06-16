"""Cache service — cache-aside pattern with stampede prevention.

Provides a thin wrapper around Redis for context caching operations.
Uses the cache-aside pattern with optional ``SET NX EX`` stampede
protection so that concurrent requests for the same cache key
serialise the recompute behind a single distributed lock.

Key namespace convention: ``ctx:{org_id}:{user_id}:{query_hash}``
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, TypeVar
from uuid import UUID

logger = logging.getLogger(__name__)

# ── Type variable for the generic ``get_or_compute`` method ───────────────────

T = TypeVar("T")

# ── Constants ──────────────────────────────────────────────────────────────────

STAMPEDE_LOCK_TTL: int = 10
"""TTL in seconds for the stampede-protection lock.

Should be shorter than the actual cache TTL.  If a worker holding the
lock crashes, the lock auto-releases after this period.
"""


class CacheService:
    """Cache-aside service with optional stampede protection.

    Wraps an async Redis client.  When ``redis`` is ``None``, all
    operations are no-ops — the service degrades gracefully without
    caching infrastructure.

    Args:
        redis: An optional async Redis client.
        default_ttl: Default cache TTL in seconds.  **Required** — there
            is no fallback.  Callers can override this per-call via the
            ``ttl`` parameter.
    """

    def __init__(
        self, redis: object | None = None, default_ttl: int | None = None
    ) -> None:
        if default_ttl is None:
            raise ValueError(
                "default_ttl is required. "
                "Set context_cache_ttl in the per-org configuration "
                "via PATCH /admin/org/config."
            )
        self._redis = redis
        self._default_ttl: int = default_ttl

    # ── Public API ──────────────────────────────────────────────────────────────

    async def get(self, key: str) -> str | None:
        """Retrieve a cached value by key.

        Args:
            key: The full Redis key (already namespaced).

        Returns:
            The cached string value, or ``None`` if missing or Redis is
            unavailable.
        """
        if self._redis is None:
            return None
        try:
            from redis.asyncio import Redis as AsyncRedis

            r: AsyncRedis = self._redis  # type: ignore[assignment]
            return await r.get(key)
        except Exception:
            logger.warning(
                "cache_service.get_failed",
                extra={"key": key},
                exc_info=True,
            )
            return None

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        """Set a cached value with an optional TTL.

        Args:
            key: The full Redis key.
            value: The string value to cache.
            ttl: TTL in seconds.  Falls back to ``self._default_ttl``
                (which defaults to ``30``) when ``None``.

        Returns:
            ``True`` if the value was set, ``False`` if Redis is
            unavailable.
        """
        if self._redis is None:
            return False
        try:
            from redis.asyncio import Redis as AsyncRedis

            r: AsyncRedis = self._redis  # type: ignore[assignment]
            effective_ttl = ttl if ttl is not None else self._default_ttl
            return await r.setex(key, effective_ttl, value)  # type: ignore[return-value]
        except Exception:
            logger.warning(
                "cache_service.set_failed",
                extra={"key": key, "ttl": ttl},
                exc_info=True,
            )
            return False

    async def delete(self, key: str) -> bool:
        """Delete a single cache key.

        Args:
            key: The full Redis key to delete.

        Returns:
            ``True`` if at least one key was deleted, ``False`` otherwise.
        """
        if self._redis is None:
            return False
        try:
            from redis.asyncio import Redis as AsyncRedis

            r: AsyncRedis = self._redis  # type: ignore[assignment]
            deleted = await r.delete(key)
            return deleted > 0
        except Exception:
            logger.warning(
                "cache_service.delete_failed",
                extra={"key": key},
                exc_info=True,
            )
            return False

    async def get_or_compute(
        self,
        key: str,
        compute_fn: Callable[[], T],
        ttl: int | None = None,
        enable_stampede_protection: bool = True,
    ) -> T:
        """Cache-aside read with optional stampede protection.

        Attempts to read from cache first.  On a cache miss, acquires a
        lock (``SET NX EX``) and calls ``compute_fn`` under the lock.
        The result is cached before the lock is released.

        Stampede protection uses a separate lock key (``{key}:lock``)
        to ensure only one process recomputes the value when the cache
        is cold or expired.

        Args:
            key: The full Redis cache key.
            compute_fn: A callable that produces the value to cache.
                Called only on cache miss or when the lock is acquired.
            ttl: Cache TTL in seconds.  Falls back to
                ``self._default_ttl`` (default ``30``) when ``None``.
            enable_stampede_protection: When ``True``, uses ``SET NX EX``
                to prevent multiple concurrent recomputes.

        Returns:
            The cached or freshly computed value.
        """
        # ── Check cache ──────────────────────────────────────────────────
        cached = await self.get(key)
        if cached is not None:
            # The compute_fn may return a different type (e.g. dict), but
            # if we cached a string we return it as-is.  Callers that
            # expect structured data should use ``get`` / ``set`` directly
            # or JSON-serialise in the compute_fn.
            return cached  # type: ignore[return-value]

        # ── Stampede protection ──────────────────────────────────────────
        if enable_stampede_protection and self._redis is not None:
            lock_key = f"{key}:lock"
            try:
                from redis.asyncio import Redis as AsyncRedis

                r: AsyncRedis = self._redis  # type: ignore[assignment]
                acquired = await r.set(lock_key, "1", nx=True, ex=STAMPEDE_LOCK_TTL)
                if acquired:
                    pass  # Lock set atomically with TTL
                else:
                    # Another process is computing — wait briefly and
                    # retry the cache read.
                    import asyncio

                    await asyncio.sleep(0.1)
                    retried = await self.get(key)
                    if retried is not None:
                        return retried  # type: ignore[return-value]
                    # Lock holder may have crashed — proceed to compute.
            except Exception:
                logger.warning(
                    "cache_service.stampede_lock_failed",
                    extra={"key": key},
                    exc_info=True,
                )

        # ── Compute and cache ────────────────────────────────────────────
        value = compute_fn()
        effective_ttl = ttl if ttl is not None else self._default_ttl

        if isinstance(value, str):
            await self.set(key, value, ttl=effective_ttl)
        else:
            # Serialise non-string types to JSON for caching.
            await self.set(key, json.dumps(value), ttl=effective_ttl)

        # Release the stampede lock if we acquired it
        if enable_stampede_protection and self._redis is not None:
            try:
                from redis.asyncio import Redis as AsyncRedis

                r: AsyncRedis = self._redis  # type: ignore[assignment]
                await r.delete(lock_key)
            except Exception:
                pass  # Lock will expire via TTL

        return value

    # ── Key Builders ───────────────────────────────────────────────────────────

    @staticmethod
    def build_context_cache_key(
        org_id: str,
        user_id: str,
        query: str,
    ) -> str:
        """Build a namespaced cache key for context assembly results.

        Key format: ``ctx:{org_id}:{user_id}:{query_hash}``

        The query is SHA-256 hashed to keep keys a bounded length
        regardless of query length.

        Args:
            org_id: The organization UUID string.
            user_id: The user UUID string.
            query: The natural-language query string.

        Returns:
            A namespaced Redis key string.
        """
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return f"ctx:{org_id}:{user_id}:{query_hash}"

    @staticmethod
    def build_user_cache_pattern(org_id: str, user_id: str) -> str:
        """Build a Redis key pattern for all context cache entries of a user.

        Used by the invalidation routine on new message ingestion.

        Args:
            org_id: The organization UUID string.
            user_id: The user UUID string.

        Returns:
            A glob pattern string for ``SCAN`` / ``KEYS``.
        """
        return f"ctx:{org_id}:{user_id}:*"

    # ── Invalidation ───────────────────────────────────────────────────────────

    async def invalidate_user_context(
        self,
        org_id: str,
        user_id: str,
    ) -> int:
        """Invalidate all context cache entries for a user.

        Uses ``SCAN`` + ``DEL`` to match ``ctx:{org_id}:{user_id}:*``.
        This is called by the ingestion service after new messages arrive
        so that subsequent context queries fetch fresh data.

        Args:
            org_id: The organization UUID string.
            user_id: The user UUID string.

        Returns:
            Number of cache keys deleted.
        """
        if self._redis is None:
            return 0

        pattern = self.build_user_cache_pattern(org_id, user_id)
        cursor: int = 0
        deleted = 0

        try:
            from redis.asyncio import Redis as AsyncRedis

            r: AsyncRedis = self._redis  # type: ignore[assignment]
            while True:
                cursor, keys = await r.scan(
                    cursor=cursor, match=pattern, count=100
                )
                if keys:
                    deleted += await r.delete(*keys)
                if cursor == 0:
                    break

            if deleted > 0:
                logger.info(
                    "cache_service.context_cache_invalidated",
                    extra={
                        "org_id": org_id,
                        "user_id": user_id,
                        "keys_deleted": deleted,
                    },
                )
        except Exception:
            logger.warning(
                "cache_service.invalidation_failed",
                extra={"org_id": org_id, "user_id": user_id},
                exc_info=True,
            )

        return deleted
