"""Readiness checks backed by the database — called by routers/health.py.

DB access lives here (services layer), never in the router.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import rls
from repositories.graph_relationship_repository import GraphRelationshipRepository

logger = logging.getLogger(__name__)

SURREAL_PROBE_TIMEOUT_S: float = 2.0
"""Budget for the one-shot SurrealDB /ready probe — a hung server must not
stall the readiness check."""


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


async def get_graphs_status(
    falkordb_client: Any | None,
) -> dict[str, dict[str, bool | None]]:
    """Probe graph-backend reachability for ``/ready`` (informational only).

    Never raises and never affects the ready/degraded verdict — every
    failure is reported as ``reachable: false`` in the payload.

    Args:
        falkordb_client: The app-level FalkorDB client (``None`` when boot
            degraded without one).

    Returns:
        ``{"falkordb": {"configured": True, "reachable": bool},
        "surrealdb": {"configured": bool, "reachable": bool | None}}`` —
        booleans only, never connection details.
    """
    return {
        "falkordb": {
            "configured": True,
            "reachable": await _ping_falkordb(falkordb_client),
        },
        "surrealdb": await _probe_surrealdb(),
    }


async def _ping_falkordb(falkordb_client: Any | None) -> bool:
    """PING FalkorDB via the app client (best-effort).

    Args:
        falkordb_client: The app-level FalkorDB client, or ``None``.

    Returns:
        ``True`` on PONG, ``False`` when there is no client or on any error.
    """
    if falkordb_client is None:
        return False
    try:
        await falkordb_client.connection.ping()
        return True
    except Exception:
        logger.warning("readiness.falkordb_unreachable")
        return False


async def _probe_surrealdb() -> dict[str, bool | None]:
    """One-shot SurrealDB reachability probe (best-effort).

    Returns ``reachable: None`` with zero network I/O when
    ``OZ_SURREALDB_URL`` is unset; otherwise connects once under a short
    timeout and always closes the connection.

    Returns:
        ``{"configured": bool, "reachable": bool | None}``.
    """
    from core.config import get_settings

    settings = get_settings()
    if not settings.SURREALDB_URL:
        return {"configured": False, "reachable": None}
    try:
        from surrealdb import AsyncSurreal

        from core.surreal_pool import SurrealConnectionPool

        surreal = AsyncSurreal(settings.SURREALDB_URL)
        try:
            await asyncio.wait_for(
                SurrealConnectionPool._connect(
                    surreal,
                    settings.SURREALDB_USER,
                    settings.SURREALDB_PASS,
                    settings.SURREALDB_NAMESPACE,
                    settings.SURREALDB_DATABASE,
                ),
                timeout=SURREAL_PROBE_TIMEOUT_S,
            )
            return {"configured": True, "reachable": True}
        finally:
            try:
                await surreal.close()
            except Exception:
                logger.debug("readiness.surrealdb_probe_close_failed")
    except Exception:
        logger.warning("readiness.surrealdb_unreachable")
        return {"configured": True, "reachable": False}
