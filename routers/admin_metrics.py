"""Admin metrics endpoints — HTTP adapter layer only.

Provides aggregated metrics for the admin panel frontend, combining
DB-sourced counts with Prometheus-backed latency/error metrics.

Endpoints:
    GET /metrics/summary   — Aggregated RED + DB metrics for the admin panel
    GET /metrics/queries   — List available predefined metric queries
    GET /metrics/query     — Run a predefined org-scoped metric query
    GET /metrics/targets   — List Prometheus scrape targets and health

All endpoints require API key or JWT authentication.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from dependencies.auth import require_permission
from dependencies.db import get_db
from models.episode import Episode
from models.fact import Fact
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
    return MetricsService(prometheus_url=get_settings().PROMETHEUS_URL)


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
    org_id: str = Depends(require_permission("members:read")),
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

    # ── Prometheus metrics (org-scoped) ──────────────────────────────────
    perf = await prom.get_summary(org_id=str(org_uuid))

    # Overwrite DB fields into the response
    perf.episodes = episode_stats
    perf.graphs = graph_stats
    perf.users_total = user_count

    return perf


# ── Predefined query helpers ──────────────────────────────────────────────

ENRICHMENT_STATUS_LABELS: dict[int, str] = {
    0: "pending",
    1: "fact_extraction",
    3: "fact_extraction + classification",
    7: "fact_extraction + classification + summarization",
    15: "+ entity_links",
    31: "+ embedding",
    63: "fully_enriched",
}


def _result(
    query_name: str,
    org_scoped: bool,
    columns: list[str],
    rows: list,
    params: dict,
    warning: str | None = None,
) -> dict:
    """Build the standard query response dict."""
    resp: dict = {
        "query": query_name,
        "org_scoped": org_scoped,
        "columns": columns,
        "rows": rows,
        "total": len(rows),
        "parameters": params,
    }
    if warning:
        resp["warning"] = warning
    return resp


async def _prom_instant(promql: str) -> float:
    """Run a PromQL instant query and return the scalar value."""
    base_url = get_settings().PROMETHEUS_URL.rstrip("/")
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(
            f"{base_url}/api/v1/query", params={"query": promql}
        )
        resp.raise_for_status()
        data = resp.json()
    if data["status"] != "success":
        raise HTTPException(
            status_code=502,
            detail=f"Prometheus error: {data.get('error', '')}",
        )
    results = data["data"]["result"]
    if not results:
        return 0.0
    return float(results[0]["value"][1])


async def _prom_range(promql: str, days: int) -> list[list]:
    """Run a PromQL range query and return rows as [[timestamp, value]]."""
    base_url = get_settings().PROMETHEUS_URL.rstrip("/")
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).isoformat()
    end = now.isoformat()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{base_url}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": "1h"},
        )
        resp.raise_for_status()
        data = resp.json()
    if data["status"] != "success":
        raise HTTPException(
            status_code=502,
            detail=f"Prometheus error: {data.get('error', '')}",
        )
    results = data["data"]["result"]
    if not results:
        return []
    return [[str(v[0]), float(v[1])] for v in results[0].get("values", [])]


# ── DB query handlers ─────────────────────────────────────────────────────


async def _episodes_per_day(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    conditions = [
        Episode.organization_id == org_uuid,
        Episode.is_deleted.is_(False),
        Episode.created_at >= func.now() - text(f"interval '{days} days'"),
    ]
    if project_id:
        conditions.append(Episode.project_id == UUID(project_id))
    stmt = (
        select(
            func.date_trunc("day", Episode.created_at).label("date"),
            func.count(Episode.id).label("count"),
        )
        .select_from(Episode)
        .where(*conditions)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(stmt)
    rows = [[str(r.date), r.count] for r in result]
    return _result("episodes_per_day", True, ["date", "count"], rows, {"days": days})


async def _messages_per_day(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    # Episodes = message turns; same query shape as episodes_per_day
    conditions = [
        Episode.organization_id == org_uuid,
        Episode.is_deleted.is_(False),
        Episode.created_at >= func.now() - text(f"interval '{days} days'"),
    ]
    if project_id:
        conditions.append(Episode.project_id == UUID(project_id))
    stmt = (
        select(
            func.date_trunc("day", Episode.created_at).label("date"),
            func.count(Episode.id).label("count"),
        )
        .select_from(Episode)
        .where(*conditions)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(stmt)
    rows = [[str(r.date), r.count] for r in result]
    return _result("messages_per_day", True, ["date", "count"], rows, {"days": days})


async def _users_per_day(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    stmt = (
        select(
            func.date_trunc("day", User.created_at).label("date"),
            func.count(User.id).label("count"),
        )
        .select_from(User)
        .where(
            User.organization_id == org_uuid,
            User.is_deleted.is_(False),
            User.created_at >= func.now() - text(f"interval '{days} days'"),
        )
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(stmt)
    rows = [[str(r.date), r.count] for r in result]
    return _result("users_per_day", True, ["date", "count"], rows, {"days": days})


async def _entities_per_day(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    conditions = [
        GraphEntity.organization_id == org_uuid,
        GraphEntity.created_at >= func.now() - text(f"interval '{days} days'"),
    ]
    if project_id:
        conditions.append(GraphEntity.project_id == UUID(project_id))
    stmt = (
        select(
            func.date_trunc("day", GraphEntity.created_at).label("date"),
            func.count(GraphEntity.id).label("count"),
        )
        .select_from(GraphEntity)
        .where(*conditions)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(stmt)
    rows = [[str(r.date), r.count] for r in result]
    return _result("entities_per_day", True, ["date", "count"], rows, {"days": days})


async def _facts_per_day(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    conditions = [
        Fact.organization_id == org_uuid,
        Fact.created_at >= func.now() - text(f"interval '{days} days'"),
    ]
    if project_id:
        conditions.append(Fact.project_id == UUID(project_id))
    stmt = (
        select(
            func.date_trunc("day", Fact.created_at).label("date"),
            func.count(Fact.id).label("count"),
        )
        .select_from(Fact)
        .where(*conditions)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(stmt)
    rows = [[str(r.date), r.count] for r in result]
    return _result("facts_per_day", True, ["date", "count"], rows, {"days": days})


async def _enrichment_progress(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    conditions = [
        Episode.organization_id == org_uuid,
        Episode.is_deleted.is_(False),
    ]
    if project_id:
        conditions.append(Episode.project_id == UUID(project_id))
    stmt = (
        select(
            Episode.enrichment_status,
            func.count(Episode.id).label("count"),
        )
        .select_from(Episode)
        .where(*conditions)
        .group_by(Episode.enrichment_status)
        .order_by(text("count DESC"))
    )
    result = await db.execute(stmt)
    rows = [[r.enrichment_status, r.count] for r in result]
    labels = {
        str(k): v
        for k, v in ENRICHMENT_STATUS_LABELS.items()
        if any(row[0] == k for row in rows)
    }
    resp = _result(
        "enrichment_progress", True, ["enrichment_status", "count"], rows, {}
    )
    resp["labels"] = labels
    return resp


async def _top_projects_by_episodes(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    conditions = [
        Episode.organization_id == org_uuid,
        Episode.is_deleted.is_(False),
    ]
    if project_id:
        conditions.append(Episode.project_id == UUID(project_id))
    stmt = (
        select(
            Episode.project_id,
            func.count(Episode.id).label("episode_count"),
        )
        .select_from(Episode)
        .where(*conditions)
        .group_by(Episode.project_id)
        .order_by(text("episode_count DESC"))
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = [[str(r.project_id), r.episode_count] for r in result]
    return _result(
        "top_projects_by_episodes",
        True,
        ["project_id", "episode_count"],
        rows,
        {"limit": limit},
    )


async def _top_users_by_messages(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    conditions = [
        Episode.organization_id == org_uuid,
        Episode.is_deleted.is_(False),
    ]
    if project_id:
        conditions.append(Episode.project_id == UUID(project_id))
    stmt = (
        select(
            Episode.user_id,
            func.count(Episode.id).label("message_count"),
        )
        .select_from(Episode)
        .where(*conditions)
        .group_by(Episode.user_id)
        .order_by(text("message_count DESC"))
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = [[str(r.user_id), r.message_count] for r in result]
    return _result(
        "top_users_by_messages",
        True,
        ["user_id", "message_count"],
        rows,
        {"limit": limit},
    )


# ── Prometheus query handlers (org-scoped via org_id label) ───────────────


async def _error_rate_by_day(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    promql = f'sum(increase(openzync_http_requests_total{{status="5xx",org_id="{org_uuid}"}}[1d]))'
    rows = await _prom_range(promql, days)
    return _result(
        "error_rate_by_day", True, ["timestamp", "value"], rows, {"days": days}
    )


async def _latency_percentiles(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    queries = {
        "overall_p50": f'histogram_quantile(0.50, sum(rate(openzync_http_request_duration_seconds_bucket{{org_id="{org_uuid}"}}[5m])) by (le)) * 1000',
        "overall_p95": f'histogram_quantile(0.95, sum(rate(openzync_http_request_duration_seconds_bucket{{org_id="{org_uuid}"}}[5m])) by (le)) * 1000',
        "overall_p99": f'histogram_quantile(0.99, sum(rate(openzync_http_request_duration_seconds_bucket{{org_id="{org_uuid}"}}[5m])) by (le)) * 1000',
        "context_p50": f'histogram_quantile(0.50, sum(rate(openzync_context_latency_seconds_bucket{{org_id="{org_uuid}"}}[5m])) by (le)) * 1000',
        "context_p95": f'histogram_quantile(0.95, sum(rate(openzync_context_latency_seconds_bucket{{org_id="{org_uuid}"}}[5m])) by (le)) * 1000',
        "context_p99": f'histogram_quantile(0.99, sum(rate(openzync_context_latency_seconds_bucket{{org_id="{org_uuid}"}}[5m])) by (le)) * 1000',
        "graph_p50": f'histogram_quantile(0.50, sum(rate(openzync_graph_search_latency_seconds_bucket{{org_id="{org_uuid}"}}[5m])) by (le)) * 1000',
        "graph_p95": f'histogram_quantile(0.95, sum(rate(openzync_graph_search_latency_seconds_bucket{{org_id="{org_uuid}"}}[5m])) by (le)) * 1000',
        "graph_p99": f'histogram_quantile(0.99, sum(rate(openzync_graph_search_latency_seconds_bucket{{org_id="{org_uuid}"}}[5m])) by (le)) * 1000',
    }
    rows = []
    for name, promql in queries.items():
        val = await _prom_instant(promql)
        rows.append([name, round(val, 1)])
    return _result(
        "latency_percentiles", True, ["metric", "value_ms"], rows, {}
    )


async def _queue_depth_over_time(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    # Was Prometheus, now DB: pending enrichments (enrichment_status != 63) per day
    conditions = [
        Episode.organization_id == org_uuid,
        Episode.is_deleted.is_(False),
        Episode.enrichment_status != 63,
        Episode.created_at >= func.now() - text(f"interval '{days} days'"),
    ]
    if project_id:
        conditions.append(Episode.project_id == UUID(project_id))
    stmt = (
        select(
            func.date_trunc("day", Episode.created_at).label("date"),
            func.count(Episode.id).label("count"),
        )
        .select_from(Episode)
        .where(*conditions)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(stmt)
    rows = [[str(r.date), r.count] for r in result]
    return _result("queue_depth_over_time", True, ["date", "count"], rows, {"days": days})


async def _context_retrieval_rate(
    db: AsyncSession, org_uuid: UUID, days: int, limit: int, project_id: str | None
) -> dict:
    promql = f'sum(rate(openzync_context_latency_seconds_count{{org_id="{org_uuid}"}}[5m]))'
    rows = await _prom_range(promql, days)
    return _result(
        "context_retrieval_rate", True, ["timestamp", "rate"], rows, {"days": days}
    )


# ── Dispatch ──────────────────────────────────────────────────────────────

_QUERY_HANDLERS = {
    "episodes_per_day": _episodes_per_day,
    "messages_per_day": _messages_per_day,
    "users_per_day": _users_per_day,
    "entities_per_day": _entities_per_day,
    "facts_per_day": _facts_per_day,
    "enrichment_progress": _enrichment_progress,
    "top_projects_by_episodes": _top_projects_by_episodes,
    "top_users_by_messages": _top_users_by_messages,
    "error_rate_by_day": _error_rate_by_day,
    "latency_percentiles": _latency_percentiles,
    "queue_depth_over_time": _queue_depth_over_time,
    "context_retrieval_rate": _context_retrieval_rate,
}

AVAILABLE_QUERY_LIST: list[dict] = [
    {"name": "episodes_per_day", "description": "Daily episode count", "category": "ingestion", "org_scoped": True, "params": ["days"]},
    {"name": "messages_per_day", "description": "Daily message count", "category": "ingestion", "org_scoped": True, "params": ["days"]},
    {"name": "users_per_day", "description": "Daily user creation", "category": "users", "org_scoped": True, "params": ["days"]},
    {"name": "entities_per_day", "description": "Daily graph entity creation", "category": "graph", "org_scoped": True, "params": ["days"]},
    {"name": "facts_per_day", "description": "Daily fact extraction", "category": "graph", "org_scoped": True, "params": ["days"]},
    {"name": "enrichment_progress", "description": "Enrichment status breakdown", "category": "ingestion", "org_scoped": True, "params": []},
    {"name": "top_projects_by_episodes", "description": "Projects ranked by episode count", "category": "projects", "org_scoped": True, "params": ["limit"]},
    {"name": "top_users_by_messages", "description": "Users ranked by message count", "category": "users", "org_scoped": True, "params": ["limit"]},
    {"name": "error_rate_by_day", "description": "Daily 5xx error counts", "category": "performance", "org_scoped": True, "params": ["days"]},
    {"name": "latency_percentiles", "description": "Current p50/p95/p99 latency", "category": "performance", "org_scoped": True, "params": []},
    {"name": "queue_depth_over_time", "description": "Pending enrichments per day (org backlog)", "category": "performance", "org_scoped": True, "params": ["days"]},
    {"name": "context_retrieval_rate", "description": "Context assembly request rate", "category": "performance", "org_scoped": True, "params": ["days"]},
]


@router.get("/queries", summary="List available metric queries")
async def list_queries() -> dict:
    """Return the list of available predefined metric queries."""
    return {"queries": AVAILABLE_QUERY_LIST}


@router.get(
    "/query",
    summary="Run a predefined org-scoped metric query",
    description="Runs a predefined query scoped to the authenticated organization.",
)
async def run_org_query(
    query: str = Query(..., description="Query name (see /metrics/queries)"),
    days: int = Query(default=7, ge=1, le=365, description="Look-back window in days"),
    limit: int = Query(default=20, ge=1, le=100, description="Max results"),
    project_id: str | None = Query(default=None, description="Optional project UUID filter"),
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_permission("members:read")),
    prom: MetricsService = Depends(_get_metrics_service),
) -> dict:
    """Run a predefined metric query scoped to the authenticated org.

    All queries are org-scoped (Prometheus handlers filter by org_id label;
    queue_depth_over_time is DB-backed pending enrichments per day).

    Raises:
        HTTPException: 422 if the query name is unknown.
    """
    handler = _QUERY_HANDLERS.get(query)
    if not handler:
        available = [q["name"] for q in AVAILABLE_QUERY_LIST]
        raise HTTPException(
            status_code=422,
            detail=f"Unknown query. Available: {available}",
        )
    return await handler(db, UUID(org_id), days, limit, project_id)


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
    _org_id: str = Depends(require_permission("members:read")),
) -> dict:
    """Get Prometheus scrape target health.

    Returns:
        Dict with ``targets`` list and ``status``.
    """
    import httpx

    base_url = get_settings().PROMETHEUS_URL.rstrip("/")
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
