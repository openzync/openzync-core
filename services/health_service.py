"""Readiness checks backed by the database — called by routers/health.py.

DB access lives here (services layer), never in the router.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import rls
from repositories.graph_relationship_repository import GraphRelationshipRepository

logger = logging.getLogger(__name__)


async def get_pending_edge_sync_count(
    factory: async_sessionmaker[AsyncSession] | None,
) -> int | None:
    """Count stale graph edges awaiting expiry (best-effort).

    ``None`` when the count cannot be computed — ``/ready`` reports null
    instead of failing the probe.

    Args:
        factory: The app's async session factory, or ``None`` when
            unavailable (e.g. startup bypassed in tests).

    Returns:
        The reconcile backlog size, or ``None`` on any failure.
    """
    if factory is None:
        return None
    try:
        async with factory() as session:
            # Platform-wide scan — same bypass as the reconcile cron.
            await rls.set_bypass_rls(session, True)
            return await GraphRelationshipRepository(session).count_stale_edges()
    except Exception:
        logger.exception("readiness.pending_edge_sync_failed")
        return None
