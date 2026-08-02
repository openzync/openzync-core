"""Pydantic schemas for the context assembly domain.

Includes request validation and response models for the context
assembly endpoint.  Schemas must never import from ``models/``,
``services/``, or ``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "ContextBlob",
    "ContextMetadata",
    "ContextRequest",
    "ContextResponse",
]


class ContextMetadata(BaseModel):
    """Metadata returned alongside the assembled context block.

    Attributes:
        cache_hit: Whether the result was served from cache.
        assembly_time_ms: Wall-clock time for context assembly (ms).
        source_counts: Breakdown of items by source type.
        total_items: Total number of items included in the context.
        as_of: Effective-at timestamp used for fact retrieval (None = now).
    """

    cache_hit: bool = Field(
        default=False,
        description="Whether the result was served from cache.",
    )
    assembly_time_ms: float = Field(
        default=0.0,
        description="Wall-clock time for context assembly (ms).",
    )
    source_counts: dict[str, Any] = Field(
        default_factory=dict,
        description="Breakdown of items by source type.",
    )
    total_items: int = Field(
        default=0,
        description="Total number of items included in the context.",
    )
    as_of: datetime | None = Field(
        default=None,
        description="Effective-at timestamp used for fact retrieval (UTC). "
        "None means the query ran against 'now'.",
    )


class ContextBlob(BaseModel):
    """A blob referenced in context assembly.

    Attributes:
        id: Blob UUID.
        file_name: Original filename.
        mime_type: MIME type (e.g. ``"application/pdf"``).
        file_size: Size in bytes.
        download_url: Presigned download URL with short TTL (may be
            ``None`` if unavailable).
    """

    id: UUID
    file_name: str
    mime_type: str
    file_size: int
    download_url: str | None = None  # Presigned URL, short TTL


class ContextRequest(BaseModel):
    """Query parameters for ``GET /v1/projects/{project_id}/context``.

    Attributes:
        query: Natural-language query to retrieve relevant context for.
        limit: Maximum number of items per source type to include (1–100).
        format: Output format — ``"text"`` for plain text or ``"json"``
            for structured JSON.
    """

    query: str = Field(
        ...,
        description="Natural-language query for context retrieval.",
        min_length=1,
        max_length=2000,
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max items per source type (1–100).",
    )
    format: str = Field(
        default="text",
        pattern=r"^(text|json)$",
        description='Output format — "text" or "json".',
    )


class ContextResponse(BaseModel):
    """Response body for the context assembly endpoint.

    Attributes:
        context: The assembled context block as a string (text or JSON
            serialised, depending on the requested format).
        metadata: Assembly metadata including cache status, timing, and
            source breakdown.
    """

    context: str = Field(
        ...,
        description="Assembled context block for LLM injection.",
    )
    metadata: ContextMetadata = Field(
        default_factory=ContextMetadata,
        description="Assembly metadata (cache, timing, counts).",
    )
