"""Observation detection patterns — immutable value types for detectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CoOccurrencePattern:
    """A detected co-occurrence between two entities.

    Attributes:
        entity_a_id: UUID of the first entity.
        entity_a_name: Name of the first entity.
        entity_b_id: UUID of the second entity.
        entity_b_name: Name of the second entity.
        co_count: Number of episodes where both entities appear.
        total_episodes: Total number of episodes in the project.
        relationship_ids: UUIDs of graph_relationships connecting
            these two entities (if any).
    """

    entity_a_id: UUID
    entity_a_name: str
    entity_b_id: UUID
    entity_b_name: str
    co_count: int
    total_episodes: int
    relationship_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TemporalGapPattern:
    """Temporal gap analysis for a single entity.

    Attributes:
        entity_id: UUID of the entity.
        entity_name: Name of the entity.
        appearance_count: Number of episode appearances.
        pattern_type: One of ``periodic``, ``widening``, ``narrowing``,
            ``burst``, ``irregular``.
        mean_gap_hours: Mean gap between consecutive appearances, in hours.
        stddev_gap_hours: Standard deviation of gaps, in hours.
        min_gap_hours: Minimum gap, in hours.
        max_gap_hours: Maximum gap, in hours.
        span_days: Total time span from first to last appearance, in days.
    """

    entity_id: UUID
    entity_name: str
    appearance_count: int
    pattern_type: str
    mean_gap_hours: float
    stddev_gap_hours: float
    min_gap_hours: float
    max_gap_hours: float
    span_days: float


@dataclass(frozen=True, slots=True)
class BehavioralPattern:
    """A detected behavioral pattern for a single entity.

    Attributes:
        entity_id: UUID of the entity.
        entity_name: Name of the entity.
        entity_type: Type of the entity.
        frequent_predicates: Predicates that appear most often in facts
            where this entity is the subject.
        total_facts: Total number of facts about this entity.
        description_hint: Structured hint for LLM content generation.
    """

    entity_id: UUID
    entity_name: str
    entity_type: str
    frequent_predicates: dict[str, int]  # predicate → count
    total_facts: int
    description_hint: str
