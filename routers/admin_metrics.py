"""Admin metrics endpoints — HTTP adapter layer only.

Provides aggregated metrics for the admin panel frontend, combining
DB-sourced counts with Prometheus-backed latency/error metrics.

Endpoints:
    GET /metrics/summary   — Aggregated RED + DB metrics for the admin panel
    GET /metrics/query     — Run an arbitrary PromQL query
    GET /metrics/targets   — List Prometheus scrape targets and health

All endpoints require API key or JWT authentication.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from dependencies.auth import require_org_id
from dependencies.db import get_db
from models.episode import Episode
from models.graph_entity import GraphEntity
from models.user import User
from schemas.admin_metrics import (
    EpisodeStats,
    GraphStats,
    MetricsSummaryResponse,
)
from services.metrics_service import MetricsService

router = APIRouter(
    prefix="/metrics",
    tags=["Admin - Metrics"],
)


# ── Dependency ────────────────────────────────────────────────────────────────


def _get_metrics_service() -> MetricsService:
    """Dependency factory for ``MetricsService``."""
    return MetricsService(prometheus_url=settings.PROMETHEUS_URL)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get(
    "/summary",
    response_model=MetricsSummaryResponse,
    summary="Aggregated admin dashboard metrics",
    description=(
        "Returns a combined view of DB counts (episodes, users, graphs) and "
        "Prometheus-backed performance metrics (latency, error rate, request "
        "rate).  The ``status`` field is ``\"degraded\"`` if Prometheus is "
        "unreachable — DB counts are still returned."
    ),
)
async def get_metrics_summary(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    prom: MetricsService = Depends(_get_metrics_service),
) -> MetricsSummaryResponse:
    """Get aggregated metrics for the admin dashboard.

    Merges DB counts and Prometheus metrics into a single response.
    DB counts are scoped to the authenticated organization.
    """
    org_uuid = UUID(org_id)

    # ── DB counts (run concurrently) ─────────────────────────────────────
    episode_stats, graph_stats, user_count = await _fetch_db_counts(
        db, org_uuid
    )

    # ── Prometheus metrics ───────────────────────────────────────────────
    perf = await prom.get_summary()

    # Overwrite DB fields into the response
    perf.episodes = episode_stats
    perf.graphs = graph_stats
    perf.users_total = user_count

    return perf


@router.get(
    "/query",
    summary="Run arbitrary PromQL query",
    description=(
        "Executes a PromQL instant query and returns the raw result. "
        "Useful for the frontend to build custom charts without "
        "backend changes.  Returns 502 if Prometheus is unreachable."
    ),
)
async def get_promql_query(
    query: str = Query(..., description="PromQL query string"),
    _org_id: str = Depends(require_org_id),
    prom: MetricsService = Depends(_get_metrics_service),
) -> dict:
    """Run an arbitrary PromQL query.

    Args:
        query: The PromQL expression to evaluate.

    Returns:
        Raw Prometheus query result with ``status`` and ``data`` fields.
    """
    import httpx

    base_url = settings.PROMETHEUS_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{base_url}/api/v1/query",
                params={"query": query},
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "status": "ok",
                "query": query,
                "data": data.get("data", {}),
            }
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Prometheus query failed: {exc}",
        ) from exc


@router.get(
    "/targets",
    summary="Prometheus scrape targets",
    description=(
        "Lists all Prometheus scrape targets and their current health. "
        "Useful for the admin panel's health indicator.  Returns 502 if "
        "Prometheus is unreachable."
    ),
)
async def get_prometheus_targets(
    _org_id: str = Depends(require_org_id),
) -> dict:
    """Get Prometheus scrape target health.

    Returns:
        Dict with ``targets`` list and ``status``.
    """
    import httpx

    base_url = settings.PROMETHEUS_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{base_url}/api/v1/targets")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Prometheus targets unavailable: {exc}",
        ) from exc

    targets = []
    for t in data.get("data", {}).get("activeTargets", []):
        targets.append({
            "job": t.get("labels", {}).get("job", ""),
            "instance": t.get("labels", {}).get("instance", ""),
            "health": t.get("health", "unknown"),
            "last_scrape": t.get("lastScrape", ""),
            "last_error": t.get("lastError", "") or None,
        })

    return {"status": "ok", "targets": targets}


# ── DB helper functions ───────────────────────────────────────────────────────


async def _fetch_db_counts(
    db: AsyncSession, org_id: UUID
) -> tuple[EpisodeStats, GraphStats, int]:
    """Run all DB count queries for the admin summary.

    Args:
        db: Async database session.
        org_id: Organization UUID for tenant isolation.

    Returns:
        Tuple of (EpisodeStats, GraphStats, user_count).
    """
    # Define enrichment bitmask constants (mirrors services/worker/tasks/base.py)
    ENRICHMENT_ENTITY_LINKS = 1 << 3
    ENRICHMENT_NONE = 0

    # ── Episode counts ──────────────────────────────────────────────────
    # Total episodes
    total_ep_result = await db.execute(
        select(func.count(Episode.id)).where(
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
        )
    )
    episodes_total = total_ep_result.scalar() or 0

    # Episodes in last 24h
    ep_24h_result = await db.execute(
        select(func.count(Episode.id)).where(
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            Episode.created_at >= func.now() - text("interval '24 hours'"),
        )
    )
    episodes_24h = ep_24h_result.scalar() or 0

    # Episodes with incomplete enrichment (some bits still 0)
    in_prog_result = await db.execute(
        select(func.count(Episode.id)).where(
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            Episode.enrichment_status != 63,  # not all bits set
        )
    )
    episodes_in_progress = in_prog_result.scalar() or 0

    # Episodes with no enrichment started
    pending_result = await db.execute(
        select(func.count(Episode.id)).where(
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            Episode.enrichment_status == ENRICHMENT_NONE,
        )
    )
    episodes_pending = pending_result.scalar() or 0

    # Fully enriched episodes (all 6 bits = status 63)
    fully_enriched_result = await db.execute(
        select(func.count(Episode.id)).where(
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            Episode.enrichment_status == 63,
        )
    )
    episodes_fully_enriched = fully_enriched_result.scalar() or 0

    # Episodes with embedding populated
    with_embeddings_result = await db.execute(
        select(func.count(Episode.id)).where(
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
            Episode.embedding.isnot(None),
        )
    )
    episodes_with_embeddings = with_embeddings_result.scalar() or 0

    episode_stats = EpisodeStats(
        added_total=episodes_total,
        added_24h=episodes_24h,
        in_progress=episodes_in_progress,
        enrichment_pending=episodes_pending,
        fully_enriched=episodes_fully_enriched,
        with_embeddings=episodes_with_embeddings,
        fully_enriched_pct=round(
            episodes_fully_enriched / episodes_total * 100, 1
        ) if episodes_total > 0 else 0.0,
    )

    # ── Graph counts ────────────────────────────────────────────────────
    entities_result = await db.execute(
        select(func.count(GraphEntity.id)).where(
            GraphEntity.organization_id == org_id,
        )
    )
    entities_total = entities_result.scalar() or 0

    entities_24h_result = await db.execute(
        select(func.count(GraphEntity.id)).where(
            GraphEntity.organization_id == org_id,
            GraphEntity.created_at >= func.now() - text("interval '24 hours'"),
        )
    )
    entities_24h = entities_24h_result.scalar() or 0

    graph_stats = GraphStats(
        entities_total=entities_total,
        entities_24h=entities_24h,
        relationships_total=0,  # GraphRelationship model TBD
    )

    # ── User count ──────────────────────────────────────────────────────
    users_result = await db.execute(
        select(func.count(User.id)).where(
            User.organization_id == org_id,
            User.is_deleted.is_(False),
        )
    )
    users_total = users_result.scalar() or 0

    return episode_stats, graph_stats, users_total
