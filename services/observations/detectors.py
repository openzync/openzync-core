"""Observation detectors — graph-topology detection algorithms."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import GraphBackendUnavailableError
from packages.graph_backend.interface import GraphBackend
from services.observations.patterns import (
    BehavioralPattern,
    CoOccurrencePattern,
    TemporalGapPattern,
    _is_burst,
    _is_monotonic,
    _stddev,
)

logger = structlog.get_logger(__name__)


class DetectionMixin:
    """Composed into :class:`ObservationService`."""

    _backend: Any | None
    _db: AsyncSession | None
    _min_co_count: int
    _min_appearances_for_temporal: int
    _min_gap_hours: float
    _co_confidence_cap: int

    # ═══════════════════════════════════════════════════════════════════════════
    # Detection algorithms
    # ═══════════════════════════════════════════════════════════════════════════
    async def detect_co_occurrences(
        self,
        project_id: UUID,
        organization_id: UUID | None = None,
    ) -> list[CoOccurrencePattern]:
        """Find entity pairs that co-occur above the frequency threshold.

        Delegates to ``backend.get_co_occurring_entity_pairs()`` for the
        graph-topology query.  Returns all pairs whose co-occurrence count
        meets or exceeds ``min_co_count``.

        Args:
            project_id: Project scope.
            organization_id: Organization scope — required for backend
                calls (RLS enforcement).

        Returns:
            A list of ``CoOccurrencePattern`` instances, ordered by
            co-occurrence count descending.

        Raises:
            GraphBackendUnavailableError: If the graph backend is not
                configured.
        """
        self._assert_backend()
        assert self._backend is not None  # mypy type narrowing
        if organization_id is None:
            raise ValueError("organization_id is required when using GraphBackend")

        # Get total episode count for the project (denominator for ratio)
        total_episodes = await self._get_episode_count(project_id, organization_id)
        if total_episodes == 0:
            return []

        # Find co-occurring pairs via graph backend
        pair_rows = await self._backend.get_co_occurring_entity_pairs(
            org_id=organization_id,
            project_id=project_id,
            min_co_count=self._min_co_count,
        )
        if not pair_rows:
            return []

        # Single batch call for supporting evidence — one logical round
        # trip instead of a sequential query per pair.
        pairs = [
            (
                UUID(row["entity_a_id"])
                if isinstance(row["entity_a_id"], str)
                else row["entity_a_id"],
                UUID(row["entity_b_id"])
                if isinstance(row["entity_b_id"], str)
                else row["entity_b_id"],
            )
            for row in pair_rows
        ]
        evidence = await self._backend.get_relationship_ids_between_many(
            org_id=organization_id,
            project_id=project_id,
            pairs=pairs,
        )

        patterns: list[CoOccurrencePattern] = []
        for row, (a_id, b_id) in zip(pair_rows, pairs, strict=True):
            patterns.append(CoOccurrencePattern(
                entity_a_id=a_id,
                entity_a_name=row["entity_a_name"],
                entity_b_id=b_id,
                entity_b_name=row["entity_b_name"],
                co_count=row["co_count"],
                total_episodes=total_episodes,
                relationship_ids=evidence.get((a_id, b_id), []),
            ))

        return patterns

    async def detect_temporal_gaps(
        self,
        project_id: UUID,
        organization_id: UUID | None = None,
    ) -> list[TemporalGapPattern]:
        """Analyze temporal gaps between entity appearances.

        For each entity with sufficient episode appearances, fetches
        timestamps via ``backend.get_entity_appearance_timestamps()``,
        calculates gaps between consecutive appearances, and classifies
        the pattern.

        Args:
            project_id: Project scope.
            organization_id: Organization scope — required for backend
                calls (RLS enforcement).

        Returns:
            A list of ``TemporalGapPattern`` instances, one per entity
            that has enough appearances to analyze.

        Raises:
            GraphBackendUnavailableError: If the graph backend is not
                configured.
        """
        self._assert_backend()
        assert self._backend is not None  # mypy type narrowing
        backend: GraphBackend = self._backend  # local var for closure type narrowing
        if organization_id is None:
            raise ValueError("organization_id is required when using GraphBackend")

        # Get all entities for the project
        entities = await backend.get_all_entities(
            org_id=organization_id,
            project_id=project_id,
        )

        # Fetch timestamps per entity in parallel
        async def _fetch_timestamps(
            entity: dict[str, Any],
        ) -> tuple[UUID, str, list[datetime]] | None:
            eid = UUID(entity["id"]) if isinstance(entity["id"], str) else entity["id"]
            timestamps = await backend.get_entity_appearance_timestamps(
                org_id=organization_id,
                project_id=project_id,
                entity_id=eid,
            )
            if timestamps:
                return (eid, entity["name"], list(timestamps))
            return None

        results = await asyncio.gather(
            *[_fetch_timestamps(e) for e in entities],
        )

        # Build entity → timestamps map
        entity_timestamps: dict[UUID, tuple[str, list[datetime]]] = {}
        for result in results:
            if result is not None:
                eid, name, timestamps = result
                entity_timestamps[eid] = (name, timestamps)

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
        a consistent behavior).  Queries the ``facts`` table directly via
        raw SQL — this is not a graph operation.

        Args:
            project_id: Project scope.
            organization_id: Reserved for future use.

        Returns:
            A list of ``BehavioralPattern`` instances, one per entity
            with notable fact-predicate patterns.
        """
        # Get predicate frequency for each entity via direct DB query
        rows = await self._get_fact_predicate_counts(project_id, organization_id)

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
    async def _get_episode_count(
        self,
        project_id: UUID,
        organization_id: UUID,
    ) -> int:
        """Get total distinct episode count for a project via graph backend.

        Delegates to ``GraphBackend.get_total_entity_linked_episode_count``.

        Args:
            project_id: Project scope.
            organization_id: Organisation scope (for RLS enforcement).

        Returns:
            Number of distinct episodes with linked entities.

        Raises:
            GraphBackendUnavailableError: If the backend is unavailable.
        """
        if self._backend is None:
            raise GraphBackendUnavailableError(
                "Graph backend is not available. "
                "ObservationService requires a graph_backend instance."
            )
        return await self._backend.get_total_entity_linked_episode_count(
            org_id=organization_id,
            project_id=project_id,
        )
