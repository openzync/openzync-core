"""Pydantic schemas for admin dashboard statistics.

All response models aggregate data across an entire organization.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class OrgStatsResponse(BaseModel):
    """Aggregate statistics for the dashboard overview.

    Provides a quick snapshot of the organization's data volume
    across all users and sessions.
    """

    organization_id: UUID = Field(
        ..., description="The organization UUID."
    )
    total_users: int = Field(
        ..., description="Total non-deleted users in the organization."
    )
    total_sessions: int = Field(
        ..., description="Total sessions across all users."
    )
    total_episodes: int = Field(
        ..., description="Total episodes (conversation turns)."
    )
    total_facts: int = Field(
        ..., description="Total extracted facts across all users."
    )
    total_messages: int = Field(
        ..., description="Total messages across all episodes."
    )
    total_api_keys: int = Field(
        ..., description="Total non-revoked API keys."
    )


class UsageStatsResponse(BaseModel):
    """Daily usage statistics for the dashboard.

    Attributes:
        date: The date (YYYY-MM-DD) for this data point.
        message_count: Number of messages processed on this date.
        session_count: Number of sessions created on this date.
    """

    date: str = Field(
        ..., description="Date in YYYY-MM-DD format."
    )
    message_count: int = Field(
        ..., description="Messages processed on this date."
    )
    session_count: int = Field(
        ..., description="Sessions created on this date."
    )
