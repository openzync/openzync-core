"""Pydantic schemas for the admin LLM usage read API.

All response models expose metered inference rows scoped to the
authenticated organization. No ORM leakage — routers convert via
``model_validate``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LLMUsageResponse(BaseModel):
    """A single metered inference record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="Usage row UUID.")
    user_id: UUID = Field(..., description="Owning organization UUID.")
    provider: str = Field(..., description="Backend identifier.")
    model: str = Field(..., description="Model identifier.")
    worker: str | None = Field(None, description="Worker/service label.")
    project_id: UUID | None = Field(None, description="Project scope.")
    episode_id: UUID | None = Field(None, description="Source episode.")
    session_id: UUID | None = Field(None, description="Source session.")
    community_id: UUID | None = Field(None, description="Summarised community.")
    task_run_id: UUID | None = Field(None, description="Owning task run.")
    prompt_tokens: int = Field(..., description="Prompt tokens.")
    completion_tokens: int = Field(..., description="Completion tokens.")
    reasoning_tokens: int = Field(..., description="Reasoning tokens.")
    cache_read_input_tokens: int = Field(..., description="Cache-read tokens.")
    cache_creation_input_tokens: int = Field(..., description="Cache-creation tokens.")
    total_tokens: int = Field(..., description="Prompt + completion tokens.")
    embed_count: int | None = Field(None, description="Embedded text count.")
    embed_dim: int | None = Field(None, description="Embedding dimensionality.")
    duration_ms: int = Field(..., description="Call duration in milliseconds.")
    created_at: datetime = Field(..., description="Record timestamp.")


class UsageSummaryResponse(BaseModel):
    """Aggregate metering totals over the requested window."""

    calls: int = Field(..., description="Metered call count.")
    prompt_tokens: int = Field(..., description="Summed prompt tokens.")
    completion_tokens: int = Field(..., description="Summed completion tokens.")
    reasoning_tokens: int = Field(..., description="Summed reasoning tokens.")
    total_tokens: int = Field(..., description="Summed prompt + completion.")
    avg_duration_ms: float = Field(..., description="Average call duration.")


class LLMUsageListResponse(BaseModel):
    """Paginated metering rows plus window summary."""

    data: list[LLMUsageResponse] = Field(..., description="Metering rows.")
    total: int = Field(..., description="Total rows matching the filters.")
    summary: UsageSummaryResponse = Field(
        ..., description="Aggregates over the same window and filters."
    )


class LlmUsageDailyBucket(BaseModel):
    """One sparse UTC-day aggregate (no zero-fill; frontend fills gaps)."""

    date: str = Field(..., description="UTC day in YYYY-MM-DD format.")
    total_tokens: int = Field(..., description="Summed prompt + completion.")
    calls: int = Field(..., description="Metered call count on this day.")


class LLMUsageDailyListResponse(BaseModel):
    """Sparse per-day buckets plus window summary (``group_by=day``)."""

    data: list[LlmUsageDailyBucket] = Field(..., description="Day buckets.")
    # ⚠️ BREAKING (grouped only): total=data day-bucket count, not call count.
    total: int = Field(..., description="Number of day buckets (not calls).")
    summary: UsageSummaryResponse = Field(
        ..., description="Aggregates over the same window and filters."
    )
