"""Domain events — immutable records of state transitions.

Producers append these; outbox/worker infrastructure delivers them.
No framework imports — plain frozen dataclasses only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class FactSuperseded:
    """An active fact was closed by a successor fact."""

    old_fact_id: UUID
    new_fact_id: UUID | None
    organization_id: UUID
    project_id: UUID
    at_time: datetime


@dataclass(frozen=True, slots=True)
class FactInvalidated:
    """An active fact was closed by LLM-driven contradiction detection."""

    old_fact_id: UUID
    new_fact_id: UUID | None
    organization_id: UUID
    project_id: UUID
    reason: str | None
    at_time: datetime


@dataclass(frozen=True, slots=True)
class IngestBatchCompleted:
    """An ingest batch committed episodes for downstream enrichment."""

    job_id: str
    organization_id: UUID
    project_id: UUID
    session_id: UUID
    episode_count: int


@dataclass(frozen=True, slots=True)
class ObservationPersisted:
    """A graph-topology observation was persisted."""

    observation_id: UUID
    organization_id: UUID
    project_id: UUID
    observation_type: str
    subject_entity_id: UUID
    related_entity_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ExtractionCompleted:
    """A structured extraction finished for an episode."""

    episode_id: UUID
    organization_id: UUID
    project_id: UUID
    schema_id: UUID | None = None


__all__ = [
    "ExtractionCompleted",
    "FactInvalidated",
    "FactSuperseded",
    "IngestBatchCompleted",
    "ObservationPersisted",
]
