"""Observation service — graph-topology pattern detection and description generation.

This service implements the second-pass inference over graph topology that
surfaces observations like "Jane upgrades within 2 weeks of every product
launch" or "Entity X and Entity Y always appear together in support tickets."

Architecture
------------
Pattern detection is **SQL-first and LLM-optional**.  All detection algorithms
query PostgreSQL directly and produce structured results.  The LLM is used
only to generate the ``content`` (natural-language description) field; when
the LLM is unavailable, a template-based description is used instead.

Detection methods
-----------------
1. **Co-occurrence frequency**  (``detect_co_occurrences``)
   - Queries ``graph_episode_entities`` for entity pairs that appear in the
     same episodes above a configurable threshold.
   - Produces one ``CoOccurrencePattern`` per pair, with supporting
     ``graph_relationships`` evidence.

2. **Temporal gap analysis**  (``detect_temporal_gaps``)
   - For each entity, orders its episode appearances by time and calculates
     gaps between consecutive appearances.
   - Classifies the gap pattern as periodic, widening, narrowing, burst,
     or irregular.
   - Produces one ``TemporalGapPattern`` per entity.

3. **Behavioral pattern detection**  (``detect_behavioral_patterns``)
   - Scans facts for each entity to find consistently repeated predicates
     or notable temporal sequences (e.g., ``asked_about_pricing`` followed
     by ``churned``).
   - Produces one ``BehavioralPattern`` per entity.

Warn-only design
----------------
All detection is **warn-only** — the service logs contradictions but never
mutates existing facts, relationships, or observations.  Auto-expiry or
corrective mutation, if ever needed, must go behind a feature flag and be
explicitly enabled.

Cross-tenant safety
-------------------
Every query scopes by ``project_id`` and ``organization_id``.  No cross-tenant
data leakage is possible.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from models.graph_observation import ObservationType
from repositories.observation_repository import ObservationRepository

logger = structlog.get_logger(__name__)

# ── Configuration defaults ─────────────────────────────────────────────────────
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


class ObservationService:
    """Graph-topology pattern detection and observation persistence.

    Args:
        repo: An ``ObservationRepository`` for queries and persistence.
        min_co_count: Minimum co-occurrence count (default 3).
        min_appearances_for_temporal: Minimum appearances for temporal
            analysis (default 3).
        min_gap_hours: Minimum gap in hours to count as a 'gap'
            (default 1.0).
        co_confidence_cap: Co-occurrence count at which confidence
            saturates at 1.0 (default 20).
    """

    def __init__(
        self,
        repo: ObservationRepository,
        *,
        min_co_count: int = _DEFAULT_MIN_CO_COUNT,
        min_appearances_for_temporal: int = _DEFAULT_MIN_APPEARANCES_FOR_TEMPORAL,
        min_gap_hours: float = _DEFAULT_MIN_GAP_HOURS,
        co_confidence_cap: int = _DEFAULT_CO_CONFIDENCE_CAP,
    ) -> None:
        self._repo = repo
        self._min_co_count = min_co_count
        self._min_appearances_for_temporal = min_appearances_for_temporal
        self._min_gap_hours = min_gap_hours
        self._co_confidence_cap = co_confidence_cap

    # ═══════════════════════════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════════════════════════

    async def run_full_project_scan(
        self,
        project_id: UUID,
        organization_id: UUID,
        llm_backend: Any | None = None,
    ) -> dict[str, int]:
        """Run all detection algorithms and persist observations.

        Orchestrates the full pipeline:
        1. Detect co-occurrences
        2. Detect temporal gaps
        3. Detect behavioral patterns
        4. Generate descriptions (LLM or template)
        5. Persist via ObservationRepository.upsert()

        Args:
            project_id: Project scope.
            organization_id: Organization scope (for RLS).
            llm_backend: Optional LLM backend for content generation.
                If ``None``, template-based descriptions are used.

        Returns:
            A dict with counts per observation type:
            ``{"co_occurrence": N, "temporal_pattern": M, ...}``
        """
        counts: dict[str, int] = {}

        # ── 1. Co-occurrence ──────────────────────────────────────────────────
        co_pairs = await self.detect_co_occurrences(project_id, organization_id)
        count = await self._persist_co_occurrences(
            co_pairs, project_id, organization_id, llm_backend,
        )
        counts[str(ObservationType.CO_OCCURRENCE)] = count
        logger.info(
            "observation_service.co_occurrences_detected",
            project_id=str(project_id),
            count=count,
        )

        # ── 2. Temporal gaps ──────────────────────────────────────────────────
        temporal_patterns = await self.detect_temporal_gaps(
            project_id, organization_id,
        )
        count = await self._persist_temporal_patterns(
            temporal_patterns, project_id, organization_id, llm_backend,
        )
        counts[str(ObservationType.TEMPORAL_PATTERN)] = count
        logger.info(
            "observation_service.temporal_patterns_detected",
            project_id=str(project_id),
            count=count,
        )

        # ── 3. Behavioral patterns ────────────────────────────────────────────
        behavioral_patterns = await self.detect_behavioral_patterns(
            project_id, organization_id,
        )
        count = await self._persist_behavioral_patterns(
            behavioral_patterns, project_id, organization_id, llm_backend,
        )
        counts[str(ObservationType.BEHAVIORAL_PATTERN)] = count
        logger.info(
            "observation_service.behavioral_patterns_detected",
            project_id=str(project_id),
            count=count,
        )

        return counts

    # ═══════════════════════════════════════════════════════════════════════════
    # Detection algorithms
    # ═══════════════════════════════════════════════════════════════════════════

    async def detect_co_occurrences(
        self,
        project_id: UUID,
        organization_id: UUID | None = None,
    ) -> list[CoOccurrencePattern]:
        """Find entity pairs that co-occur above the frequency threshold.

        Queries ``graph_episode_entities`` for pairs of entities that
        appear together in the same episode.  Returns all pairs whose
        co-occurrence count meets or exceeds ``min_co_count``.

        Args:
            project_id: Project scope.
            organization_id: Optional — reserved for future RLS enforcement
                at the query level (currently handled by SQLAlchemy session).

        Returns:
            A list of ``CoOccurrencePattern`` instances, ordered by
            co-occurrence count descending.
        """
        # Get total episode count for the project (denominator for ratio)
        total_episodes = await self._repo.get_episode_count(project_id)
        if total_episodes == 0:
            return []

        # Find co-occurring pairs via repository
        pair_rows = await self._repo.get_co_occurring_pairs(
            project_id, self._min_co_count,
        )

        patterns: list[CoOccurrencePattern] = []
        for row in pair_rows:
            # Fetch supporting relationship IDs between this pair
            rel_ids = await self._repo.get_relationship_ids_between(
                project_id, row["entity_a_id"], row["entity_b_id"],
            )
            patterns.append(CoOccurrencePattern(
                entity_a_id=row["entity_a_id"],
                entity_a_name=row["entity_a_name"],
                entity_b_id=row["entity_b_id"],
                entity_b_name=row["entity_b_name"],
                co_count=row["co_count"],
                total_episodes=total_episodes,
                relationship_ids=rel_ids,
            ))

        return patterns

    async def detect_temporal_gaps(
        self,
        project_id: UUID,
        organization_id: UUID | None = None,
    ) -> list[TemporalGapPattern]:
        """Analyze temporal gaps between entity appearances.

        For each entity with sufficient episode appearances, calculates
        gaps between consecutive appearances and classifies the pattern.

        Args:
            project_id: Project scope.
            organization_id: Reserved for future use.

        Returns:
            A list of ``TemporalGapPattern`` instances, one per entity
            that has enough appearances to analyze.
        """
        # Get entity → episode timestamps via repository
        rows = await self._repo.get_entity_timestamps(project_id)

        # Group timestamps by entity in Python
        entity_timestamps: dict[UUID, tuple[str, list[datetime]]] = {}
        for row in rows:
            eid = row["entity_id"]
            if eid not in entity_timestamps:
                entity_timestamps[eid] = (row["entity_name"], [])
            entity_timestamps[eid][1].append(row["episode_created_at"])

        patterns: list[TemporalGapPattern] = []
        for entity_id, (entity_name, timestamps) in entity_timestamps.items():
            if len(timestamps) < self._min_appearances_for_temporal:
                continue

            # Calculate gaps in hours between consecutive appearances
            gaps = [
                (timestamps[i + 1] - timestamps[i]).total_seconds() / 3600
                for i in range(len(timestamps) - 1)
            ]
            if not gaps:
                continue

            # Filter out gaps below minimum threshold (near-simultaneous)
            significant_gaps = [g for g in gaps if g >= self._min_gap_hours]
            if not significant_gaps:
                continue

            mean_gap = sum(significant_gaps) / len(significant_gaps)
            stddev_gap = _stddev(significant_gaps, mean_gap)
            min_gap = min(significant_gaps)
            max_gap = max(significant_gaps)
            span = (timestamps[-1] - timestamps[0]).total_seconds() / 86400
            cv = stddev_gap / mean_gap if mean_gap > 0 else float("inf")

            # Classify the pattern
            if cv < 0.25 and len(significant_gaps) >= 2:
                pattern_type = "periodic"
            elif _is_monotonic(significant_gaps, increasing=True):
                pattern_type = "widening"
            elif _is_monotonic(significant_gaps, increasing=False):
                pattern_type = "narrowing"
            elif _is_burst(gaps, min_gap_threshold=0.5, max_burst_window=6):
                pattern_type = "burst"
            else:
                pattern_type = "irregular"

            patterns.append(TemporalGapPattern(
                entity_id=entity_id,
                entity_name=entity_name,
                appearance_count=len(timestamps),
                pattern_type=pattern_type,
                mean_gap_hours=round(mean_gap, 1),
                stddev_gap_hours=round(stddev_gap, 1),
                min_gap_hours=round(min_gap, 1),
                max_gap_hours=round(max_gap, 1),
                span_days=round(span, 1),
            ))

        # Sort by most regular pattern first (periodic first, then by mean gap)
        _PATTERN_SORT_ORDER = {
            "periodic": 0, "narrowing": 1, "widening": 2,
            "burst": 3, "irregular": 4,
        }
        patterns.sort(key=lambda p: (_PATTERN_SORT_ORDER.get(p.pattern_type, 99),
                                     p.mean_gap_hours))

        return patterns

    async def detect_behavioral_patterns(
        self,
        project_id: UUID,
        organization_id: UUID | None = None,
    ) -> list[BehavioralPattern]:
        """Detect behavioral patterns from extracted facts.

        Finds entities that have facts with notable predicate distributions
        (e.g., the same predicate appears with high frequency, suggesting
        a consistent behavior).

        Args:
            project_id: Project scope.
            organization_id: Reserved for future use.

        Returns:
            A list of ``BehavioralPattern`` instances, one per entity
            with notable fact-predicate patterns.
        """
        # Get predicate frequency for each entity via repository
        rows = await self._repo.get_fact_predicate_counts(project_id)

        # Aggregate by entity
        entity_data: dict[UUID, dict[str, Any]] = {}
        for row in rows:
            eid = row["entity_id"]
            if eid not in entity_data:
                entity_data[eid] = {
                    "entity_name": row["entity_name"],
                    "entity_type": row["entity_type"],
                    "predicates": {},
                    "total_facts": row["total_facts"],
                }
            entity_data[eid]["predicates"][row["predicate"]] = row["predicate_count"]

        patterns: list[BehavioralPattern] = []
        for entity_id, data in entity_data.items():
            # Sort predicates by count descending
            sorted_preds = dict(
                sorted(data["predicates"].items(),
                       key=lambda x: x[1], reverse=True)
            )
            # Build hint for description
            top_pred = next(iter(sorted_preds.items()), (None, 0))
            if top_pred[0]:
                hint = (
                    f"Entity '{data['entity_name']}' frequently exhibits "
                    f"predicate '{top_pred[0]}' ({top_pred[1]} occurrences "
                    f"out of {data['total_facts']} total facts)."
                )
            else:
                hint = (
                    f"Entity '{data['entity_name']}' has no notable "
                    f"predicate patterns."
                )

            patterns.append(BehavioralPattern(
                entity_id=entity_id,
                entity_name=data["entity_name"],
                entity_type=data["entity_type"],
                frequent_predicates=sorted_preds,
                total_facts=data["total_facts"],
                description_hint=hint,
            ))

        # Sort by total_facts descending (most facts first)
        patterns.sort(key=lambda p: p.total_facts, reverse=True)
        return patterns

    # ═══════════════════════════════════════════════════════════════════════════
    # Description generation (LLM-optional)
    # ═══════════════════════════════════════════════════════════════════════════

    def build_co_occurrence_description(
        self,
        pattern: CoOccurrencePattern,
        llm_content: str | None = None,
    ) -> str:
        """Generate a description for a co-occurrence observation.

        If ``llm_content`` is provided (from an LLM call), it is used.
        Otherwise, a template-based description is built.

        Args:
            pattern: The detected co-occurrence pattern.
            llm_content: Optional LLM-generated content.

        Returns:
            A natural-language description string.
        """
        if llm_content:
            return llm_content
        ratio = round(pattern.co_count / pattern.total_episodes * 100)
        return (
            f"'{pattern.entity_a_name}' appears alongside "
            f"'{pattern.entity_b_name}' in {pattern.co_count} out of "
            f"{pattern.total_episodes} episodes ({ratio}% co-occurrence rate)."
        )

    def build_temporal_description(
        self,
        pattern: TemporalGapPattern,
        llm_content: str | None = None,
    ) -> str:
        """Generate a description for a temporal pattern observation.

        Args:
            pattern: The detected temporal gap pattern.
            llm_content: Optional LLM-generated content.

        Returns:
            A natural-language description string.
        """
        if llm_content:
            return llm_content

        pattern_labels = {
            "periodic": "regular intervals",
            "widening": "increasing gaps",
            "narrowing": "decreasing gaps",
            "burst": "clustered appearances with long gaps between",
            "irregular": "irregular intervals",
        }
        label = pattern_labels.get(pattern.pattern_type, pattern.pattern_type)

        return (
            f"'{pattern.entity_name}' appears at {label} "
            f"(mean gap: {pattern.mean_gap_hours:.1f}h, "
            f"span: {pattern.span_days:.1f}d, "
            f"{pattern.appearance_count} appearances)."
        )

    def build_behavioral_description(
        self,
        pattern: BehavioralPattern,
        llm_content: str | None = None,
    ) -> str:
        """Generate a description for a behavioral pattern observation.

        Args:
            pattern: The detected behavioral pattern.
            llm_content: Optional LLM-generated content.

        Returns:
            A natural-language description string.
        """
        if llm_content:
            return llm_content

        if not pattern.frequent_predicates:
            return (
                f"'{pattern.entity_name}' has no notable behavior patterns "
                f"detected ({pattern.total_facts} facts analyzed)."
            )

        top_pred, top_count = next(iter(pattern.frequent_predicates.items()))
        return (
            f"'{pattern.entity_name}' most frequently exhibits "
            f"the predicate '{top_pred}' ({top_count} out of "
            f"{pattern.total_facts} facts). "
            f"Additional predicates: "
            f"{', '.join(f'{p}({c})' for p, c in list(pattern.frequent_predicates.items())[1:4])}."
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # Persistence helpers
    # ═══════════════════════════════════════════════════════════════════════════

    async def _persist_co_occurrences(
        self,
        patterns: list[CoOccurrencePattern],
        project_id: UUID,
        organization_id: UUID,
        llm_backend: Any | None,
    ) -> int:
        """Persist co-occurrence observations — two per pair (one per direction).

        Each pair produces two observations:
        - (entity_a, co_occurrence, entity_b)
        - (entity_b, co_occurrence, entity_a)

        Args:
            patterns: Detected co-occurrence patterns.
            project_id: Project scope.
            organization_id: Organization scope.
            llm_backend: Optional LLM backend.

        Returns:
            Number of observations persisted (2 × number of pairs).
        """
        now = datetime.now(timezone.utc)
        persisted = 0

        for pair in patterns:
            # Direction A → B
            desc_a = self.build_co_occurrence_description(pair)
            await self._repo.upsert(
                organization_id=organization_id,
                project_id=project_id,
                subject_entity_id=pair.entity_a_id,
                related_entity_id=pair.entity_b_id,
                observation_type=str(ObservationType.CO_OCCURRENCE),
                content=desc_a,
                confidence=min(pair.co_count / self._co_confidence_cap, 1.0),
                supporting_relationship_ids=pair.relationship_ids or None,
                valid_from=now,
            )
            persisted += 1

            # Direction B → A
            # Swap names for the mirrored description
            swapped_pair = CoOccurrencePattern(
                entity_a_id=pair.entity_b_id,
                entity_a_name=pair.entity_b_name,
                entity_b_id=pair.entity_a_id,
                entity_b_name=pair.entity_a_name,
                co_count=pair.co_count,
                total_episodes=pair.total_episodes,
                relationship_ids=pair.relationship_ids,
            )
            desc_b = self.build_co_occurrence_description(swapped_pair)
            await self._repo.upsert(
                organization_id=organization_id,
                project_id=project_id,
                subject_entity_id=pair.entity_b_id,
                related_entity_id=pair.entity_a_id,
                observation_type=str(ObservationType.CO_OCCURRENCE),
                content=desc_b,
                confidence=min(pair.co_count / self._co_confidence_cap, 1.0),
                supporting_relationship_ids=pair.relationship_ids or None,
                valid_from=now,
            )
            persisted += 1

        return persisted

    async def _persist_temporal_patterns(
        self,
        patterns: list[TemporalGapPattern],
        project_id: UUID,
        organization_id: UUID,
        llm_backend: Any | None,
    ) -> int:
        """Persist temporal pattern observations.

        Each entity gets one entity-level observation (``related_entity_id``
        is NULL).

        Args:
            patterns: Detected temporal gap patterns.
            project_id: Project scope.
            organization_id: Organization scope.
            llm_backend: Optional LLM backend.

        Returns:
            Number of observations persisted.
        """
        now = datetime.now(timezone.utc)
        persisted = 0

        for pattern in patterns:
            desc = self.build_temporal_description(pattern)
            # Confidence based on pattern clarity: periodic/narrowing higher
            confidence_map = {
                "periodic": 0.85, "narrowing": 0.75, "widening": 0.70,
                "burst": 0.65, "irregular": 0.40,
            }
            confidence = confidence_map.get(pattern.pattern_type, 0.5)

            await self._repo.upsert(
                organization_id=organization_id,
                project_id=project_id,
                subject_entity_id=pattern.entity_id,
                related_entity_id=None,  # entity-level observation
                observation_type=str(ObservationType.TEMPORAL_PATTERN),
                content=desc,
                confidence=confidence,
                valid_from=now,
            )
            persisted += 1

        return persisted

    async def _persist_behavioral_patterns(
        self,
        patterns: list[BehavioralPattern],
        project_id: UUID,
        organization_id: UUID,
        llm_backend: Any | None,
    ) -> int:
        """Persist behavioral pattern observations.

        Each entity gets one entity-level observation (``related_entity_id``
        is NULL).

        Args:
            patterns: Detected behavioral patterns.
            project_id: Project scope.
            organization_id: Organization scope.
            llm_backend: Optional LLM backend.

        Returns:
            Number of observations persisted.
        """
        now = datetime.now(timezone.utc)
        persisted = 0

        for pattern in patterns:
            desc = self.build_behavioral_description(pattern)
            # Confidence based on how dominant the top predicate is
            if pattern.frequent_predicates:
                top_count = next(iter(pattern.frequent_predicates.values()))
                confidence = min(top_count / max(pattern.total_facts, 1) * 1.5, 0.95)
            else:
                confidence = 0.2

            await self._repo.upsert(
                organization_id=organization_id,
                project_id=project_id,
                subject_entity_id=pattern.entity_id,
                related_entity_id=None,  # entity-level observation
                observation_type=str(ObservationType.BEHAVIORAL_PATTERN),
                content=desc,
                confidence=round(confidence, 2),
                valid_from=now,
            )
            persisted += 1

        return persisted

    # ═══════════════════════════════════════════════════════════════════════════
    # Internal helpers
    # ═══════════════════════════════════════════════════════════════════════════

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
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return variance ** 0.5


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
        if increasing and gaps[i + 1] > gaps[i]:
            consistent += 1
        elif not increasing and gaps[i + 1] < gaps[i]:
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
