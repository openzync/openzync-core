"""Pydantic models for the session domain."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SessionCreateRequest(BaseModel):
    """Request body for ``POST /v1/projects/{project_id}/sessions``."""

    external_id: str = Field(..., min_length=1, max_length=255, description="Caller-defined session identifier.")
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionResponse(BaseModel):
    """Response from session CRUD endpoints."""

    id: str = Field(..., description="Internal UUID.")
    project_id: str = Field(..., description="Owning project UUID.")
    created_by: str = Field(..., description="User UUID who created the session.")
    external_id: str = Field(..., description="Caller-defined identifier.")
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = Field(default=True)
    message_count: int = Field(default=0)
    fact_count: int = Field(default=0)
    created_at: str = Field(..., description="ISO-8601 creation timestamp.")


class SessionListResponse(BaseModel):
    """Response from ``GET /v1/projects/{project_id}/sessions``."""

    data: list[SessionResponse] = Field(..., description="List of sessions.")
    next_cursor: str | None = Field(default=None, description="Cursor for the next page.")
    has_more: bool = Field(default=False)


class SessionMessagesResponse(BaseModel):
    """Response from ``GET /v1/projects/{project_id}/sessions/{session_id}/messages``."""

    class MessageItem(BaseModel):
        id: str = Field(..., description="Episode UUID.")
        role: str = Field(..., description="Message role.")
        content: str = Field(..., description="Message content.")
        metadata: dict[str, Any] = Field(default_factory=dict)
        token_count: int = Field(default=0)
        sequence_number: int = Field(default=0)
        created_at: str = Field(..., description="ISO-8601 timestamp.")

    data: list[MessageItem] = Field(..., description="List of messages.")
    next_cursor: str | None = Field(default=None)
    has_more: bool = Field(default=False)
