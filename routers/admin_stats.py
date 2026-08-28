"""Admin dashboard statistics endpoints — HTTP adapter layer only.

Provides aggregate data for the dashboard frontend:
- Organization-level counts (episodes, sessions, facts, extractions,
  observations, classifications)
- Daily usage trends (episodes, sessions, facts, extractions,
  observations, classifications, nodes, edges per day)

All endpoints require JWT authentication (dashboard session).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_permission
from dependencies.db import get_db
from models.dialog_classification import DialogClassification
from models.episode import Episode
from models.fact import Fact
from models.graph_entity import GraphEntity
from models.graph_observation import GraphObservation
from models.project import Project
from models.session import Session
from models.structured_extraction import StructuredExtraction
from schemas.admin_stats import OrgStatsResponse, UsageStatsResponse

router = APIRouter(
    prefix="/v1/admin/stats",
    tags=["Admin - Stats"],
)


@router.get(
    "/org",
    response_model=OrgStatsResponse,
    summary="Organization aggregate statistics",
    description=(
        "Returns aggregate counts for the authenticated organization: "
        "total episodes, sessions, facts, extractions, observations, "
        "and classifications. Requires a JWT dashboard token."
    ),
)
async def get_org_stats(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_permission("members:read")),
) -> OrgStatsResponse:
    """Get aggregate statistics for the authenticated organization.

    Args:
        db: Async database session.
        org_id: Authenticated organization ID (from JWT or API key).

    Returns:
        OrgStatsResponse with aggregate counts.
    """
    org_uuid = UUID(org_id)

    episode_count = await _count_episodes(db, org_uuid)
    session_count = await _count_sessions(db, org_uuid)
    fact_count = await _count_facts(db, org_uuid)
    extraction_count = await _count_extractions(db, org_uuid)
    observation_count = await _count_observations(db, org_uuid)
    classification_count = await _count_classifications(db, org_uuid)

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
        "observations, classifications, nodes, and edges for the last N days. "
        "Useful for dashboard charts. Requires a JWT dashboard token."
    ),
)
async def get_usage_stats(
    days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Number of days to look back.",
    ),
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_permission("members:read")),
) -> list[UsageStatsResponse]:
    """Get daily usage statistics for the organization.

    Args:
        days: Look-back window in days (default 30, max 365).
        db: Async database session.
        org_id: Authenticated organization ID.

    Returns:
        List of daily usage data points, newest first.
    """
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
    episode_stmt = (
        select(
            func.date_trunc("day", Episode.created_at).label("date"),
            func.count(Episode.id).label("count"),
        )
        .select_from(Episode)
        .where(
            Episode.organization_id == org_uuid,
            Episode.is_deleted.is_(False),
            Episode.created_at >= func.now() - text(f"interval '{days} days'"),
        )
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(episode_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["episode_count"] = row.count

    # Daily session counts
    session_stmt = (
        select(
            func.date_trunc("day", Session.created_at).label("date"),
            func.count(Session.id).label("count"),
        )
        .select_from(Session)
        .where(
            Session.organization_id == org_uuid,
            Session.is_deleted.is_(False),
            Session.created_at >= func.now() - text(f"interval '{days} days'"),
        )
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(session_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["session_count"] = row.count

    # Daily fact counts
    fact_stmt = (
        select(
            func.date_trunc("day", Fact.created_at).label("date"),
            func.count(Fact.id).label("count"),
        )
        .select_from(Fact)
        .where(
            Fact.organization_id == org_uuid,
            Fact.created_at >= func.now() - text(f"interval '{days} days'"),
        )
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(fact_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["fact_count"] = row.count

    # Daily extraction counts — scoped via Project join
    extraction_stmt = (
        select(
            func.date_trunc("day", StructuredExtraction.created_at).label("date"),
            func.count(StructuredExtraction.id).label("count"),
        )
        .select_from(StructuredExtraction)
        .join(Project, StructuredExtraction.project_id == Project.id)
        .where(
            Project.organization_id == org_uuid,
            StructuredExtraction.created_at
            >= func.now() - text(f"interval '{days} days'"),
        )
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(extraction_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["extraction_count"] = row.count

    # Daily observation counts
    observation_stmt = (
        select(
            func.date_trunc("day", GraphObservation.created_at).label("date"),
            func.count(GraphObservation.id).label("count"),
        )
        .select_from(GraphObservation)
        .where(
            GraphObservation.organization_id == org_uuid,
            GraphObservation.created_at >= func.now() - text(f"interval '{days} days'"),
        )
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(observation_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["observation_count"] = row.count

    # Daily classification counts
    classification_stmt = (
        select(
            func.date_trunc("day", DialogClassification.created_at).label("date"),
            func.count(DialogClassification.id).label("count"),
        )
        .select_from(DialogClassification)
        .where(
            DialogClassification.organization_id == org_uuid,
            DialogClassification.created_at
            >= func.now() - text(f"interval '{days} days'"),
        )
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(classification_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["classification_count"] = row.count

    # Daily node counts (graph entities)
    node_stmt = (
        select(
            func.date_trunc("day", GraphEntity.created_at).label("date"),
            func.count(GraphEntity.id).label("count"),
        )
        .select_from(GraphEntity)
        .where(
            GraphEntity.organization_id == org_uuid,
            GraphEntity.created_at >= func.now() - text(f"interval '{days} days'"),
        )
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )
    result = await db.execute(node_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        _ensure(date_str)["node_count"] = row.count

    # Daily edge counts (graph_relationships via raw SQL — no ORM model)
    edge_stmt = text(  # noqa: S608
        f"SELECT date_trunc('day', created_at) AS date, COUNT(id) AS count "
        f"FROM graph_relationships "
        f"WHERE organization_id = :org_id "
        f"AND created_at >= NOW() - interval '{days} days' "
        f"GROUP BY date ORDER BY date DESC"
    )
    result = await db.execute(edge_stmt, {"org_id": str(org_uuid)})
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


async def _count_episodes(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(Episode.id)).where(
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
        )
    )
    return result.scalar() or 0


async def _count_sessions(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(Session.id)).where(
            Session.organization_id == org_id,
            Session.is_deleted.is_(False),
        )
    )
    return result.scalar() or 0


async def _count_facts(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(Fact.id)).where(
            Fact.organization_id == org_id,
        )
    )
    return result.scalar() or 0


async def _count_extractions(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(StructuredExtraction.id))
        .select_from(StructuredExtraction)
        .join(Project, StructuredExtraction.project_id == Project.id)
        .where(Project.organization_id == org_id)
    )
    return result.scalar() or 0


async def _count_observations(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(GraphObservation.id)).where(
            GraphObservation.organization_id == org_id,
        )
    )
    return result.scalar() or 0


async def _count_classifications(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(DialogClassification.id)).where(
            DialogClassification.organization_id == org_id,
        )
    )
    return result.scalar() or 0
