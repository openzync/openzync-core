"""Observation patterns — detection value types and gap math."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

# These can be overridden via constructor kwargs for testing or tuning.

_DEFAULT_MIN_CO_COUNT: int = 3
"""Minimum co-occurrence episode count for a pair to produce an observation."""

_DEFAULT_MIN_APPEARANCES_FOR_TEMPORAL: int = 3
"""Minimum episode appearances for temporal gap analysis."""

_DEFAULT_MIN_GAP_HOURS: float = 1.0
"""Minimum gap in hours between episodes for it to count as a 'gap'."""

_DEFAULT_CO_CONFIDENCE_CAP: int = 20
"""Co-occurrence count at which confidence saturates at 1.0."""


# ── Pattern data types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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


@dataclass(frozen=True)
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


# ── Pattern detection thresholds — tunable per instance ──────────────────────

# ── Module-level helpers ─────────────────────────────────────────────────────


def _stddev(values: list[float], mean: float) -> float:
    """Compute population standard deviation.

    Args:
        values: List of numeric values.
        mean: Pre-computed mean of the values.

    Returns:
        Population standard deviation.
    """
    if len(values) < 2:
        return 0.0
    variance: float = sum((v - mean) ** 2 for v in values) / len(values)
    return variance ** 0.5  # type: ignore[no-any-return]


def _is_monotonic(gaps: list[float], *, increasing: bool) -> bool:
    """Check if gap sequence is consistently increasing or decreasing.

    Uses a relaxed check: at least 60% of consecutive gaps must follow
    the trend direction.

    Args:
        gaps: List of gap values.
        increasing: If True, check for increasing trend; else decreasing.

    Returns:
        True if the gaps follow the trend.
    """
    if len(gaps) < 3:
        return False
    consistent = 0
    total = len(gaps) - 1
    for i in range(total):
        if increasing and gaps[i + 1] > gaps[i] or not increasing and gaps[i + 1] < gaps[i]:
            consistent += 1
    return consistent / total >= 0.6


def _is_burst(
    gaps: list[float],
    *,
    min_gap_threshold: float = 0.5,
    max_burst_window: float = 6,
) -> bool:
    """Detect burst-cluster patterns.

    A burst pattern means appearances come in clusters: several quick
    appearances (gaps < max_burst_window), then long gaps between
    clusters.

    Args:
        gaps: List of gaps between consecutive appearances.
        min_gap_threshold: Minimum gap in hours to count as a
            'between-cluster' gap.
        max_burst_window: Max gap in hours within a cluster.

    Returns:
        True if the pattern is burst-like.
    """
    if len(gaps) < 3:
        return False
    # Count gaps that are very short (within a burst)
    within_burst = sum(1 for g in gaps if g <= max_burst_window)
    # Count gaps that are long (between bursts)
    between_bursts = sum(1 for g in gaps if g > max_burst_window * 4)
    # Burst pattern: at least 2 within-burst gaps and at least 1 between-burst
    return within_burst >= 2 and between_bursts >= 1
