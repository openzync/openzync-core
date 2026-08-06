"""Expire graph edges matching a fact-triple key — ARQ worker task.

Synchronises fact supersession into non-Postgres graph backends
(SurrealDB / FalkorDB).  Enqueued by :class:`GraphEdgeSyncService` on the
low-priority queue with the edge triple key + the deterministic
supersession instant.  The task resolves the org's backend inside the
worker (``workers.backend.resolve_graph_backend``) and expires the edge
via the ``expire_relationships_matching`` contract.

Idempotency: the backend's ``WHERE invalid_at IS NULL`` predicate means a
replay of the same triple matches zero rows (count 0) — no error, no
double side effect.  This is also what makes ARQ retries and the
``reconcile_graph_edges`` cron safe.

Failure semantics: facts are the source of truth and are NEVER rolled
back on expiry failure.  On final failure the task logs the full context
(org/project/triple/at_time/fact_id), increments
``openzync_graph_edge_sync_failures_total`` and re-raises so ARQ records
the failure — nothing is swallowed silently.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from prometheus_client import Counter

from middleware.metrics import METRICS_REGISTRY
from workers.tasks.base import with_retry

logger = logging.getLogger(__name__)

graph_edge_sync_failures_total = Counter(
    "openzync_graph_edge_sync_failures_total",
    "Final failures expiring graph edges after fact supersession.",
    registry=METRICS_REGISTRY,
)


async def _resolve_backend(ctx: dict[str, Any], org_id: UUID, db: Any) -> Any:
    """Resolve the org's graph backend for this task's session.

    Delegates to ``workers.backend.resolve_graph_backend`` — the worker
    resolution path the calling context uses.  Raises
    ``GraphBackendUnavailableError`` when the worker context lacks the
    dispatcher (loud, never a silent Postgres fallback).

    Args:
        ctx: ARQ worker context dict.
        org_id: The organization UUID.
        db: The task's async SQLAlchemy session.

    Returns:
        A resolved ``GraphBackend`` instance, or ``None`` when graph is
        explicitly disabled for the org.
    """
    from workers.backend import resolve_graph_backend

    return await resolve_graph_backend(ctx, org_id, db)


@with_retry(max_retries=3, base_delay_s=2.0)
async def _expire_graph_edges(
    ctx: dict[str, Any],
    *,
    org_id: UUID,
    project_id: UUID,
    source_id: UUID,
    target_id: UUID,
    relationship_type: str,
    at_time: datetime,
) -> int:
    """Expire matching edges on the org's resolved backend (retried).

    Args:
        ctx: ARQ worker context (``db_session_factory`` + dispatcher).
        org_id: Tenant scope.
        project_id: Project scope.
        source_id: Edge source (fact subject entity).
        target_id: Edge target (fact object entity).
        relationship_type: Edge label (fact predicate).
        at_time: The supersession instant (deterministic).

    Returns:
        Number of edges set invalid (0 on replay / graph disabled).

    Raises:
        GraphBackendUnavailableError: If backend resolution fails.
    """
    session_factory = ctx.get("db_session_factory")
    if session_factory is None:
        logger.error("expire_graph_edges.no_session_factory")
        raise RuntimeError("db_session_factory missing from ARQ ctx")

    async with session_factory() as db:
        backend = await _resolve_backend(ctx, org_id, db)
        if backend is None:
            logger.info(
                "expire_graph_edges.graph_disabled",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            return 0
        count = await backend.expire_relationships_matching(
            org_id=org_id,
            project_id=project_id,
            source_id=source_id,
            target_id=target_id,
            relationship_type=relationship_type,
            at_time=at_time,
        )
        await db.commit()
        logger.info(
            "expire_graph_edges.expired",
            extra={
                "org_id": str(org_id),
                "project_id": str(project_id),
                "source_id": str(source_id),
                "target_id": str(target_id),
                "relationship_type": relationship_type,
                "at_time": at_time.isoformat(),
                "expired_count": count,
            },
        )
        return count


async def expire_graph_edges(
    ctx: dict[str, Any],
    *,
    org_id: str,
    project_id: str,
    source_id: str,
    target_id: str,
    relationship_type: str,
    at_time: datetime,
    fact_id: str,
) -> str:
    """ARQ task — expire graph edges matching a superseded fact's key.

    Wraps the retried expiry with final-failure telemetry: logs the full
    context, increments ``openzync_graph_edge_sync_failures_total`` and
    re-raises — never swallowed, never rolls back the fact commit (facts
    are the source of truth).

    Args:
        ctx: ARQ worker context dict.
        org_id: Organization UUID string.
        project_id: Project UUID string.
        source_id: Edge source UUID string.
        target_id: Edge target UUID string.
        relationship_type: Edge label (fact predicate).
        at_time: The supersession instant (deterministic).
        fact_id: The superseded fact's UUID string — idempotency/provenance.

    Returns:
        A short summary string for the ARQ log.

    Raises:
        The underlying exception after final retry exhaustion.
    """
    try:
        count = await _expire_graph_edges(
            ctx,
            org_id=UUID(org_id),
            project_id=UUID(project_id),
            source_id=UUID(source_id),
            target_id=UUID(target_id),
            relationship_type=relationship_type,
            at_time=at_time,
        )
        return (
            f"expired {count} edge(s) for "
            f"{source_id}->{target_id} {relationship_type}"
        )
    except Exception as exc:
        logger.error(
            "expire_graph_edges.failed",
            extra={
                "org_id": org_id,
                "project_id": project_id,
                "source_id": source_id,
                "target_id": target_id,
                "relationship_type": relationship_type,
                "at_time": at_time.isoformat(),
                "fact_id": fact_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        graph_edge_sync_failures_total.inc()
        raise
