"""Pydantic schemas for session and message CRUD operations.

Corresponds to the ``/v1/projects/{project_id}/sessions`` endpoints.
Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateSessionRequest(BaseModel):
    """Request body for POST /v1/projects/{project_id}/sessions.

    Attributes:
        external_id: Caller-defined session identifier. Must be unique per
            project (enforced by a DB unique constraint on
            (project_id, external_id)).
        metadata: Optional metadata key-value pairs. Deep-merged on subsequent
            PATCH operations (not yet implemented).
    """

    external_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Caller-defined session identifier. Must be unique per project.",
        examples=["session_abc", "ticket_4492", "chat_8f3a"],
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional session metadata. Deep-merged on subsequent updates.",
        examples=[{"channel": "web", "language": "en", "agent_version": "2.1.0"}],
    )


class SessionResponse(BaseModel):
    """Response body for single-session GET and POST endpoints.

    Contains full session details including aggregate statistics.
    """

    id: UUID = Field(..., description="Internal OpenZep session UUID.")
    project_id: UUID = Field(
        ..., description="Project UUID this session belongs to."
    )
    created_by: UUID = Field(
        ..., description="UUID of the user who created this session."
    )
    external_id: str = Field(..., description="Caller-defined session identifier.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_",
        description="Session metadata JSON.",
    )
    is_active: bool = Field(
        default=True, description="Whether the session is accepting new messages."
    )
    message_count: int = Field(
        default=0, description="Total number of messages (episodes) in this session."
    )
    fact_count: int = Field(
        default=0,
        description="Total number of facts extracted from this session's messages.",
    )
    closed_at: datetime | None = Field(
        default=None,
        description="Timestamp when the session was closed. Null if open.",
    )
    created_at: datetime = Field(
        ..., description="Session creation timestamp (UTC)."
    )
    updated_at: datetime = Field(
        ..., description="Last activity timestamp (UTC)."
    )

    model_config = ConfigDict(from_attributes=True)


class SessionListResponse(BaseModel):
    """Lightweight session representation for list endpoints.

    Excludes ``metadata`` and ``updated_at`` to keep list responses compact.
    Use the individual GET endpoint for full details.
    """

    id: UUID = Field(..., description="Internal OpenZep session UUID.")
    project_id: UUID = Field(
        ..., description="Project UUID this session belongs to."
    )
    created_by: UUID = Field(
        ..., description="UUID of the user who created this session."
    )
    external_id: str = Field(..., description="Caller-defined session identifier.")
    is_active: bool = Field(
        default=True, description="Whether the session is accepting new messages."
    )
    message_count: int = Field(
        default=0, description="Total number of messages in this session."
    )
    fact_count: int = Field(
        default=0, description="Total number of facts extracted from this session."
    )
    created_at: datetime = Field(
        ..., description="Session creation timestamp (UTC)."
    )

    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    """A single message (episode) within a session.

    Messages are ordered by ``sequence_number`` within a session for
    deterministic ordering (not by ``created_at``, which can have ties).
    """

    id: UUID = Field(..., description="Internal episode UUID.")
    role: str = Field(
        ..., description="Message role: user / assistant / system / tool."
    )
    content: str = Field(..., description="Message body text.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_",
        description="Per-message metadata JSON.",
    )
    token_count: int = Field(
        default=0, description="Approximate token count for this message."
    )
    sequence_number: int = Field(
        ..., description="Zero-indexed position within the session."
    )
    created_at: datetime = Field(
        ..., description="Message creation timestamp (UTC)."
    )

    model_config = ConfigDict(from_attributes=True)
