"""Pydantic schemas for the observation (graph-topology analysis) domain.

Observations are currently created internally by the ``compute_observations``
worker — there is no public create/update API.  These schemas exist to support
future query endpoints and to provide a typed serialization layer.

Schemas must never import from ``models/``, ``services/``, or ``routers/``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ObservationResponse(BaseModel):
    """A single observation about an entity, surfaced from graph-topology analysis.

    Attributes:
        id: UUID primary key.
        organization_id: Owning organization (RLS enforcement).
        project_id: Owning project (project-scoped isolation).
        subject_entity_id: The entity this observation is about.
        related_entity_id: For pair-level observations (e.g., co-occurrence),
            the other entity in the pair.  NULL for entity-level observations.
        observation_type: Type of observation — one of ``ObservationType``
            enum values (e.g. ``co_occurrence``, ``temporal_pattern``).
        content: Natural-language description of the observation.
        supporting_fact_ids: UUIDs of ``facts`` rows that support this observation.
        supporting_relationship_ids: UUIDs of ``graph_relationships`` rows
            that support this observation.
        confidence: How confident the system is in this observation (0.0–1.0).
        valid_from: Start of this observation's temporal validity.
        valid_to: End of this observation's temporal validity (NULL = open).
        observation_metadata: Arbitrary JSONB metadata for extensibility.
        created_at: Row creation timestamp.
        updated_at: Row last-update timestamp.
    """

    id: UUID = Field(..., description="UUID primary key.")
    organization_id: UUID = Field(..., description="Owning organization.")
    project_id: UUID = Field(..., description="Owning project.")
    subject_entity_id: UUID = Field(
        ..., description="The entity this observation is about.",
    )
    related_entity_id: UUID | None = Field(
        default=None,
        description="Other entity in a pair-level observation (NULL for entity-level).",
    )
    observation_type: str = Field(
        ..., description="One of ObservationType enum values.",
    )
    content: str = Field(
        ..., description="Natural-language description of the observation.",
    )
    supporting_fact_ids: list[UUID] | None = Field(
        default=None, description="UUIDs of supporting facts.",
    )
    supporting_relationship_ids: list[UUID] | None = Field(
        default=None, description="UUIDs of supporting graph relationships.",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0,
        description="Confidence score (0.0–1.0).",
    )
    valid_from: datetime | None = Field(
        default=None, description="Start of temporal validity (UTC).",
    )
    valid_to: datetime | None = Field(
        default=None, description="End of temporal validity (NULL = open).",
    )
    observation_metadata: dict | None = Field(
        default=None, description="Arbitrary JSONB metadata.",
    )
    created_at: datetime = Field(
        ..., description="Row creation timestamp (UTC).",
    )
    updated_at: datetime = Field(
        ..., description="Row last-update timestamp (UTC).",
    )

    model_config = ConfigDict(from_attributes=True)


class ObservationListResponse(BaseModel):
    """Paginated list response for observation query endpoints.

    Attributes:
        data: List of observation responses for the current page.
        total: Total number of observations matching the query.
    """

    data: list[ObservationResponse] = Field(
        ..., description="List of observations for the current page.",
    )
    total: int = Field(
        ..., ge=0, description="Total number of matching observations.",
    )
