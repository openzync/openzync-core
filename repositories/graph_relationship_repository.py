"""Repository for graph_relationships edge-expiry bookkeeping.

Owns the stale-edge backlog count surfaced by ``GET /ready`` — an
active edge (``invalid_at IS NULL``) with no effective-at-now fact
asserting its triple key.  The COUNT mirrors the scan query in
``workers/tasks/reconcile_graph_edges.py`` (``STALE_EDGES_SQL``): if
that query changes, this one must change with it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# COUNT mirror of the reconcile scan: same anti-join, no LIMIT/ORDER BY.
_STALE_EDGES_COUNT_SQL = """
SELECT COUNT(*)
FROM graph_relationships e
WHERE e.invalid_at IS NULL
      AND NOT EXISTS (
          SELECT 1
          FROM facts f
          WHERE f.invalid_at IS NULL
          AND (f.valid_to IS NULL OR f.valid_to > :now)
          AND f.organization_id = e.organization_id
          AND f.project_id = e.project_id
          AND f.subject_entity_id = e.source_id
          AND f.predicate = e.relationship_type
          AND f.object_entity_id = e.target_id
      )
"""


class GraphRelationshipRepository:
    """Read-only edge-expiry bookkeeping for ``graph_relationships``.

    Args:
        db: An async SQLAlchemy session (RLS-bypassed for the
            platform-wide count).
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def count_stale_edges(self) -> int:
        """Count active edges with no effective-at-now fact asserting them.

        Returns:
            The reconcile backlog size (0 when fully synced).
        """
        result = await self._db.execute(
            text(_STALE_EDGES_COUNT_SQL), {"now": datetime.now(UTC)}
        )
        return int(result.scalar_one())
