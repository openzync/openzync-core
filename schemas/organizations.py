"""Pydantic schemas for organization management.

Used by the admin bootstrap flow (``POST /admin/organizations``).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class CreateOrgRequest(BaseModel):
    """Request body for ``POST /admin/organizations``.

    Attributes:
        name: Human-readable organization name.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable organization name.",
        examples=["Acme Corp"],
    )


class CreateOrgResponse(BaseModel):
    """Response body for ``POST /admin/organizations``.

    Attributes:
        organization_id: UUID of the newly created organization.
        organization_name: Name of the organization.
    """

    organization_id: UUID = Field(
        ..., description="UUID of the newly created organization."
    )
    organization_name: str = Field(..., description="Name of the organization.")
