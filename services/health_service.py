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


async def is_platform_seeded(
    factory: async_sessionmaker[AsyncSession] | None,
) -> bool | None:
    """Check whether the platform org + root user exist (setup probe).

    The ONLY setup signal the frontend uses: ``/ready`` reports
    ``needs_setup=true`` when this returns ``False``.

    Args:
        factory: The app's async session factory, or ``None`` when
            unavailable (e.g. startup bypassed in tests).

    Returns:
        ``True`` when both rows exist, ``False`` when either is missing,
        ``None`` when the check cannot run (fail-open — ``/ready`` must
        not claim ``needs_setup`` on a transient DB error).
    """
    if factory is None:
        return None
    try:
        from repositories.organization_repository import OrganizationRepository
        from repositories.user_repository import UserRepository

        async with factory() as session:
            org = await OrganizationRepository(session).get_platform_org()
            if org is None:
                return False
            root = await UserRepository(session).get_platform_root()
            return root is not None
    except Exception:
        logger.exception("readiness.platform_seed_check_failed")
        return None
