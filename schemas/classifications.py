"""Pydantic schemas for dialog classification query responses."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ClassificationResponse(BaseModel):
    """Classification result for a single episode.

    Returned by the classification query endpoint.  Excludes the ``raw`` LLM
    output field — that is available via direct DB access if needed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    episode_id: UUID
    intent: str | None = None
    emotion: str | None = None
    valence: str | None = None
    arousal: str | None = None
    confidence: float
    created_at: datetime


class ClassificationListResponse(BaseModel):
    """Response model for listing classifications within a session."""

    data: list[ClassificationResponse]
    total: int
