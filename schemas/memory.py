"""Pydantic schemas for the memory (message ingestion) domain.

Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Message(BaseModel):
    """A single conversation turn within a session.

    Attributes:
        role: Message sender role — one of ``user``, ``assistant``,
            ``system``, ``tool``.
        content: Message body text. Maximum 64KB when UTF-8 encoded.
        created_at: ISO-8601 timestamp. Assigned server-side if omitted.
        metadata: Optional caller-defined metadata (tags, labels, etc.).
    """

    role: str = Field(
        ...,
        description="Message sender role. One of: user, assistant, system, tool.",
        pattern=r"^(user|assistant|system|tool)$",
    )
    content: str = Field(
        ...,
        description="Message body text. Max 64KB.",
        max_length=65536,
    )
    created_at: datetime | None = Field(
        default=None,
        description="ISO-8601 timestamp of when the message was created. "
        "Assigned server-side if omitted.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional caller-defined metadata (tags, labels, etc.).",
    )

    @field_validator("content")
    @classmethod
    def check_byte_size(cls, v: str) -> str:
        """Enforce 64KB wire-level limit per SEC-09.

        ``max_length`` on the ``Field`` only checks Unicode code-point count.
        Multi-byte characters (e.g. emoji) can blow past 64KB on the wire
        while staying under the character limit. This validator catches that.

        Args:
            v: The content string to validate.

        Returns:
            The content string unchanged if valid.

        Raises:
            ValueError: If the UTF-8 encoded content exceeds 65536 bytes.
        """
        if len(v.encode("utf-8")) > 65536:
            raise ValueError("Content exceeds 64KB when encoded as UTF-8")
        return v


class IngestMemoryRequest(BaseModel):
    """Request body for ``POST /v1/projects/{project_id}/memory``.

    Attributes:
        session_id: Optional session external ID. If omitted, a session
            named ``__default__`` is auto-created for the user.
        messages: List of message objects. Must contain at least 1 and
            at most 1000 messages.
    """

    session_id: str | None = Field(
        default=None,
        description="Session external_id. Auto-creates a __default__ session "
        "if omitted.",
    )
    messages: list[Message] = Field(
        ...,
        description="List of message objects to ingest. At least 1 required.",
        min_length=1,
        max_length=1000,
    )


class IngestMemoryResponse(BaseModel):
    """Response returned after successful ingestion.

    Attributes:
        job_id: UUID string identifying the async enrichment job. Can be
            used to track completion via the job status endpoint.
        episode_count: Number of episodes (messages) ingested.
        status: Always ``"accepted"`` for synchronous acknowledgement.
        message: Human-readable status message.
    """

    job_id: str | None = Field(
        default=None,
        description="UUID of the async enrichment job for tracking.",
    )
    episode_count: int = Field(
        default=0,
        description="Number of episodes (messages) ingested.",
    )
    status: str = Field(
        default="accepted",
        description="Always 'accepted' for synchronous acknowledgement.",
    )
    message: str = Field(
        default="Messages accepted for processing",
        description="Human-readable status message.",
    )


class DeleteMemoryResponse(BaseModel):
    """Response body for ``DELETE /v1/projects/{project_id}/memory``.

    Attributes:
        status: Outcome of the deletion operation.
        episodes_deleted: Number of episodes soft-deleted.
        facts_deleted: Number of facts soft-deleted.
    """

    status: str = Field(
        default="deleted",
        description="Outcome of the deletion operation.",
    )
    episodes_deleted: int = Field(
        default=0,
        ge=0,
        description="Number of episodes soft-deleted.",
    )
    facts_deleted: int = Field(
        default=0,
        ge=0,
        description="Number of facts soft-deleted.",
    )
