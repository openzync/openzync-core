"""Pydantic schemas for dialog classification query responses."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ClassificationResponse(BaseModel):
    """Classification result for a single episode.

    Returned by the classification query endpoint.  Excludes the ``raw`` LLM
    output field — that is available via direct DB access if needed.

    The ``message`` and ``role`` fields are populated by the service layer
    via a batch query — they are not ORM-mapped attributes.
    """

    id: UUID
    episode_id: UUID
    intent: str | None = None
    emotion: str | None = None
    valence: str | None = None
    arousal: str | None = None
    confidence: float
    created_at: datetime
    message: str = ""       # Full episode.content text (populated by service layer)
    role: str = ""           # user/assistant/system/tool (populated by service layer)


class ClassificationListResponse(BaseModel):
    """Response model for listing classifications within a session."""

    data: list[ClassificationResponse]
    total: int
