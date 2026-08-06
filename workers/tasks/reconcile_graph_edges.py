"""Graph edge reconciliation — expires active edges with no active fact.

Runs as a periodic ARQ cron job (low-priority worker).  Postgres
anti-join: an active edge (``invalid_at IS NULL``) is kept only while an
active fact asserts the same edge key ``(subject_entity_id = source_id,
predicate = relationship_type, object_entity_id = target_id)``.  Edges
with no matching active fact are expired by enqueueing the
``expire_graph_edges`` task per edge.

This is the safety net AND the backfill: it self-heals any drift the
post-commit sync missed (worker crash, enqueue loss, pre-existing rows
written before Phase 3) without replaying the fact commit — facts remain
the source of truth, the edge expiry is derived state.

Idempotent by construction: the ``expire_graph_edges`` task's
``WHERE invalid_at IS NULL`` means re-expiring an already-invalidated
edge matches zero rows (count 0, no error), so overlapping cron ticks
and the post-commit sync never conflict.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

RECONCILE_BATCH_SIZE: int = 200
"""Maximum number of stale edges to expire per reconciliation tick.

Limits the burst of enqueued ``expire_graph_edges`` jobs on each run.
"""

# Active edges whose triple key has NO active fact asserting it.
# ``f.valid_to IS NULL`` excludes superseded facts — an edge kept alive by
# a superseded fact (post-commit sync failed) is exactly the drift this
# cron exists to repair.
STALE_EDGES_SQL = """
SELECT e.id, e.organization_id, e.project_id,
       e.source_id, e.target_id, e.relationship_type
FROM graph_relationships e
WHERE e.invalid_at IS NULL
      AND NOT EXISTS (
          SELECT 1
          FROM facts f
          WHERE f.invalid_at IS NULL
            AND f.valid_to IS NULL
            AND f.organization_id = e.organization_id
            AND f.project_id = e.project_id
            AND f.subject_entity_id = e.source_id
            AND f.predicate = e.relationship_type
            AND f.object_entity_id = e.target_id
      )
ORDER BY e.updated_at ASC
LIMIT :limit
"""


async def reconcile_graph_edges(ctx: dict[str, Any]) -> str:
    """Expire active graph edges that no active fact re-asserts.

    Scans active edges lacking a matching active fact (Postgres
    anti-join), batch-limited, and enqueues one ``expire_graph_edges``
    task per edge on the low-priority queue.

    Args:
        ctx: ARQ worker context dict containing ``db_session_factory``
            and ``redis`` (an :class:`ArqRedis` instance).

    Returns:
        A summary string for the cron log, e.g.
        ``"Enqueued 12 edge expiries"`` or ``"No stale edges found"``.

    Raises:
        Exception: If the DB query fails (logged by ARQ cron).
    """
    session_factory = ctx.get("db_session_factory")
    if session_factory is None:
        logger.error("reconcile_graph_edges.no_session_factory")
        return "Skipped: no db_session_factory in ARQ ctx"

    arq_redis: Any = ctx.get("redis")
    if arq_redis is None:
        logger.error("reconcile_graph_edges.no_arq_redis")
        return "Skipped: no redis in ARQ ctx"

    queue_name: str | None = ctx.get("_queue_name")
    if queue_name is None:
        # Fallback: the low queue (where this cron runs).
        queue_name = "OpenZync:development:queue:low"

    # ── Scan stale edges (Postgres anti-join) ───────────────────────────
    now = datetime.now(timezone.utc)
    stale_edges: list[dict[str, Any]] = []
    async with session_factory() as db:
        result = await db.execute(
            text(STALE_EDGES_SQL),
            {"limit": RECONCILE_BATCH_SIZE},
        )
        for row in result.all():
            stale_edges.append(
                {
                    "org_id": str(row.organization_id),
                    "project_id": str(row.project_id),
                    "source_id": str(row.source_id),
                    "target_id": str(row.target_id),
                    "relationship_type": row.relationship_type,
                    "edge_id": str(row.id),
                }
            )

    if not stale_edges:
        logger.debug("reconcile_graph_edges.nothing_stale")
        return "No stale edges found"

    logger.info(
        "reconcile_graph_edges.found_stale",
        extra={"count": len(stale_edges)},
    )

    # ── Enqueue an expiry per stale edge ────────────────────────────────
    enqueued = 0
    for edge in stale_edges:
        try:
            await arq_redis.enqueue_job(
                "expire_graph_edges",
                org_id=edge["org_id"],
                project_id=edge["project_id"],
                source_id=edge["source_id"],
                target_id=edge["target_id"],
                relationship_type=edge["relationship_type"],
                at_time=now,
                # Reconcile-sourced expiries have no superseding fact —
                # carry the edge id as provenance for log correlation.
                fact_id=edge["edge_id"],
                _queue_name=queue_name,
            )
            enqueued += 1
        except Exception as exc:
            logger.warning(
                "reconcile_graph_edges.enqueue_failed",
                extra={
                    "org_id": edge["org_id"],
                    "source_id": edge["source_id"],
                    "target_id": edge["target_id"],
                    "relationship_type": edge["relationship_type"],
                    "error": str(exc),
                },
            )

    summary = f"Enqueued {enqueued} edge expiries (from {len(stale_edges)} stale)"
    logger.info("reconcile_graph_edges.completed", summary=summary)
    return summary
