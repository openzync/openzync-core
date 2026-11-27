"""Pydantic schemas for the platform admin system endpoints.

Member-role changes. These endpoints are reachable only through
``require_admin`` with the RLS-bypass DB session.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID  # noqa: TC003 — runtime import: pydantic resolves field types

from pydantic import BaseModel, Field


class UpdateMemberRoleRequest(BaseModel):
    """Request body for ``PATCH /admin/system/users/{user_id}/role``.

    Only demotion is assignable — promotion is frozen, grant
    ``permissions`` instead.  Anything else fails with 422 at the
    schema boundary.
    """

    # ⚠️ BREAKING: ``role`` narrowed to ``Literal["member"]`` — the
    # ``admin`` value is gone, promotion raises 422 even for root.
    role: Literal["member"] = Field(
        ...,
        description="New role for the member — only ``member`` (demotion).",
    )


class MemberRoleResponse(BaseModel):
    """Response body for the member-role change endpoint."""

    id: UUID = Field(..., description="The member's user UUID.")
    user_id: UUID = Field(..., description="Owner user UUID (self-ID).")
    role: str = Field(..., description="The updated role.")
