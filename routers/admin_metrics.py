"""Admin metrics endpoints — HTTP adapter layer only.

Provides aggregated metrics for the admin panel frontend, combining
DB-sourced counts with Prometheus-backed latency/error metrics.

Endpoints:
    GET /metrics/summary   — Aggregated RED + DB metrics for the admin panel
    GET /metrics/targets   — List Prometheus scrape targets and health
    GET /metrics/batch     — Run all 12 predefined queries at once (partial OK)

    ``GET /metrics/batch`` takes EITHER ``days`` (one of 7/30/90, window is
    ``[now-days, now)``) OR ``from``+``to`` (``YYYY-MM-DD``, window is
    ``[from 00:00 UTC, to 00:00 UTC + 1 day)``). The styles are mutually
    exclusive — mixing them, omitting both, or ``from >= to`` is a 422.
    Every handler filters on the explicit ``[start, end)`` window.

The single-query surface (``GET /metrics/queries``, ``GET /metrics/query``)
was removed — the dashboard renders every predefined query in one batched
call instead of fanning out N single-query requests.

All endpoints require API key or JWT authentication.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from dependencies.auth import require_permission
from dependencies.db import get_db
from dependencies.org_config import get_org_config
from schemas.admin_metrics import MetricsSummaryResponse
from schemas.organization_config import OrgConfigBase
from schemas.sorting import MonitorTargetSortBy, SortDir
from services.admin_metrics_queries_service import AdminMetricsQueriesService
from services.admin_metrics_service import AdminMetricsService
from services.metrics_service import MetricsService

router = APIRouter(
    prefix="/metrics",
    tags=["Admin - Metrics"],
)


# ── Dependencies ──────────────────────────────────────────────────────────────


def _get_metrics_service() -> MetricsService:
    """Dependency factory for ``MetricsService``."""
    return MetricsService(prometheus_url=get_settings().PROMETHEUS_URL)


def _get_admin_metrics_service(
    db: AsyncSession = Depends(get_db),
    prom: MetricsService = Depends(_get_metrics_service),
) -> AdminMetricsService:
    """Dependency factory for ``AdminMetricsService``."""
    return AdminMetricsService(db=db, prom=prom)


def _get_admin_metrics_queries_service(
    db: AsyncSession = Depends(get_db),
) -> AdminMetricsQueriesService:
    """Dependency factory for ``AdminMetricsQueriesService``."""
    return AdminMetricsQueriesService(db=db, prom_client=_get_prom_client())


# ── Shared Prometheus client ──────────────────────────────────────────────────
# ⚠️ One module-level client reused by every Prom helper/targets call.
# Per-call ``async with httpx.AsyncClient()`` opens a new connection pool per
# query — under /metrics/batch (12 handlers, 4 hitting Prom) that is 4 pools
# per request. Per-request timeouts still override the default below.

_prom_client: httpx.AsyncClient | None = None


def _get_prom_client() -> httpx.AsyncClient:
    """Return the shared Prometheus HTTP client (lazy singleton)."""
    global _prom_client
    if _prom_client is None:
        _prom_client = httpx.AsyncClient(timeout=10.0)
    return _prom_client


async def close_prom_client() -> None:
    """Close the shared Prometheus HTTP client (lifespan shutdown).

    Safe to call when the client was never created — a no-op in that case.
    """
    global _prom_client
    if _prom_client is not None:
        await _prom_client.aclose()
        _prom_client = None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get(
    "/summary",
    response_model=MetricsSummaryResponse,
    summary="Aggregated admin dashboard metrics",
    description=(
        "Returns a combined view of DB counts (episodes, users, graphs) and "
        "Prometheus-backed performance metrics (latency, error rate, request "
        'rate).  The ``status`` field is ``"degraded"`` if Prometheus is '
        "unreachable — DB counts are still returned."
    ),
)
async def get_metrics_summary(
    request: Request,
    days: int | None = Query(
        default=None,
        description="Look-back window — one of 7/30/90. "
        "Mutually exclusive with from/to. "
        "Omit all window params for the last 24h.",
    ),
    from_date: str | None = Query(
        default=None,
        alias="from",
        description="Start date YYYY-MM-DD inclusive (00:00 UTC). "
        "Requires to; mutually exclusive with days.",
    ),
    to_date: str | None = Query(
        default=None,
        alias="to",
        description="End date YYYY-MM-DD inclusive "
        "(window ends to 00:00 UTC + 1 day). "
        "Requires from; mutually exclusive with days.",
    ),
    org_id: str = Depends(require_permission("members:read")),
    org_config: OrgConfigBase = Depends(get_org_config),
    service: AdminMetricsService = Depends(_get_admin_metrics_service),
) -> MetricsSummaryResponse:
    """Get aggregated metrics for the admin dashboard.

    Merges DB counts and Prometheus metrics into a single response.
    DB counts are scoped to the authenticated organization.
    Only the Prometheus range time-series honor the window; DB totals
    and instant queries stay snapshot semantics.
    """
    if days is not None and (from_date is not None or to_date is not None):
        raise HTTPException(
            status_code=422,
            detail="Pass either days or from/to, not both.",
        )
    if days is not None:
        if days not in (7, 30, 90):
            raise HTTPException(
                status_code=422,
                detail="days must be one of 7, 30, 90.",
            )
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
    elif from_date is not None or to_date is not None:
        if from_date is None or to_date is None:
            raise HTTPException(
                status_code=422,
                detail="Pass either days or both from and to.",
            )
        try:
            from_day = date.fromisoformat(from_date)
            to_day = date.fromisoformat(to_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="from/to must be YYYY-MM-DD.",
            ) from exc
        if from_day >= to_day:
            raise HTTPException(
                status_code=422,
                detail="from must be earlier than to.",
            )
        start = datetime.combine(from_day, dtime.min, tzinfo=UTC)
        end = datetime.combine(to_day, dtime.min, tzinfo=UTC) + timedelta(days=1)
    else:
        end = datetime.now(UTC)
        start = end - timedelta(hours=24)
    return await service.get_summary(request, org_config, UUID(org_id), start, end)


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
    sort_by: MonitorTargetSortBy | None = Query(
        default=None,
        description="Sort key — ``name`` (job), ``created_at`` "
        "(last_scrape), ``type`` (instance), ``status`` (health).",
    ),
    sort_dir: SortDir = Query(
        default="asc",
        description="Sort direction (default asc).",
    ),
) -> dict:
    """Get Prometheus scrape target health.

    Default order is the Prometheus response order (preserved); sort keys
    map to target fields (``name``→job, ``created_at``→last_scrape,
    ``type``→instance, ``status``→health).

    Returns:
        Dict with ``targets`` list and ``status``.
    """
    base_url = get_settings().PROMETHEUS_URL.rstrip("/")
    try:
        resp = await _get_prom_client().get(f"{base_url}/api/v1/targets", timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Prometheus targets unavailable: {exc}",
        ) from exc

    targets = []
    for t in data.get("data", {}).get("activeTargets", []):
        targets.append(
            {
                "job": t.get("labels", {}).get("job", ""),
                "instance": t.get("labels", {}).get("instance", ""),
                "health": t.get("health", "unknown"),
                "last_scrape": t.get("lastScrape", ""),
                "last_error": t.get("lastError", "") or None,
            }
        )

    if sort_by is not None:
        # note: explicit key selection — no getattr on raw input.
        def _target_key(target: dict) -> tuple[str, str]:
            if sort_by == "name":
                return (target["job"], target["instance"])
            if sort_by == "created_at":
                return (target["last_scrape"], target["job"])
            if sort_by == "type":
                return (target["instance"], target["job"])
            return (target["health"], target["job"])

        targets.sort(key=_target_key, reverse=(sort_dir == "desc"))

    return {"status": "ok", "targets": targets}


@router.get(
    "/batch",
    summary="Run all predefined metric queries at once",
    description=(
        "Runs all 12 predefined org-scoped queries concurrently over an "
        "explicit ``[start, end)`` window and returns a partial-OK envelope: "
        "per-query failures are reported in ``errors`` with HTTP 200, and "
        "the failing ``results`` entry is omitted. Pass EITHER ``days`` "
        "(one of 7/30/90, window ``[now-days, now)``) OR ``from``+``to`` "
        "(``YYYY-MM-DD``, window ``[from 00:00 UTC, to 00:00 UTC + 1 day)``). "
        "Per-query timeouts are 5s (DB) / 8s (Prometheus) with a "
        "~15s overall budget."
    ),
)
async def get_metrics_batch(
    request: Request,
    days: int | None = Query(
        default=None,
        description="Look-back window — one of 7/30/90. "
        "Mutually exclusive with from/to.",
    ),
    from_date: str | None = Query(
        default=None,
        alias="from",
        description="Start date YYYY-MM-DD inclusive (00:00 UTC). "
        "Requires to; mutually exclusive with days.",
    ),
    to_date: str | None = Query(
        default=None,
        alias="to",
        description="End date YYYY-MM-DD inclusive "
        "(window ends to 00:00 UTC + 1 day). "
        "Requires from; mutually exclusive with days.",
    ),
    limit: int = Query(default=20, ge=1, le=50, description="Max rows for rankings"),
    include_archived: bool = Query(
        default=False,
        description="Include archived projects (top_projects_by_episodes only).",
    ),
    project_id: UUID | None = Query(
        default=None, description="Optional project filter"
    ),
    org_id: str = Depends(require_permission("members:read")),
    org_config: OrgConfigBase = Depends(get_org_config),
    service: AdminMetricsQueriesService = Depends(_get_admin_metrics_queries_service),
) -> dict:
    """Run every predefined query concurrently; partial failures stay HTTP 200.

    Returns:
        ``{"results": {name: query-dict}, "errors": [...], "meta": {...}}``
        where each ``results`` value has ``query/org_scoped/columns/rows/
        total/parameters`` and each ``errors`` entry has
        ``query/code/message``. ``meta`` echoes the window as either
        ``days`` or ``from``+``to``, plus ``limit``, ``duration_ms``,
        and ``partial``.
    """
    if days is not None and (from_date is not None or to_date is not None):
        raise HTTPException(
            status_code=422,
            detail="Pass either days or from/to, not both.",
        )
    if days is not None:
        if days not in (7, 30, 90):
            raise HTTPException(
                status_code=422,
                detail="days must be one of 7, 30, 90.",
            )
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        window_meta: dict = {"days": days}
    else:
        if from_date is None or to_date is None:
            raise HTTPException(
                status_code=422,
                detail="Pass either days or both from and to.",
            )
        try:
            from_day = date.fromisoformat(from_date)
            to_day = date.fromisoformat(to_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="from/to must be YYYY-MM-DD.",
            ) from exc
        if from_day >= to_day:
            raise HTTPException(
                status_code=422,
                detail="from must be earlier than to.",
            )
        start = datetime.combine(from_day, dtime.min, tzinfo=UTC)
        end = datetime.combine(to_day, dtime.min, tzinfo=UTC) + timedelta(days=1)
        window_meta = {"from": from_date, "to": to_date}
    result = await service.run_batch(
        request,
        org_config,
        UUID(org_id),
        start,
        end,
        limit,
        project_id,
        include_archived,
    )
    result["meta"] = {**window_meta, **result["meta"]}
    return result
