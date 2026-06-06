"""Pydantic models for the memory (message ingestion) domain."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Message(BaseModel):
    """A single conversation turn."""

    role: str = Field(..., description="Message sender role: user, assistant, system, tool.")
    content: str = Field(..., description="Message body text.", max_length=65536)
    created_at: datetime | None = Field(default=None, description="ISO-8601 timestamp.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Caller-defined metadata.")


class IngestMemoryRequest(BaseModel):
    """Request body for ``POST /v1/users/{user_id}/memory``."""

    session_id: str | None = Field(default=None, description="Optional session external ID.")
    messages: list[Message] = Field(..., min_length=1, max_length=1000)


class IngestMemoryResponse(BaseModel):
    """Response returned after successful ingestion."""

    job_id: str | None = Field(default=None, description="UUID of the async enrichment job.")
    episode_count: int = Field(default=0, description="Number of episodes ingested.")
    status: str = Field(default="accepted", description="Always 'accepted'.")
    message: str = Field(default="Messages accepted for processing.")


class ContextResponse(BaseModel):
    """Response from the context assembly endpoint."""

    context: str = Field(..., description="Formatted context block for LLM injection.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Assembly metadata.")
