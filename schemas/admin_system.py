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


# ═══════════════════════════════════════════════════════════════════════════════
# System settings (read-only, masked)
# ═══════════════════════════════════════════════════════════════════════════════


SYSTEM_SETTING_CATEGORIES: dict[str, str] = {
    # Infrastructure
    "OZ_DATABASE_URL": "Infrastructure",
    "OZ_REDIS_URL": "Infrastructure",
    "OZ_FALKORDB_URL": "Infrastructure",
    "OZ_FALKORDB_MAX_CONNECTIONS": "Infrastructure",
    "OZ_FALKORDB_SOCKET_TIMEOUT": "Infrastructure",
    "OZ_SURREALDB_URL": "Infrastructure",
    "OZ_PROMETHEUS_URL": "Infrastructure",
    # Security
    "OZ_SECRET_KEY": "Security",
    "OZ_WEBHOOK_SIGNING_SECRET": "Security",
    "OZ_ROOT_PASSWORD": "Security",
    # Auth
    "OZ_JWT_ACCESS_TOKEN_TTL_MINUTES": "Auth",
    "OZ_JWT_REFRESH_TOKEN_TTL_DAYS": "Auth",
    # Email
    "OZ_SMTP_HOST": "Email",
    "OZ_SMTP_PORT": "Email",
    "OZ_SMTP_USERNAME": "Email",
    "OZ_SMTP_PASSWORD": "Email",
    "OZ_SMTP_FROM_ADDR": "Email",
    "OZ_SMTP_USE_TLS": "Email",
    "OZ_SMTP_START_TLS": "Email",
    # Platform (everything else)
    "OZ_ENVIRONMENT": "Platform",
    "OZ_LOG_LEVEL": "Platform",
    "OZ_CORS_ORIGINS": "Platform",
    "OZ_FRONTEND_URL": "Platform",
    "OZ_HOSTS_ALLOWED": "Platform",
    "OZ_MAX_WORKERS": "Platform",
    "OZ_RATE_LIMIT_IP_MAX": "Platform",
    "OZ_RATE_LIMIT_WINDOW_SEC": "Platform",
    "OZ_PROMPT_CACHING_ENABLED": "Platform",
    "OZ_PROMPT_CACHING_ANTHROPIC_MIN_TOKENS": "Platform",
    "OZ_PROMPT_CACHING_ANTHROPIC_TTL": "Platform",
}
"""Functional category for every key in ``core.openbao.SYSTEM_KEY_MAPPING``."""


class SystemSettingItem(BaseModel):
    """One platform system setting with a masked value."""

    key: str = Field(..., description="``OZ_*`` system setting key.")
    category: str = Field(
        ...,
        description="Functional category: Infrastructure/Security/Auth/Email/Platform.",
    )
    is_set: bool = Field(..., description="Whether the key is set in the system secret.")
    masked_value: str | None = Field(
        default=None,
        description="Masked value (bullets for secrets, userinfo-stripped URLs); None when unset.",
    )


class SystemSettingsResponse(BaseModel):
    """Response for ``GET /admin/system/settings``."""

    data: list[SystemSettingItem] = Field(
        ...,
        description="All known system settings, masked.",
    )


class SystemSettingRevealResponse(BaseModel):
    """Response for ``GET /admin/system/settings/{key}``."""

    key: str = Field(..., description="``OZ_*`` system setting key.")
    value: str = Field(..., description="Raw stored value.")
