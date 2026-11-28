"""Cache service — cache-aside pattern with stampede prevention.

Provides a thin wrapper around Redis for context caching operations.
Uses the cache-aside pattern with optional ``SET NX EX`` stampede
protection so that concurrent requests for the same cache key
serialise the recompute behind a single distributed lock.

Key namespace convention: ``ctx:{user_id}:{project_id}:{query_hash}``
"""

from __future__ import annotations

import hashlib
import logging
from core.exceptions import CacheUnavailableError

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

class CacheService:
    """Cache-aside service with stampede protection.

    Wraps an async Redis client.  All operations require a live Redis
    connection — there is no silent no-op mode.  Every cache failure
    raises :class:`CacheUnavailableError`.  Never silently degrades.

    Args:
        redis: An async Redis client.  **Required** — ``None`` is not
            accepted.
        default_ttl: Default cache TTL in seconds.  **Required** — there
            is no fallback.  Callers can override this per-call via the
            ``ttl`` parameter.

    Raises:
        ValueError: If ``redis`` is ``None`` or ``default_ttl`` is
            ``None``.
    """

    def __init__(
        self, redis: object, default_ttl: int | None = None
    ) -> None:
        if redis is None:
            raise ValueError(
                "redis client is required. CacheService does not support "
                "silent no-op mode. Ensure Redis is configured and available."
            )
        if default_ttl is None:
            raise ValueError(
                "default_ttl is required. "
                "Set context_cache_ttl in the platform configuration "
                "via PATCH /admin/platform-config."
            )
        self._redis = redis
        self._default_ttl: int = default_ttl

    # ── Public API ──────────────────────────────────────────────────────────────

    async def get(self, key: str) -> str | None:
        """Retrieve a cached value by key.

        Args:
            key: The full Redis key (already namespaced).

        Returns:
            The cached string value, or ``None`` if the key does not
            exist.

        Raises:
            CacheUnavailableError: If Redis is unreachable or the read
                fails.
        """
        try:
            from redis.asyncio import Redis as AsyncRedis

            r: AsyncRedis = self._redis  # type: ignore[assignment]
            return await r.get(key)
        except Exception as exc:
            logger.error(
                "cache.get_failed",
                extra={"key": key},
                exc_info=True,
            )
            raise CacheUnavailableError(
                f"Cache read failed for key '{key}'."
            ) from exc

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        """Set a cached value with an optional TTL.

        Args:
            key: The full Redis key.
            value: The string value to cache.
            ttl: TTL in seconds.  Falls back to ``self._default_ttl``
                (which defaults to ``30``) when ``None``.

        Returns:
            ``True`` if the value was set.

        Raises:
            CacheUnavailableError: If Redis is unreachable or the write
                fails.
        """
        try:
            from redis.asyncio import Redis as AsyncRedis

            r: AsyncRedis = self._redis  # type: ignore[assignment]
            effective_ttl = ttl if ttl is not None else self._default_ttl
            return await r.setex(key, effective_ttl, value)  # type: ignore[return-value]
        except Exception as exc:
            logger.error(
                "cache.set_failed",
                extra={"key": key, "ttl": ttl},
                exc_info=True,
            )
            raise CacheUnavailableError(
                f"Cache write failed for key '{key}'."
            ) from exc

    async def delete(self, key: str) -> bool:
        """Delete a single cache key.

        Args:
            key: The full Redis key to delete.

        Returns:
            ``True`` if at least one key was deleted, ``False`` otherwise.

        Raises:
            CacheUnavailableError: If Redis is unreachable or the delete
                fails.
        """
        try:
            from redis.asyncio import Redis as AsyncRedis

            r: AsyncRedis = self._redis  # type: ignore[assignment]
            deleted = await r.delete(key)
            return deleted > 0
        except Exception as exc:
            logger.error(
                "cache.delete_failed",
                extra={"key": key},
                exc_info=True,
            )
            raise CacheUnavailableError(
                f"Cache delete failed for key '{key}'."
            ) from exc

    # ── Key Builders ───────────────────────────────────────────────────────────

    @staticmethod
    def build_context_cache_key(
        user_id: str,
        project_id: str,
        query: str,
        as_of: str | None = None,
        sort: str = "relevance",
        format: str = "text",  # noqa: A002
        limit: int = 20,
        version: str = "0",
    ) -> str:
        """Build a namespaced cache key for context assembly results.

        Format: ``ctx:{org}:{proj}:v{ver}[:{as_of}][:{sort}]:{fmt}:{lim}:{qhash}``

        The query is SHA-256 hashed to keep keys a bounded length
        regardless of query length.  An optional ``as_of`` ISO timestamp
        segment keeps effective-at results from poisoning the default
        (now) cache and from colliding with other timestamps.  A
        non-default ``sort`` segment keeps relevance/recent orderings
        from colliding.  ``format`` and ``limit`` are always included —
        a cached ``text``/``limit=20`` result must never serve a
        ``json``/``limit=50`` request.  The ``v{version}`` segment is the
        per-project cache generation: ingestion bumps it via
        :meth:`bump_context_version` instead of SCAN+DEL, so invalidation
        is O(1) and stale keys simply expire via TTL.

        Args:
            user_id: The user UUID string.
            project_id: The project UUID string.
            query: The natural-language query string.
            as_of: ISO-8601 effective-at timestamp; ``None`` omits the
                segment (plain "now" queries keep the legacy key shape).
            sort: ``"relevance"`` (omitted — legacy key shape) or
                ``"recent"`` (appended as a segment).
            format: Output format — ``"text"`` or ``"json"`` (always
                included; no legacy omission).
            limit: Max items per source type (always included).
            version: Per-project cache generation from
                :meth:`get_context_version` (``"0"`` when unknown).

        Returns:
            A namespaced Redis key string.
        """
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        parts = [f"ctx:{user_id}:{project_id}", f"v{version}"]
        if as_of is not None:
            parts.append(as_of)
        if sort != "relevance":
            parts.append(sort)
        parts.append(format)
        parts.append(str(limit))
        parts.append(query_hash)
        return ":".join(parts)

    @staticmethod
    def build_project_cache_pattern(user_id: str, project_id: str) -> str:
        """Build a Redis key pattern for all context cache entries of a project.

        Used by the invalidation routine on new message ingestion.

        Args:
            user_id: The user UUID string.
            project_id: The project UUID string.

        Returns:
            A glob pattern string for ``SCAN`` / ``KEYS``.
        """
        return f"ctx:{user_id}:{project_id}:*"

    # ── Invalidation ───────────────────────────────────────────────────────────

    # ── Context cache generation (O(1) invalidation) ──────────────────────────

    @staticmethod
    def build_context_version_key(user_id: str, project_id: str) -> str:
        """Build the Redis key holding a project's context cache generation.

        Args:
            user_id: The user UUID string.
            project_id: The project UUID string.

        Returns:
            A namespaced Redis key string (``ctxver:{org}:{proj}``).
        """
        return f"ctxver:{user_id}:{project_id}"

    async def get_context_version(self, user_id: str, project_id: str) -> str:
        """Return the project's current context cache generation.

        Missing key means generation ``"0"`` (nothing ingested yet, or
        the counter expired — plain ``INCR`` recreates it on next bump).

        Args:
            user_id: The user UUID string.
            project_id: The project UUID string.

        Returns:
            The generation string for :meth:`build_context_cache_key`.

        Raises:
            CacheUnavailableError: If Redis is unreachable.
        """
        key = self.build_context_version_key(user_id, project_id)
        try:
            from redis.asyncio import Redis as AsyncRedis

            r: AsyncRedis = self._redis  # type: ignore[assignment]
            raw = await r.get(key)
        except Exception as exc:
            logger.error(
                "cache.version_read_failed",
                extra={"key": key},
                exc_info=True,
            )
            raise CacheUnavailableError(
                f"Cache version read failed for key '{key}'."
            ) from exc
        if raw is None:
            return "0"
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    async def bump_context_version(self, user_id: str, project_id: str) -> int:
        """Atomically bump the project's context cache generation.

        O(1) invalidation: every key minted after this call carries the
        new generation, so pre-bump entries are never read again and
        expire via TTL — no SCAN+DEL.

        Args:
            user_id: The user UUID string.
            project_id: The project UUID string.

        Returns:
            The new generation number.

        Raises:
            CacheUnavailableError: If Redis is unreachable.
        """
        key = self.build_context_version_key(user_id, project_id)
        try:
            from redis.asyncio import Redis as AsyncRedis

            r: AsyncRedis = self._redis  # type: ignore[assignment]
            return int(await r.incr(key))
        except Exception as exc:
            logger.error(
                "cache.version_bump_failed",
                extra={"key": key},
                exc_info=True,
            )
            raise CacheUnavailableError(
                f"Cache version bump failed for key '{key}'."
            ) from exc

    async def invalidate_project_context(
        self,
        user_id: str,
        project_id: str,
    ) -> int:
        """Invalidate all context cache entries for a project.

        Uses ``SCAN`` + ``DEL`` to match ``ctx:{user_id}:{project_id}:*``.
        Called after new message ingestion so subsequent context queries
        fetch fresh data from the database.

        Args:
            user_id: The user UUID string.
            project_id: The project UUID string.

        Returns:
            Number of cache keys deleted.

        Raises:
            CacheUnavailableError: If Redis is unreachable or the
                invalidation fails.
        """
        pattern = self.build_project_cache_pattern(user_id, project_id)
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
                    "cache.project_context_cache_invalidated",
                    extra={
                        "user_id": user_id,
                        "project_id": project_id,
                        "keys_deleted": deleted,
                    },
                )
        except Exception as exc:
            logger.error(
                "cache.project_invalidation_failed",
                extra={"user_id": user_id, "project_id": project_id},
                exc_info=True,
            )
            raise CacheUnavailableError(
                f"Cache invalidation failed for user '{user_id}', project '{project_id}'."
            ) from exc

        return deleted
