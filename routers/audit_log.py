"""Admin audit log router — HTTP adapter layer only.

Provides:
    GET /v1/admin/audit-logs — Paginated, filterable audit log listing.

All endpoints require JWT authentication (dashboard session).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import get_dashboard_user, require_org_id
from dependencies.db import get_db
from schemas.audit_log import AuditLogFilter, AuditLogListResponse, AuditLogResponse
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
        "organization.  Supports filtering by action, actor, resource, "
        "status code, and date range."
    ),
)
async def list_audit_logs(
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
    action: str | None = Query(None, description="Filter by action (exact match)"),
    actor_id: str | None = Query(None, description="Filter by actor ID"),
    actor_type: str | None = Query(None, description="Filter by actor type (user, api_key, system)"),
    resource_type: str | None = Query(None, description="Filter by resource type"),
    resource_id: str | None = Query(None, description="Filter by resource ID"),
    status_code: int | None = Query(None, description="Filter by HTTP status code"),
    created_after: str | None = Query(None, description="Include entries after this ISO 8601 timestamp"),
    created_before: str | None = Query(None, description="Include entries before this ISO 8601 timestamp"),
    limit: int = Query(default=50, ge=1, le=500, description="Max entries per page"),
    offset: int = Query(default=0, ge=0, description="Number of entries to skip"),
) -> AuditLogListResponse:
    """Get paginated audit log entries for the admin dashboard.

    Args:
        db: Database session.
        org_id: Authenticated organization ID (from auth dependency).
        _user_id: Authenticated dashboard user ID (must be JWT).
        action: Optional action filter.
        actor_id: Optional actor filter.
        actor_type: Optional actor type filter.
        resource_type: Optional resource type filter.
        resource_id: Optional resource ID filter.
        status_code: Optional HTTP status code filter.
        created_after: Optional start date filter.
        created_before: Optional end date filter.
        limit: Page size.
        offset: Pagination offset.

    Returns:
        Paginated list of audit log entries with total count.
    """
    from uuid import UUID

    service = AuditLogService(db)
    entries, total = await service.query_logs(
        organization_id=UUID(org_id),
        action=action,
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type=resource_type,
        resource_id=resource_id,
        status_code=status_code,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )

    items = [
        AuditLogResponse(
            id=e.id,
            organization_id=e.organization_id,
            actor_id=e.actor_id,
            actor_type=e.actor_type,
            action=e.action,
            resource_type=e.resource_type,
            resource_id=e.resource_id,
            details=e.details or {},
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
