"""Observation domain entity — a graph-topology pattern record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Observation:
    """Framework-free observation snapshot mapped from the ORM row at the boundary.

    Attributes:
        id: Observation primary key.
        project_id: Owning project (tenant scope).
        organization_id: Owning organization (tenant scope).
        subject_entity_id: Entity the observation is about.
        related_entity_id: Other entity for pair-level observations.
        observation_type: ``co_occurrence`` / ``temporal_pattern`` /
            ``behavioral_pattern``.
        content: Natural-language description.
        confidence: Confidence score (0.0–1.0).
        valid_from: Temporal validity start.
    """

    id: UUID
    project_id: UUID
    organization_id: UUID
    subject_entity_id: UUID
    observation_type: str
    content: str
    confidence: float = 0.0
    related_entity_id: UUID | None = None
    valid_from: datetime | None = None
