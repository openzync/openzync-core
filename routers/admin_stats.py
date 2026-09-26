"""Admin dashboard statistics endpoints — HTTP adapter layer only.

Provides aggregate data for the dashboard frontend:
- Organization-level counts (episodes, sessions, facts, extractions,
  observations, classifications) — windowed by days/from/to and
  optional project_id.
- Daily usage trends (episodes, sessions, facts, extractions,
  observations, classifications, nodes, edges per day) — same windowing.

All endpoints require JWT authentication (dashboard session).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_permission
from dependencies.db import get_db
from dependencies.org_config import get_org_config
from models.dialog_classification import DialogClassification
from models.episode import Episode
from models.fact import Fact
from models.graph_observation import GraphObservation
from models.project import Project
from models.session import Session
from models.structured_extraction import StructuredExtraction
from packages.graph_backend.interface import GraphBackend
from schemas.admin_stats import OrgStatsResponse, UsageStatsResponse
from schemas.organization_config import OrgConfigBase
from services.graph_stats_service import GraphStatsService

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/v1/admin/stats",
    tags=["Admin - Stats"],
)


async def _resolve_graph_backend(
    request: Request,
    db: AsyncSession,
    org_config: OrgConfigBase,
    org_id: UUID,
) -> GraphBackend | None:
    """Resolve the org-configured graph backend, fail-soft to ``None``.

    Same pattern as the ``context``/``search`` routers.  Admin usage stats
    degrade (node counts read as zero) instead of failing when the graph
    is unavailable — every degraded path logs.
    """
    dispatcher = getattr(request.app.state, "graph_backend_dispatcher", None)
    if dispatcher is None:
        logger.warning("admin_stats.no_graph_dispatcher")
        return None
    surreal = None
    if org_config.graph_backend == "surrealdb":
        pool = getattr(request.app.state, "surreal_connection_pool", None)
        if pool is not None:
            try:
                surreal = await pool.get_or_create(org_id, org_config)
            except Exception as exc:
                logger.warning(
                    "admin_stats.surreal_connection_failed",
                    error=str(exc),
                )
                return None
    try:
        return dispatcher.resolve_and_create(
            org_config,
            db,
            surreal=surreal,
            falkordb_client=getattr(request.app.state, "falkordb_client", None),
        )
    except Exception as exc:
        # Unavailable, unknown, or retired (postgres → GoneError) backends
        # all degrade to zeros here — never fail the usage endpoint.
        logger.warning("admin_stats.graph_backend_unresolved", error=str(exc))
        return None


def _resolve_window(
    days: int | None,
    from_date: date | None,
    to_date: date | None,
) -> tuple[datetime, datetime | None]:
    """Resolve query window into (start, end_exclusive).

    Rules:
    - If both from and to provided: use custom range, validate from<=to,
      build start at 00:00 UTC of from_date and end_exclusive at 00:00 UTC
      of to_date+1d. Ignore days.
    - Elif days is not None: start = now - days, no end filter.
    - Else: default days=30.

    Raises 422 if only one of from/to is provided or if from > to.
    """
    if from_date is not None or to_date is not None:
        if from_date is None or to_date is None:
            raise HTTPException(
                status_code=422,
                detail="Both `from` and `to` must be provided together",
            )
        if from_date > to_date:
            raise HTTPException(
                status_code=422,
                detail="`from` must be <= `to`",
            )
        start = datetime.combine(from_date, time.min, tzinfo=UTC)
        end_exclusive = datetime.combine(
            to_date + timedelta(days=1), time.min, tzinfo=UTC
        )
        return start, end_exclusive
    if days is not None:
        start = datetime.now(UTC) - timedelta(days=days)
        return start, None
    start = datetime.now(UTC) - timedelta(days=30)
    return start, None


@router.get(
    "/org",
    response_model=OrgStatsResponse,
    summary="Organization aggregate statistics",
    description=(
        "Returns aggregate counts for the authenticated organization: "
        "total episodes, sessions, facts, extractions, observations, "
        "and classifications. Windowed by days/from/to and optional "
        "project_id. Default window is last 30 days (BREAKING: previously all-time)."
    ),
)
async def get_org_stats(
    days: int | None = Query(default=None, ge=1, le=365),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    project_id: UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_permission("members:read")),
) -> OrgStatsResponse:
    """Get aggregate statistics for the authenticated organization.

    Windowed by days/from/to and optional project_id. Default window is
    last 30 days.

    Args:
        days: Look-back window in days (1-365). Ignored if from/to provided.
        from_date: Inclusive start date (YYYY-MM-DD).
        to_date: Inclusive end date (YYYY-MM-DD).
        project_id: Optional project scope — when provided, all counts are
            filtered to that project (AND with organization_id for RLS).
        db: Async database session.
        org_id: Authenticated organization ID (from JWT or API key).

    Returns:
        OrgStatsResponse with windowed aggregate counts.
    """
    start, end_exclusive = _resolve_window(days, from_date, to_date)
    org_uuid = UUID(org_id)

    episode_count = await _count_episodes(
        db, org_uuid, start, end_exclusive, project_id
    )
    session_count = await _count_sessions(
        db, org_uuid, start, end_exclusive, project_id
    )
    fact_count = await _count_facts(db, org_uuid, start, end_exclusive, project_id)
    extraction_count = await _count_extractions(
        db, org_uuid, start, end_exclusive, project_id
    )
    observation_count = await _count_observations(
        db, org_uuid, start, end_exclusive, project_id
    )
    classification_count = await _count_classifications(
        db, org_uuid, start, end_exclusive, project_id
    )

    return OrgStatsResponse(
        organization_id=org_uuid,
        total_episodes=episode_count,
        total_sessions=session_count,
        total_facts=fact_count,
        total_extractions=extraction_count,
        total_observations=observation_count,
        total_classifications=classification_count,
    )


@router.get(
    "/usage",
    response_model=list[UsageStatsResponse],
    summary="Daily usage trends",
    description=(
        "Returns daily counts for episodes, sessions, facts, extractions, "
        "observations, classifications, nodes, and edges. Windowed by "
        "days/from/to and optional project_id. Default window is last 30 days."
    ),
)
async def get_usage_stats(
    request: Request,
    days: int | None = Query(default=None, ge=1, le=365),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    project_id: UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_permission("members:read")),
    org_config: OrgConfigBase = Depends(get_org_config),
) -> list[UsageStatsResponse]:
    """Get daily usage statistics for the organization.

    Windowed by days/from/to and optional project_id. Default window is
    last 30 days.

    Args:
        request: FastAPI request (graph backend dispatcher + pools).
        days: Look-back window in days (default 30 when no window provided, max 365).
            Ignored if from/to provided.
        from_date: Inclusive start date (YYYY-MM-DD).
        to_date: Inclusive end date (YYYY-MM-DD).
        project_id: Optional project scope.
        db: Async database session.
        org_id: Authenticated organization ID.
        org_config: Org config (graph backend selection).

    Returns:
        List of daily usage data points, newest first.
    """
    start, end_exclusive = _resolve_window(days, from_date, to_date)
    org_uuid = UUID(org_id)

    daily_counts: dict[str, dict[str, int]] = {}

    def _ensure(date_str: str) -> dict[str, int]:
        return daily_counts.setdefault(
            date_str,
            {
                "episode_count": 0,
                "session_count": 0,
                "fact_count": 0,
                "extraction_count": 0,
                "observation_count": 0,
                "classification_count": 0,
                "node_count": 0,
                "edge_count": 0,
            },
        )

    # Daily episode counts
    ep_conds = [
        Episode.organization_id == org_uuid,
        Episode.is_deleted.is_(False),
        Episode.created_at >= start,
    ]
    if end_exclusive is not None:
        ep_conds.append(Episode.created_at < end_exclusive)
    if project_id is not None:
        ep_conds.append(Episode.project_id == project_id)
    episode_stmt = (
        select(
            func.date_trunc("day", Episode.created_at).label("date"),
            func.count(Episode.id).label("count"),
        )
        .select_from(Episode)
        .where(*ep_conds)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(episode_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["episode_count"] = row.count

    # Daily session counts
    sess_conds = [
        Session.organization_id == org_uuid,
        Session.is_deleted.is_(False),
        Session.created_at >= start,
    ]
    if end_exclusive is not None:
        sess_conds.append(Session.created_at < end_exclusive)
    if project_id is not None:
        sess_conds.append(Session.project_id == project_id)
    session_stmt = (
        select(
            func.date_trunc("day", Session.created_at).label("date"),
            func.count(Session.id).label("count"),
        )
        .select_from(Session)
        .where(*sess_conds)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(session_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["session_count"] = row.count

    # Daily fact counts
    fact_conds = [
        Fact.organization_id == org_uuid,
        Fact.created_at >= start,
    ]
    if end_exclusive is not None:
        fact_conds.append(Fact.created_at < end_exclusive)
    if project_id is not None:
        fact_conds.append(Fact.project_id == project_id)
    fact_stmt = (
        select(
            func.date_trunc("day", Fact.created_at).label("date"),
            func.count(Fact.id).label("count"),
        )
        .select_from(Fact)
        .where(*fact_conds)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(fact_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["fact_count"] = row.count

    # Daily extraction counts — scoped via Project join
    extr_conds = [
        Project.organization_id == org_uuid,
        StructuredExtraction.created_at >= start,
    ]
    if end_exclusive is not None:
        extr_conds.append(StructuredExtraction.created_at < end_exclusive)
    if project_id is not None:
        extr_conds.append(StructuredExtraction.project_id == project_id)
    extraction_stmt = (
        select(
            func.date_trunc("day", StructuredExtraction.created_at).label("date"),
            func.count(StructuredExtraction.id).label("count"),
        )
        .select_from(StructuredExtraction)
        .join(Project, StructuredExtraction.project_id == Project.id)
        .where(*extr_conds)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(extraction_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["extraction_count"] = row.count

    # Daily observation counts
    obs_conds = [
        GraphObservation.organization_id == org_uuid,
        GraphObservation.created_at >= start,
    ]
    if end_exclusive is not None:
        obs_conds.append(GraphObservation.created_at < end_exclusive)
    if project_id is not None:
        obs_conds.append(GraphObservation.project_id == project_id)
    observation_stmt = (
        select(
            func.date_trunc("day", GraphObservation.created_at).label("date"),
            func.count(GraphObservation.id).label("count"),
        )
        .select_from(GraphObservation)
        .where(*obs_conds)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(observation_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["observation_count"] = row.count

    # Daily classification counts
    cls_conds = [
        DialogClassification.organization_id == org_uuid,
        DialogClassification.created_at >= start,
    ]
    if end_exclusive is not None:
        cls_conds.append(DialogClassification.created_at < end_exclusive)
    if project_id is not None:
        cls_conds.append(DialogClassification.project_id == project_id)
    classification_stmt = (
        select(
            func.date_trunc("day", DialogClassification.created_at).label("date"),
            func.count(DialogClassification.id).label("count"),
        )
        .select_from(DialogClassification)
        .where(*cls_conds)
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(classification_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["classification_count"] = row.count

    # Daily node counts (graph entities — backend-backed, not the PG stub).
    # The PG graph_entities table is never written by the FalkorDB/SurrealDB
    # paths, so counts come from the backend's get_all_entities bucketed
    # per day in Python.  Response keys are unchanged.
    backend = await _resolve_graph_backend(request, db, org_config, org_uuid)
    stats = GraphStatsService(db, backend)
    node_project_ids = await stats.resolve_project_ids(org_uuid, project_id)
    node_per_day = await stats.entity_counts_per_day(
        org_uuid, node_project_ids, start, end_exclusive
    )
    for node_day, node_count in node_per_day.items():
        _ensure(node_day)["node_count"] = node_count

    # Daily edge counts (graph_relationships via raw SQL — no ORM model)
    edge_params: dict[str, object] = {"org_id": str(org_uuid), "start": start}
    edge_where = "WHERE organization_id = :org_id AND created_at >= :start"
    if end_exclusive is not None:
        edge_where += " AND created_at < :end_exclusive"
        edge_params["end_exclusive"] = end_exclusive
    if project_id is not None:
        edge_where += " AND project_id = :project_id"
        edge_params["project_id"] = str(project_id)
    edge_stmt = text(  # noqa: S608
        f"SELECT date_trunc('day', created_at) AS date, COUNT(id) AS count "  # noqa: S608
        f"FROM graph_relationships "  # noqa: S608
        f"{edge_where} "  # noqa: S608
        f"GROUP BY date ORDER BY date DESC"  # noqa: S608
    )
    result = await db.execute(edge_stmt, edge_params)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["edge_count"] = row.count

    return [
        UsageStatsResponse(
            date=date_str,
            episode_count=counts["episode_count"],
            session_count=counts["session_count"],
            fact_count=counts["fact_count"],
            extraction_count=counts["extraction_count"],
            observation_count=counts["observation_count"],
            classification_count=counts["classification_count"],
            node_count=counts["node_count"],
            edge_count=counts["edge_count"],
        )
        for date_str, counts in sorted(daily_counts.items(), reverse=True)
    ]


# ── Helper functions ─────────────────────────────────────────────────────────


async def _count_episodes(
    db: AsyncSession,
    org_id: UUID,
    start: datetime,
    end_exclusive: datetime | None,
    project_id: UUID | None,
) -> int:
    conds = [
        Episode.organization_id == org_id,
        Episode.is_deleted.is_(False),
        Episode.created_at >= start,
    ]
    if end_exclusive is not None:
        conds.append(Episode.created_at < end_exclusive)
    if project_id is not None:
        conds.append(Episode.project_id == project_id)
    result = await db.execute(select(func.count(Episode.id)).where(*conds))
    return result.scalar() or 0


async def _count_sessions(
    db: AsyncSession,
    org_id: UUID,
    start: datetime,
    end_exclusive: datetime | None,
    project_id: UUID | None,
) -> int:
    conds = [
        Session.organization_id == org_id,
        Session.is_deleted.is_(False),
        Session.created_at >= start,
    ]
    if end_exclusive is not None:
        conds.append(Session.created_at < end_exclusive)
    if project_id is not None:
        conds.append(Session.project_id == project_id)
    result = await db.execute(select(func.count(Session.id)).where(*conds))
    return result.scalar() or 0


async def _count_facts(
    db: AsyncSession,
    org_id: UUID,
    start: datetime,
    end_exclusive: datetime | None,
    project_id: UUID | None,
) -> int:
    conds = [
        Fact.organization_id == org_id,
        Fact.created_at >= start,
    ]
    if end_exclusive is not None:
        conds.append(Fact.created_at < end_exclusive)
    if project_id is not None:
        conds.append(Fact.project_id == project_id)
    result = await db.execute(select(func.count(Fact.id)).where(*conds))
    return result.scalar() or 0


async def _count_extractions(
    db: AsyncSession,
    org_id: UUID,
    start: datetime,
    end_exclusive: datetime | None,
    project_id: UUID | None,
) -> int:
    conds = [
        Project.organization_id == org_id,
        StructuredExtraction.created_at >= start,
    ]
    if end_exclusive is not None:
        conds.append(StructuredExtraction.created_at < end_exclusive)
    if project_id is not None:
        conds.append(StructuredExtraction.project_id == project_id)
    result = await db.execute(
        select(func.count(StructuredExtraction.id))
        .select_from(StructuredExtraction)
        .join(Project, StructuredExtraction.project_id == Project.id)
        .where(*conds)
    )
    return result.scalar() or 0


async def _count_observations(
    db: AsyncSession,
    org_id: UUID,
    start: datetime,
    end_exclusive: datetime | None,
    project_id: UUID | None,
) -> int:
    conds = [
        GraphObservation.organization_id == org_id,
        GraphObservation.created_at >= start,
    ]
    if end_exclusive is not None:
        conds.append(GraphObservation.created_at < end_exclusive)
    if project_id is not None:
        conds.append(GraphObservation.project_id == project_id)
    result = await db.execute(select(func.count(GraphObservation.id)).where(*conds))
    return result.scalar() or 0


async def _count_classifications(
    db: AsyncSession,
    org_id: UUID,
    start: datetime,
    end_exclusive: datetime | None,
    project_id: UUID | None,
) -> int:
    conds = [
        DialogClassification.organization_id == org_id,
        DialogClassification.created_at >= start,
    ]
    if end_exclusive is not None:
        conds.append(DialogClassification.created_at < end_exclusive)
    if project_id is not None:
        conds.append(DialogClassification.project_id == project_id)
    result = await db.execute(select(func.count(DialogClassification.id)).where(*conds))
    return result.scalar() or 0
