"""Pydantic schemas for structured extraction query responses."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class StructuredExtractionResponse(BaseModel):
    """A single structured extraction result.

    Contains the extracted data payload and links to the source episode
    and the extraction schema that defined the expected shape.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    episode_id: UUID
    schema_id: UUID | None = None
    data: dict
    created_at: datetime


class StructuredExtractionListResponse(BaseModel):
    """Response model for listing structured extractions within a session."""

    items: list[StructuredExtractionResponse]
    total: int
