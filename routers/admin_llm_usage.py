"""Admin LLM usage read API — HTTP adapter layer only.

Exposes metered inference rows for the authenticated organization,
windowed by days/from/to with optional project/worker/model filters,
plus aggregates over the same window. No SQL here — all queries go
through ``services.usage_service``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.sorting import SortSpec
from dependencies.auth import require_permission
from dependencies.db import get_db
from schemas.llm_usage import (
    LLMUsageListResponse,
    LLMUsageResponse,
    UsageSummaryResponse,
)
from services.usage_service import list_usage, summarize_usage

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/v1/admin/llm-usage",
    tags=["Admin - LLM Usage"],
)

LLMUsageSortBy = Literal["created_at", "duration_ms", "model", "total_tokens"]
"""Whitelisted sort keys for the usage endpoint."""


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
    "",
    response_model=LLMUsageListResponse,
    summary="List metered LLM usage",
    description=(
        "Returns paginated metering rows for the authenticated organization "
        "plus aggregates over the same window. Windowed by days/from/to "
        "with optional project_id, worker, and model filters. "
        "Default window is last 30 days."
    ),
)
async def list_llm_usage(
    days: int | None = Query(default=None, ge=1, le=365),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    project_id: UUID | None = Query(default=None),
    worker: str | None = Query(default=None),
    model: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort_by: LLMUsageSortBy | None = Query(default=None),
    sort_dir: Literal["asc", "desc"] = Query(default="desc"),
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission("members:read")),
) -> LLMUsageListResponse:
    """List metering rows with a window summary for the org.

    Args:
        days: Look-back window in days (1-365). Ignored if from/to provided.
        from_date: Inclusive start date (YYYY-MM-DD).
        to_date: Inclusive end date (YYYY-MM-DD).
        project_id: Optional project scope filter.
        worker: Optional worker label filter (e.g. ``enrich_episode``).
        model: Optional model identifier filter.
        limit: Max rows per page (1-500).
        offset: Pagination offset.
        sort_by: Sort key (``created_at``, ``duration_ms``, ``model``,
            ``total_tokens``).
        sort_dir: Sort direction.
        db: Async database session.
        user_id: Authenticated organization ID (from JWT or API key).

    Returns:
        ``LLMUsageListResponse`` with rows, total, and summary.
    """
    start, end_exclusive = _resolve_window(days, from_date, to_date)
    org_uuid = UUID(user_id)
    sort = SortSpec(sort_by=sort_by, sort_dir=sort_dir)

    rows, total = await list_usage(
        db,
        user_id=org_uuid,
        start=start,
        end=end_exclusive,
        project_id=project_id,
        worker=worker,
        model=model,
        limit=limit,
        offset=offset,
        sort=sort,
    )
    summary = await summarize_usage(
        db,
        user_id=org_uuid,
        start=start,
        end=end_exclusive,
        project_id=project_id,
        worker=worker,
        model=model,
    )
    return LLMUsageListResponse(
        data=[LLMUsageResponse.model_validate(row) for row in rows],
        total=total,
        summary=UsageSummaryResponse(
            calls=summary.calls,
            prompt_tokens=summary.prompt_tokens,
            completion_tokens=summary.completion_tokens,
            reasoning_tokens=summary.reasoning_tokens,
            total_tokens=summary.total_tokens,
            avg_duration_ms=summary.avg_duration_ms,
        ),
    )
