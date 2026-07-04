"""Pydantic schemas for the User domain.

All request/response models for user CRUD endpoints live here.
Response models use ``from_attributes = True`` for ORM-to-schema conversion.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer


class CreateUserRequest(BaseModel):
    """Request body for ``POST /v1/users``.

    Attributes:
        external_id: Caller-defined unique user identifier. Must be unique
            within the organization.
        name: Optional human-readable display name.
        email: Optional email address (validated to contain ``@``).
        metadata: Arbitrary JSON metadata for the user. Deep-merged on update.
    """

    external_id: str = Field(
        ...,
        description="Caller-defined unique user identifier. Must be unique within the organization.",
        min_length=1,
        max_length=255,
        examples=["user_abc123", "alice@example.com", "usr_8f3a2c"],
    )
    name: str | None = Field(
        default=None,
        description="Human-readable display name for the user.",
        max_length=512,
        examples=["Alice Johnson"],
    )
    email: str | None = Field(
        default=None,
        description="Email address for the user.",
        max_length=320,
        examples=["alice@example.com"],
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary JSON metadata for the user. Deep-merged on update.",
        examples=[{"plan": "pro", "region": "us-east", "onboarded_at": "2026-01-15"}],
    )

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str | None) -> str | None:
        """Validate that email contains an ``@`` sign if provided."""
        if v is not None and "@" not in v:
            raise ValueError("Email must contain '@'")
        return v

    @field_validator("external_id")
    @classmethod
    def validate_external_id(cls, v: str) -> str:
        """Validate external_id does not exceed 255 bytes when UTF-8 encoded."""
        if len(v.encode("utf-8")) > 255:
            raise ValueError("external_id exceeds 255 bytes when encoded as UTF-8")
        return v.strip()


class UpdateUserRequest(BaseModel):
    """Request body for ``PATCH /v1/users/{user_id}``.

    All fields are optional. Only provided fields are updated.
    ``metadata`` is **deep-merged** into existing metadata, not replaced.

    Attributes:
        name: New display name. Set to ``null`` to clear.
        email: New email address. Set to ``null`` to clear.
        metadata: Metadata keys to merge into existing metadata.
            Set a key to ``null`` to remove it. Does **not** replace the
            full metadata dict.
    """

    name: str | None = Field(
        default=None,
        description="New display name. Set to null to clear.",
        max_length=512,
    )
    email: str | None = Field(
        default=None,
        description="New email address. Set to null to clear.",
        max_length=320,
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Metadata keys to merge into existing metadata. "
            "Set a key to null to remove it. Does NOT replace the full metadata dict."
        ),
    )

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        """Validate that email contains an ``@`` sign if provided."""
        if v is not None and "@" not in v:
            raise ValueError("Email must contain '@'")
        return v


class UserResponse(BaseModel):
    """Response body for single-user endpoints.

    Attributes:
        id: Internal OpenZync user UUID.
        external_id: Caller-defined user identifier.
        name: Display name.
        email: Email address.
        metadata: User metadata JSON.
        organization_id: Tenant organization UUID.
        created_at: User creation timestamp (UTC).
        updated_at: Last update timestamp (UTC).
        is_deleted: Soft-delete flag. ``True`` during the 30-day GDPR grace period.
    """

    id: UUID = Field(..., description="Internal OpenZync user UUID.")
    external_id: str = Field(..., description="Caller-defined user identifier.")
    name: str | None = Field(default=None, description="Display name.")
    email: str | None = Field(default=None, description="Email address.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="User metadata JSON.",
    )
    organization_id: UUID = Field(..., description="Tenant organization UUID.")
    created_at: datetime = Field(..., description="User creation timestamp (UTC).")
    updated_at: datetime = Field(..., description="Last update timestamp (UTC).")
    is_deleted: bool = Field(
        default=False,
        description="Soft-delete flag. True during the 30-day GDPR grace period.",
    )

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class UserResponseWithStats(UserResponse):
    """Extended user response with aggregate statistics.

    Inherits all fields from :class:`UserResponse` and adds summary
    counts for ingested messages, extracted facts, and sessions.

    Attributes:
        message_count: Total number of ingested messages (episodes).
        fact_count: Total number of extracted facts for this user.
        session_count: Total number of sessions (including closed).
    """

    message_count: int = Field(
        default=0,
        description="Total number of ingested messages (episodes).",
    )
    fact_count: int = Field(
        default=0,
        description="Total number of extracted facts for this user.",
    )
    session_count: int = Field(
        default=0,
        description="Total number of sessions (including closed).",
    )


class UserListResponse(BaseModel):
    """Paginated response for ``GET /v1/users``.

    Attributes:
        data: List of users for the current page.
        next_cursor: Cursor to pass as ``?cursor=`` in the next request.
            ``None`` if no more results.
        has_more: ``True`` if there are additional pages beyond this one.
    """

    data: list[UserResponse] = Field(..., description="List of users for the current page.")
    next_cursor: str | None = Field(
        default=None,
        description="Cursor to pass as ?cursor= in the next request. Null if no more results.",
    )
    has_more: bool = Field(
        default=False,
        description="True if there are additional pages beyond this one.",
    )
