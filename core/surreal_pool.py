"""Per-org SurrealDB connection pool — created lazily, cached by org_id.

Each org gets its own ``AsyncSurreal`` connection using credentials from
:class:`OrgConfigBase`.  Connections are created on first use and cached
for the lifetime of the worker (or until :meth:`close_all` is called on
shutdown).

Thread safety: per-org ``asyncio.Lock`` prevents duplicate connections
under concurrent requests for the same org.

Usage::

    from core.surreal_pool import SurrealConnectionPool

    pool = SurrealConnectionPool()
    surreal = await pool.get_or_create(org_id, org_config)
    # ... use surreal ...
    await pool.close_all()  # on shutdown
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING
from uuid import UUID

from surrealdb import AsyncSurreal

from core.exceptions import GraphBackendUnavailableError

if TYPE_CHECKING:
    from schemas.organization_config import OrgConfigBase

logger = logging.getLogger(__name__)

# ── Default fallbacks for per-org SurrealDB fields ─────────────────────────

DEFAULT_SURREALDB_USER: str = "root"
"""Fallback username when ``org_config.surrealdb_user`` is ``None``."""

DEFAULT_SURREALDB_PASS: str = "root"
"""Fallback password when ``org_config.surrealdb_pass`` is ``None``."""

DEFAULT_SURREALDB_NAMESPACE: str = "openzep"
"""Fallback namespace when ``org_config.surrealdb_namespace`` is ``None``."""

DEFAULT_SURREALDB_DATABASE: str = "openzep"
"""Fallback database when ``org_config.surrealdb_database`` is ``None``."""


# ── Pool Implementation ────────────────────────────────────────────────────


class SurrealConnectionPool:
    """Per-org ``AsyncSurreal`` connection cache.

    Connections are created **lazily** on the first call to
    :meth:`get_or_create` for each org.  Subsequent calls return the
    cached ``AsyncSurreal`` instance.

    The pool is safe for concurrent use — per-org ``asyncio.Lock``
    prevents duplicate connections when two requests for the same org
    arrive simultaneously.

    When an org has no ``surrealdb_url`` configured, or the connection
    fails, :meth:`get_or_create` raises ``GraphBackendUnavailableError``.
    """

    def __init__(self) -> None:
        # ``_pool[org_id]`` = {"surreal": AsyncSurreal, "last_used": float}
        self._pool: dict[UUID, dict] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def get_or_create(
        self,
        org_id: UUID,
        org_config: OrgConfigBase,
    ) -> AsyncSurreal:
        """Return a cached ``AsyncSurreal`` for this org, or create one.

        Args:
            org_id: The organisation UUID — used as the cache key.
            org_config: The per-org configuration containing SurrealDB
                connection details (``surrealdb_url``, ``surrealdb_user``,
                ``surrealdb_pass``, ``surrealdb_namespace``,
                ``surrealdb_database``).

        Returns:
            An ``AsyncSurreal`` instance.

        Raises:
            GraphBackendUnavailableError: If no SurrealDB URL is configured
                for the org, or if the connection attempt fails.
        """
        # ── Fast path: already connected ──────────────────────────────────
        if org_id in self._pool:
            conn = self._pool[org_id]
            conn["last_used"] = time.monotonic()
            return conn["surreal"]

        # ── No URL configured = SurrealDB not available for this org ──────
        url = org_config.surrealdb_url
        if not url:
            raise GraphBackendUnavailableError(
                f"Failed to connect to SurrealDB for organization {org_id}."
            )

        # ── Per-org lock to prevent duplicate connections ─────────────────
        if org_id not in self._locks:
            self._locks[org_id] = asyncio.Lock()

        async with self._locks[org_id]:
            # Double-check after acquiring lock
            if org_id in self._pool:
                return self._pool[org_id]["surreal"]

            # Create a new AsyncSurreal connection
            try:
                surreal = AsyncSurreal(url)
                await surreal.connect()
                await surreal.signin({
                    "username": (
                        org_config.surrealdb_user or DEFAULT_SURREALDB_USER
                    ),
                    "password": (
                        org_config.surrealdb_pass or DEFAULT_SURREALDB_PASS
                    ),
                })
                await surreal.use(
                    org_config.surrealdb_namespace
                    or DEFAULT_SURREALDB_NAMESPACE,
                    org_config.surrealdb_database
                    or DEFAULT_SURREALDB_DATABASE,
                )
                self._pool[org_id] = {
                    "surreal": surreal,
                    "last_used": time.monotonic(),
                }
                logger.info(
                    "surreal_pool.connected",
                    extra={"org_id": str(org_id)},
                )
                return surreal
            except Exception as exc:
                logger.error(
                    "surreal_pool.connect_failed",
                    extra={"org_id": str(org_id), "error": str(exc)},
                    exc_info=True,
                )
                raise GraphBackendUnavailableError(
                    f"SurrealDB connection failed for organization {org_id}."
                ) from exc

    async def close_all(self) -> None:
        """Close all cached connections (call during application shutdown).

        Iterates over every cached ``AsyncSurreal`` connection and closes
        it.  Individual close failures are logged but do not prevent other
        connections from being closed.  The pool is cleared after all
        connections have been (attempted to be) closed.
        """
        for org_id, conn in list(self._pool.items()):
            try:
                await conn["surreal"].close()
                logger.info(
                    "surreal_pool.disconnected",
                    extra={"org_id": str(org_id)},
                )
            except Exception as exc:
                logger.warning(
                    "surreal_pool.close_failed",
                    extra={"org_id": str(org_id), "error": str(exc)},
                )
        self._pool.clear()
        self._locks.clear()
