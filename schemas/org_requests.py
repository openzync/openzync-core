"""Pydantic schemas for in-app organization creation requests.

The in-app channel (``POST /v1/org-requests``) lets any authenticated
dashboard user request a new organization.  Depending on the platform
``org_creation_policy`` the request either creates the org instantly or
enters a pending queue for superadmin approval.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

_RESERVED_ORG_NAME = "SYSTEM"
"""Reserved organization name — the platform org owns it exclusively."""


class OrgRequestCreate(BaseModel):
    """Request body for ``POST /v1/org-requests``.

    Attributes:
        organization_name: Desired organization name — must not be
            ``SYSTEM`` (case-insensitive).
        admin_email: Email of the designated admin user.  Must not belong
            to any live account (globally unique email index).
        admin_name: Optional display name for the designated admin.
    """

    organization_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Desired organization name.",
        examples=["Acme Corp"],
    )
    admin_email: EmailStr = Field(
        ...,
        description="Email of the designated admin user.",
        examples=["admin@acme.com"],
    )
    admin_name: str | None = Field(
        default=None,
        max_length=512,
        description="Display name for the designated admin.",
    )

    @field_validator("organization_name")
    @classmethod
    def _reject_reserved_name(cls, v: str) -> str:
        """Reject the reserved ``SYSTEM`` name (case-insensitive)."""
        stripped = v.strip()
        if stripped.upper() == _RESERVED_ORG_NAME:
            raise ValueError(
                f"Organization name '{v}' is reserved and cannot be used."
            )
        if not stripped:
            raise ValueError("organization_name must not be empty")
        return stripped


class OrgRequestResponse(BaseModel):
    """Response body for ``POST /v1/org-requests``.

    ``status`` is ``approved`` when the org was created instantly under
    ``allow_all``, or ``pending`` when it awaits superadmin approval.
    """

    organization_name: str = Field(..., description="Requested organization name.")
    admin_email: EmailStr = Field(..., description="Designated admin email.")
    status: str = Field(..., description="One of ``approved`` or ``pending``.")
