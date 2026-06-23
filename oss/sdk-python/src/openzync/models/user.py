"""Pydantic models for the user domain."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class UserCreateRequest(BaseModel):
    """Request body for ``POST /v1/users``."""

    external_id: str = Field(..., min_length=1, max_length=255, description="Caller-defined user identifier.")
    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UserUpdateRequest(BaseModel):
    """Request body for ``PATCH /v1/users/{user_id}``."""

    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] | None = Field(default=None)


class UserResponse(BaseModel):
    """Response from user CRUD endpoints."""

    id: str = Field(..., description="Internal UUID.")
    external_id: str = Field(..., description="Caller-defined identifier.")
    name: str | None = None
    email: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    organization_id: str = Field(..., description="Owning organization UUID.")
    created_at: str = Field(..., description="ISO-8601 creation timestamp.")
    updated_at: str = Field(..., description="ISO-8601 update timestamp.")
    is_deleted: bool = Field(default=False)
    message_count: int = Field(default=0)
    fact_count: int = Field(default=0)
    session_count: int = Field(default=0)


class UserListResponse(BaseModel):
    """Response from ``GET /v1/users``."""

    data: list[UserResponse] = Field(..., description="List of users.")
    next_cursor: str | None = Field(default=None, description="Cursor for the next page.")
    has_more: bool = Field(default=False)
