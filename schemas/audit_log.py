"""Pydantic schemas for audit log requests and responses."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuditLogResponse(BaseModel):
    """Single audit log entry returned to the frontend."""

    id: UUID = Field(..., description="Audit log entry ID")
    organization_id: UUID | None = Field(None, description="Organization UUID (may be null for unauthenticated actions)")
    actor_id: str | None = Field(None, description="Identifier of the acting entity")
    actor_type: str | None = Field(None, description="Actor category: user, api_key, or system")
    action: str = Field(..., description="The action performed, e.g. session.create")
    resource_type: str = Field(..., description="Type of resource affected")
    resource_id: str | None = Field(None, description="Identifier of the affected resource")
    details: dict = Field(default_factory=dict, description="Action-specific JSON payload")
    ip_address: str | None = Field(None, description="Source IP address")
    status_code: int | None = Field(None, description="HTTP response status code")
    method: str | None = Field(None, description="HTTP method")
    path: str | None = Field(None, description="Request URL path")
    created_at: datetime = Field(..., description="Timestamp of the event")

    model_config = ConfigDict(from_attributes=True)


class AuditLogFilter(BaseModel):
    """Query parameters for filtering audit logs."""

    action: str | None = Field(None, description="Filter by action (exact match)")
    actor_id: str | None = Field(None, description="Filter by actor ID")
    actor_type: str | None = Field(None, description="Filter by actor type")
    resource_type: str | None = Field(None, description="Filter by resource type")
    resource_id: str | None = Field(None, description="Filter by resource ID")
    status_code: int | None = Field(None, description="Filter by HTTP status code")
    created_after: datetime | None = Field(None, description="Include entries after this timestamp")
    created_before: datetime | None = Field(None, description="Include entries before this timestamp")
    limit: int = Field(default=50, ge=1, le=500, description="Max entries per page")
    offset: int = Field(default=0, ge=0, description="Number of entries to skip")


class AuditLogListResponse(BaseModel):
    """Paginated list of audit log entries."""

    items: list[AuditLogResponse] = Field(..., description="Audit log entries")
    total: int = Field(..., description="Total number of matching entries")
    limit: int = Field(..., description="Page limit used")
    offset: int = Field(..., description="Offset used")
