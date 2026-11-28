"""Pydantic schemas for the context assembly domain.

Includes request validation and response models for the context
assembly endpoint.  Schemas must never import from ``models/``,
``services/``, or ``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "ContextMetadata",
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
