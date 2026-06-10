"""Admin dashboard statistics endpoints — HTTP adapter layer only.

Provides aggregate data for the dashboard frontend:
- Organization-level counts (users, sessions, episodes, facts, messages, keys)
- Daily usage trends (messages and sessions per day)

All endpoints require JWT authentication (dashboard session).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import get_dashboard_user, require_org_id
from dependencies.db import get_db
from models.api_key import ApiKey
from models.episode import Episode
from models.fact import Fact
from models.session import Session
from models.user import User
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
        "total users, sessions, episodes, facts, messages, and API keys. "
        "Requires a JWT dashboard token."
    ),
)
async def get_org_stats(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> OrgStatsResponse:
    """Get aggregate statistics for the authenticated organization.

    Args:
        db: Async database session.
        org_id: Authenticated organization ID (from JWT or API key).
        _user_id: Authenticated dashboard user ID (ensures JWT auth).

    Returns:
        OrgStatsResponse with aggregate counts.
    """
    org_uuid = UUID(org_id)

    # All queries scoped to this organization
    user_count = await _count_users(db, org_uuid)
    session_count = await _count_sessions(db, org_uuid)
    episode_count = await _count_episodes(db, org_uuid)
    fact_count = await _count_facts(db, org_uuid)
    message_count = await _count_messages(db, org_uuid)
    api_key_count = await _count_api_keys(db, org_uuid)

    return OrgStatsResponse(
        organization_id=org_uuid,
        total_users=user_count,
        total_sessions=session_count,
        total_episodes=episode_count,
        total_facts=fact_count,
        total_messages=message_count,
        total_api_keys=api_key_count,
    )


@router.get(
    "/usage",
    response_model=list[UsageStatsResponse],
    summary="Daily usage trends",
    description=(
        "Returns daily message and session counts for the last N days. "
        "Useful for dashboard charts.  Requires a JWT dashboard token."
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
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> list[UsageStatsResponse]:
    """Get daily usage statistics for the organization.

    Args:
        days: Look-back window in days (default 30, max 365).
        db: Async database session.
        org_id: Authenticated organization ID.
        _user_id: Authenticated dashboard user ID.

    Returns:
        List of daily usage data points, newest first.
    """
    org_uuid = UUID(org_id)

    # Daily message counts — Episode has direct organization_id FK
    message_stmt = (
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

    result = await db.execute(message_stmt)
    daily_counts: dict[str, dict[str, int]] = {}
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        daily_counts.setdefault(date_str, {"message_count": 0, "session_count": 0})
        daily_counts[date_str]["message_count"] = row.count

    # Daily session counts
    session_stmt = (
        select(
            func.date_trunc("day", Session.created_at).label("date"),
            func.count(Session.id).label("count"),
        )
        .select_from(Session)
        .join(User, Session.user_id == User.id)
        .where(
            User.organization_id == org_uuid,
            User.is_deleted.is_(False),
            Session.created_at >= func.now() - text(f"interval '{days} days'"),
        )
        .group_by(text("date"))
        .order_by(text("date DESC"))
    )

    result = await db.execute(session_stmt)
    for row in result:
        date_str = str(row.date.date()) if hasattr(row.date, "date") else str(row.date)
        daily_counts.setdefault(date_str, {"message_count": 0, "session_count": 0})
        daily_counts[date_str]["session_count"] = row.count

    return [
        UsageStatsResponse(
            date=date_str,
            message_count=counts["message_count"],
            session_count=counts["session_count"],
        )
        for date_str, counts in sorted(daily_counts.items(), reverse=True)
    ]


# ── Helper functions ─────────────────────────────────────────────────────────


async def _count_users(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(User.id)).where(
            User.organization_id == org_id,
            User.is_deleted.is_(False),
        )
    )
    return result.scalar() or 0


async def _count_sessions(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(Session.id))
        .select_from(Session)
        .join(User, Session.user_id == User.id)
        .where(
            User.organization_id == org_id,
            User.is_deleted.is_(False),
        )
    )
    return result.scalar() or 0


async def _count_episodes(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(Episode.id))
        .select_from(Episode)
        .join(User, Episode.user_id == User.id)
        .where(
            User.organization_id == org_id,
            User.is_deleted.is_(False),
        )
    )
    return result.scalar() or 0


async def _count_facts(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(Fact.id))
        .select_from(Fact)
        .join(User, Fact.user_id == User.id)
        .where(
            User.organization_id == org_id,
            User.is_deleted.is_(False),
        )
    )
    return result.scalar() or 0


async def _count_messages(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(Episode.id)).where(
            Episode.organization_id == org_id,
            Episode.is_deleted.is_(False),
        )
    )
    return result.scalar() or 0


async def _count_api_keys(db: AsyncSession, org_id: UUID) -> int:
    result = await db.execute(
        select(func.count(ApiKey.id)).where(
            ApiKey.organization_id == org_id,
            ApiKey.is_revoked.is_(False),
        )
    )
    return result.scalar() or 0
