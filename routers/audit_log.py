"""Admin audit log router — HTTP adapter layer only.

Provides:
    GET /v1/admin/audit-logs — Paginated, filterable audit log listing.
    GET /v1/admin/audit-logs/actions — Distinct action names for filter dropdowns.

All endpoints require a JWT dashboard session with the ``admin`` role:
the log is user-scoped (IPs, actor IDs, resource IDs, and — with response-body
capture enabled — response bodies that may contain secrets).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_permission
from dependencies.db import get_db
from schemas.audit_log import AuditLogListResponse, AuditLogResponse
from schemas.sorting import AuditLogSortBy, SortDir, SortSpec
from services.audit_log_service import AuditLogService

router = APIRouter(
    prefix="/v1/admin/audit-logs",
    tags=["Admin - Audit Logs"],
)


@router.get(
    "",
    response_model=AuditLogListResponse,
    summary="List audit log entries",
    description=(
        "Returns paginated audit log entries for the authenticated "
        "user scope.  Supports filtering by action, actor, resource, "
        "status code, and date range."
    ),
)
async def list_audit_logs(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission("members:read")),
    action: str | None = Query(None, description="Filter by action (exact match)"),
    actor_id: str | None = Query(None, description="Filter by actor ID"),
    actor_type: str | None = Query(None, description="Filter by actor type (user, api_key, system)"),
    resource_type: str | None = Query(None, description="Filter by resource type"),
    resource_id: str | None = Query(None, description="Filter by resource ID"),
    status_code: int | None = Query(None, description="Filter by HTTP status code"),
    exclude_prefix: str | None = Query(
        None,
        description="Comma-separated action prefixes to exclude (e.g. 'http.,auth.')",
    ),
    created_after: str | None = Query(None, description="Include entries after this ISO 8601 timestamp"),
    created_before: str | None = Query(None, description="Include entries before this ISO 8601 timestamp"),
    limit: int = Query(default=50, ge=1, le=500, description="Max entries per page"),
    offset: int = Query(default=0, ge=0, description="Number of entries to skip"),
    sort_by: AuditLogSortBy | None = Query(
        default=None, description="Sort key (default created_at)."
    ),
    sort_dir: SortDir = Query(
        default="desc", description="Sort direction (default desc)."
    ),
) -> AuditLogListResponse:
    """Get paginated audit log entries for the admin dashboard.

    Permission-gated (``require_permission("members:read")``): only members
    with read access may view the user-scoped audit trail.

    Args:
        db: Database session.
        user_id: Authenticated user ID (admin-gated).
        action: Optional action filter.
        actor_id: Optional actor filter.
        actor_type: Optional actor type filter.
        resource_type: Optional resource type filter.
        resource_id: Optional resource ID filter.
        status_code: Optional HTTP status code filter.
        exclude_prefix: Comma-separated action prefixes to exclude.
        created_after: Optional start date filter.
        created_before: Optional end date filter.
        limit: Page size.
        offset: Pagination offset.
        sort_by: Whitelisted sort key.
        sort_dir: Sort direction.

    Returns:
        Paginated list of audit log entries with total count.
    """
    from uuid import UUID

    service = AuditLogService(db)
    entries, total = await service.query_logs(
        user_id=UUID(user_id),
        action=action,
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type=resource_type,
        resource_id=resource_id,
        status_code=status_code,
        exclude_prefix=exclude_prefix,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
        sort=SortSpec(sort_by=sort_by, sort_dir=sort_dir),
    )

    # One batch for the page — missing users/keys fall back per row,
    # never failing the list.
    display_names = await service.resolve_actor_display_names(entries)

    items = [
        AuditLogResponse(
            id=e.id,

            actor_id=e.actor_id,
            actor_type=e.actor_type,
            actor_display_name=display_names.get(e.id),
            action=e.action,
            resource_type=e.resource_type,
            resource_id=e.resource_id,
            details=e.details or {},
            display_name=e.details.get("display_name") if e.details else None,
            ip_address=e.ip_address,
            status_code=e.details.get("status_code") if e.details else None,
            method=e.details.get("method") if e.details else None,
            path=e.details.get("path") if e.details else None,
            created_at=e.created_at,
        )
        for e in entries
    ]

    return AuditLogListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/actions",
    response_model=list[str],
    summary="List distinct audit log actions",
    description=(
        "Returns distinct audit log action names for the authenticated "
        "user scope, sorted alphabetically — for filter dropdowns."
    ),
)
async def list_audit_log_actions(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_permission("members:read")),
) -> list[str]:
    """Get distinct audit log action names for the admin dashboard.

    Permission-gated (``require_permission("members:read")``): same
    user scope as the list endpoint.

    Args:
        db: Database session.
        user_id: Authenticated user ID (admin-gated).

    Returns:
        Alphabetically sorted distinct action names.
    """
    from uuid import UUID

    service = AuditLogService(db)
    return await service.list_distinct_actions(user_id=UUID(user_id))
