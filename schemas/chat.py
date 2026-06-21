"""Chat request/response schemas."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Request body for the chat SSE endpoint."""

    session_id: UUID | None = Field(
        default=None,
        description="Session UUID.  If omitted, the server creates or reuses "
        "the ``__chat__`` session for this user.",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="The user's chat message.",
    )


class ChatSessionResponse(BaseModel):
    """Response after creating or locating a chat session."""

    session_id: UUID
