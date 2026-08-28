"""Pydantic schemas for admin dashboard statistics.

All response models aggregate data across an entire organization.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class OrgStatsResponse(BaseModel):
    """Aggregate statistics for the dashboard overview.

    Provides a quick snapshot of the organization's data volume
    across episodes, sessions, facts, extractions, observations,
    and classifications.
    """

    organization_id: UUID = Field(..., description="The organization UUID.")
    total_episodes: int = Field(..., description="Total episodes (conversation turns).")
    total_sessions: int = Field(..., description="Total sessions across all projects.")
    total_facts: int = Field(
        ..., description="Total extracted facts across all projects."
    )
    total_extractions: int = Field(
        ..., description="Total structured extractions across all projects."
    )
    total_observations: int = Field(
        ..., description="Total graph observations across all projects."
    )
    total_classifications: int = Field(
        ..., description="Total dialog classifications across all projects."
    )


class UsageStatsResponse(BaseModel):
    """Daily usage statistics for the dashboard.

    Attributes:
        date: The date (YYYY-MM-DD) for this data point.
        episode_count: Number of episodes created on this date.
        session_count: Number of sessions created on this date.
        fact_count: Number of facts created on this date.
        extraction_count: Number of structured extractions created on this date.
        observation_count: Number of graph observations created on this date.
        classification_count: Number of dialog classifications created on this date.
        node_count: Number of graph entity nodes created on this date.
        edge_count: Number of graph relationship edges created on this date.
    """

    date: str = Field(..., description="Date in YYYY-MM-DD format.")
    episode_count: int = Field(0, description="Episodes created on this date.")
    session_count: int = Field(0, description="Sessions created on this date.")
    fact_count: int = Field(0, description="Facts created on this date.")
    extraction_count: int = Field(
        0, description="Structured extractions created on this date."
    )
    observation_count: int = Field(
        0, description="Graph observations created on this date."
    )
    classification_count: int = Field(
        0, description="Dialog classifications created on this date."
    )
    node_count: int = Field(0, description="Graph entity nodes created on this date.")
    edge_count: int = Field(
        0, description="Graph relationship edges created on this date."
    )
