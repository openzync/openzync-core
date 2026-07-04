"""Pydantic schemas for user summary API.

The summary is an auto-generated synopsis of a user's conversation history,
updated asynchronously by a background worker.  These schemas cover the
read endpoint and the trigger-regeneration endpoint.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class UserSummaryResponse(BaseModel):
    """Response model for fetching a user's summary.

    Attributes:
        user_id: The internal OpenZync user UUID.
        summary: The generated summary text, or ``None`` if not yet generated.
        updated_at: When the summary was last regenerated, or ``None``.
    """

    user_id: UUID
    summary: str | None
    updated_at: datetime | None
    model_config = ConfigDict(from_attributes=True)


class UserSummaryTriggerResponse(BaseModel):
    """Response model for triggering a summary regeneration.

    Attributes:
        message: Human-readable confirmation message.
        status: Job status indicator — always ``"processing"`` on initial
            acceptance.
        user_id: The internal OpenZync user UUID.
    """

    message: str
    status: str = "processing"
    user_id: UUID
