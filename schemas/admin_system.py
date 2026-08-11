"""Pydantic schemas for the platform super-admin system endpoints.

Cross-org administration surfaces: org listings, per-org config access,
and member-role changes.  These endpoints are reachable only through
``require_superadmin`` with the RLS-bypass DB session.
"""

from __future__ import annotations

from datetime import (
    datetime,  # noqa: TC003 — runtime import: pydantic resolves field types
)
from typing import Literal
from uuid import UUID  # noqa: TC003 — runtime import: pydantic resolves field types

from pydantic import BaseModel, Field


class SystemOrgListItem(BaseModel):
    """One row of the superadmin org listing."""

    id: UUID = Field(..., description="Organization UUID.")
    name: str = Field(..., description="Organization name.")
    status: str = Field(..., description="Lifecycle state: pending/approved/rejected.")
    created_at: datetime = Field(..., description="Creation timestamp.")


class SystemOrgListResponse(BaseModel):
    """Paginated response for ``GET /admin/system/orgs``."""

    data: list[SystemOrgListItem] = Field(..., description="Org rows on this page.")
    total: int = Field(..., description="Total matching orgs across all pages.")
    page: int = Field(..., description="Current page (1-based).")
    limit: int = Field(..., description="Page size.")


class SystemMemberListItem(BaseModel):
    """One row of the superadmin org-members listing."""

    id: UUID = Field(..., description="User UUID.")
    email: str = Field(
        ...,
        description="User email — falls back to ``external_id`` when unset.",
    )
    name: str | None = Field(default=None, description="Display name.")
    role: str = Field(..., description="Dashboard role: admin/member.")
    is_active: bool = Field(..., description="Whether the account is active.")


class SystemOrgMembersResponse(BaseModel):
    """Paginated response for ``GET /admin/system/orgs/{org_id}/members``."""

    data: list[SystemMemberListItem] = Field(
        ..., description="Member rows on this page."
    )
    total: int = Field(..., description="Total members across all pages.")
    page: int = Field(..., description="Current page (1-based).")
    limit: int = Field(..., description="Page size.")


class UpdateMemberRoleRequest(BaseModel):
    """Request body for ``PATCH /admin/system/orgs/{org_id}/members/{user_id}/role``.

    Only the two tenant roles are assignable — anything else fails with
    422 at the schema boundary.
    """

    role: Literal["admin", "member"] = Field(
        ...,
        description="New role for the member — ``admin`` or ``member``.",
    )


class MemberRoleResponse(BaseModel):
    """Response body for the member-role change endpoint."""

    id: UUID = Field(..., description="The member's user UUID.")
    organization_id: UUID = Field(..., description="Owning organization UUID.")
    role: str = Field(..., description="The updated role.")


class OrgApprovalResponse(BaseModel):
    """Response body for the org approve/reject endpoints."""

    id: UUID = Field(..., description="Organization UUID.")
    name: str = Field(..., description="Organization name.")
    status: str = Field(..., description="Lifecycle state after the action.")
