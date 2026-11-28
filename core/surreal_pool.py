"""Per-user SurrealDB connection pool — created lazily, cached by user_id.

Connection details come solely from ``OZ_SURREALDB_*`` env vars (see
``core.config.Settings``) — there is no per-user override. Each user gets
its own ``AsyncSurreal`` connection, created on first use and cached for
the lifetime of the worker (or until :meth:`close_all` is called on
shutdown).

Thread safety: per-user ``asyncio.Lock`` prevents duplicate connections
under concurrent requests for the same user.

Usage::

    from core.surreal_pool import SurrealConnectionPool

    pool = SurrealConnectionPool()
    surreal = await pool.get_or_create(user_id)
    # ... use surreal ...
    await pool.close_all()  # on shutdown
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from surrealdb import AsyncSurreal

from core.config import get_settings
from core.exceptions import GraphBackendUnavailableError

logger = logging.getLogger(__name__)

SURREAL_IDLE_TTL_S: float = 1800.0
"""Idle connections older than this are closed and evicted on next access
(30 min) — without it every org ever seen holds a socket for the worker's
whole lifetime."""

SURREAL_MAX_CACHED: int = 32
"""Cap on cached per-user connections — beyond this the least-recently-used
idle entry is evicted.  Bounds FDs when the user count grows."""

SURREAL_CONNECT_TIMEOUT_S: float = 10.0
"""Budget for connect+signin+use — a hung SurrealDB must fail fast into
``GraphBackendUnavailableError``, never hang a worker task."""


# ── Pool Implementation ────────────────────────────────────────────────────


class SurrealConnectionPool:
    """Per-user ``AsyncSurreal`` connection cache.

    Connections are created **lazily** on the first call to
    :meth:`get_or_create` for each user.  Subsequent calls return the
    cached ``AsyncSurreal`` instance.

    The pool is safe for concurrent use — per-user ``asyncio.Lock``
    prevents duplicate connections when two requests for the same user
    arrive simultaneously.

    When ``OZ_SURREALDB_URL`` is unset, or the connection fails,
    :meth:`get_or_create` raises ``GraphBackendUnavailableError``.
    """

    def __init__(self) -> None:
        # ``_pool[user_id]`` = {"surreal": AsyncSurreal, "last_used": float}
        self._pool: dict[UUID, dict] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def get_or_create(self, user_id: UUID) -> AsyncSurreal:
        """Return a cached ``AsyncSurreal`` for this user, or create one.

        Connection details (URL, credentials, namespace, database) come
        solely from ``OZ_SURREALDB_*`` env vars — ``user_id`` is only the
        cache key, so concurrent users share one server without sharing
        a connection.

        Args:
            user_id: The user UUID — used as the cache key.

        Returns:
            An ``AsyncSurreal`` instance.

        Raises:
            GraphBackendUnavailableError: If ``OZ_SURREALDB_URL`` is unset
                (surrealdb backend selected without a server), or if the
                connection attempt fails.
        """
        # ── Fast path: already connected ──────────────────────────────────
        # Idle entries past SURREAL_IDLE_TTL_S are closed + dropped here so
        # a dead-idle socket is never handed out (recreated below).
        if user_id in self._pool:
            conn = self._pool[user_id]
            if time.monotonic() - conn["last_used"] > SURREAL_IDLE_TTL_S:
                await self._close_entry(user_id)
            else:
                conn["last_used"] = time.monotonic()
                return conn["surreal"]

        # ── No URL configured = SurrealDB unavailable — fail loud ────────
        settings = get_settings()
        url = settings.SURREALDB_URL
        if not url:
            raise GraphBackendUnavailableError(
                f"SurrealDB selected for user {user_id} (graph_backend="
                "'surrealdb') but OZ_SURREALDB_URL is not set."
            )
        username = settings.SURREALDB_USER
        password = settings.SURREALDB_PASS
        namespace = settings.SURREALDB_NAMESPACE
        database = settings.SURREALDB_DATABASE

        # ── Per-user lock to prevent duplicate connections ─────────────────
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()

        async with self._locks[user_id]:
            # Double-check after acquiring lock
            if user_id in self._pool:
                return self._pool[user_id]["surreal"]

            # Cap: evict the least-recently-used entry when full so the
            # pool can't grow one socket per user without bound.
            if len(self._pool) >= SURREAL_MAX_CACHED:
                lru = min(self._pool, key=lambda k: self._pool[k]["last_used"])
                await self._close_entry(lru)
                logger.info(
                    "surreal_pool.evicted_lru",
                    extra={"user_id": str(lru)},
                )

            # Create a new AsyncSurreal connection
            try:
                surreal = AsyncSurreal(url)
                await asyncio.wait_for(
                    self._connect(surreal, username, password, namespace, database),
                    timeout=SURREAL_CONNECT_TIMEOUT_S,
                )
                self._pool[user_id] = {
                    "surreal": surreal,
                    "last_used": time.monotonic(),
                }
                logger.info(
                    "surreal_pool.connected",
                    extra={"user_id": str(user_id)},
                )
                return surreal
            except Exception as exc:
                logger.error(
                    "surreal_pool.connect_failed",
                    extra={"user_id": str(user_id), "error": str(exc)},
                    exc_info=True,
                )
                raise GraphBackendUnavailableError(
                    f"SurrealDB connection failed for user {user_id}."
                ) from exc

    @staticmethod
    async def _connect(
        surreal: AsyncSurreal,
        username: str,
        password: str,
        namespace: str,
        database: str,
    ) -> None:
        """Run connect+signin+use as one unit (wrapped in a timeout by the caller)."""
        await surreal.connect()
        await surreal.signin({
            "username": username,
            "password": password,
        })
        await surreal.use(namespace, database)

    async def _close_entry(self, user_id: UUID) -> None:
        """Close and drop one cached entry, logging (not raising) on failure."""
        conn = self._pool.pop(user_id, None)
        if conn is None:
            return
        try:
            await conn["surreal"].close()
        except Exception as exc:
            logger.warning(
                "surreal_pool.close_failed",
                extra={"user_id": str(user_id), "error": str(exc)},
            )

    async def close_all(self) -> None:
        """Close all cached connections (call during application shutdown).

        Iterates over every cached ``AsyncSurreal`` connection and closes
        it.  Individual close failures are logged but do not prevent other
        connections from being closed.  The pool is cleared after all
        connections have been (attempted to be) closed.
        """
        for user_id, conn in list(self._pool.items()):
            try:
                await conn["surreal"].close()
                logger.info(
                    "surreal_pool.disconnected",
                    extra={"user_id": str(user_id)},
                )
            except Exception as exc:
                logger.warning(
                    "surreal_pool.close_failed",
                    extra={"user_id": str(user_id), "error": str(exc)},
                )
        self._pool.clear()
        self._locks.clear()
