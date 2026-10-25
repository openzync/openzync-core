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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_permission
from dependencies.db import get_db
from dependencies.org_config import get_org_config
from schemas.admin_stats import OrgStatsResponse, UsageStatsResponse
from schemas.organization_config import OrgConfigBase
from services.admin_stats_service import AdminStatsService

router = APIRouter(
    prefix="/v1/admin/stats",
    tags=["Admin - Stats"],
)


def _get_admin_stats_service(
    db: AsyncSession = Depends(get_db),
) -> AdminStatsService:
    """Dependency factory for ``AdminStatsService``."""
    return AdminStatsService(db=db)


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
    org_id: str = Depends(require_permission("members:read")),
    service: AdminStatsService = Depends(_get_admin_stats_service),
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
        org_id: Authenticated organization ID (from JWT or API key).
        service: Admin stats service.

    Returns:
        OrgStatsResponse with windowed aggregate counts.
    """
    start, end_exclusive = _resolve_window(days, from_date, to_date)
    return await service.get_org_stats(UUID(org_id), start, end_exclusive, project_id)


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
    org_id: str = Depends(require_permission("members:read")),
    org_config: OrgConfigBase = Depends(get_org_config),
    service: AdminStatsService = Depends(_get_admin_stats_service),
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
        org_id: Authenticated organization ID.
        org_config: Org config (graph backend selection).
        service: Admin stats service.

    Returns:
        List of daily usage data points, newest first.
    """
    start, end_exclusive = _resolve_window(days, from_date, to_date)
    return await service.get_usage_stats(
        request, org_config, UUID(org_id), start, end_exclusive, project_id
    )
