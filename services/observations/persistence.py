"""Observation persistence — observation writes and detection reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import GraphBackendUnavailableError
from models.graph_observation import ObservationType
from services.observations.patterns import (
    BehavioralPattern,
    CoOccurrencePattern,
    TemporalGapPattern,
)

logger = structlog.get_logger(__name__)

class PersistenceMixin:
    """Composed into :class:`ObservationService`."""

    _backend: Any | None
    _db: AsyncSession | None
    _co_confidence_cap: int

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
        assert self._backend is not None  # called from run_full_project_scan which guards
        now = datetime.now(UTC)
        persisted = 0

        for pair in patterns:
            # Direction A → B
            desc_a = self.build_co_occurrence_description(pair)
            await self._backend.upsert_observation(
                org_id=organization_id,
                project_id=project_id,
                subject_entity_id=pair.entity_a_id,
                related_entity_id=pair.entity_b_id,
                observation_type=str(ObservationType.CO_OCCURRENCE),
                content=desc_a,
                confidence=min(pair.co_count / self._co_confidence_cap, 1.0),
                supporting_relationship_ids=pair.relationship_ids if pair.relationship_ids else None,
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
            await self._backend.upsert_observation(
                org_id=organization_id,
                project_id=project_id,
                subject_entity_id=pair.entity_b_id,
                related_entity_id=pair.entity_a_id,
                observation_type=str(ObservationType.CO_OCCURRENCE),
                content=desc_b,
                confidence=min(pair.co_count / self._co_confidence_cap, 1.0),
                supporting_relationship_ids=pair.relationship_ids if pair.relationship_ids else None,
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
        assert self._backend is not None  # called from run_full_project_scan which guards
        now = datetime.now(UTC)
        persisted = 0

        for pattern in patterns:
            desc = self.build_temporal_description(pattern)
            # Confidence based on pattern clarity: periodic/narrowing higher
            confidence_map = {
                "periodic": 0.85, "narrowing": 0.75, "widening": 0.70,
                "burst": 0.65, "irregular": 0.40,
            }
            confidence = confidence_map.get(pattern.pattern_type, 0.5)

            await self._backend.upsert_observation(
                org_id=organization_id,
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
        assert self._backend is not None  # called from run_full_project_scan which guards
        now = datetime.now(UTC)
        persisted = 0

        for pattern in patterns:
            desc = self.build_behavioral_description(pattern)
            # Confidence based on how dominant the top predicate is
            if pattern.frequent_predicates:
                top_count = next(iter(pattern.frequent_predicates.values()))
                confidence = min(top_count / max(pattern.total_facts, 1) * 1.5, 0.95)
            else:
                confidence = 0.2

            await self._backend.upsert_observation(
                org_id=organization_id,
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
    # Internal helpers — non-graph direct DB queries
    # ═══════════════════════════════════════════════════════════════════════════

    def _assert_backend(self) -> None:
        """Raise if the graph backend is not available.

        Raises:
            GraphBackendUnavailableError: If ``self._backend`` is ``None``.
        """
        if self._backend is None:
            raise GraphBackendUnavailableError(
                "Graph backend is not available. "
                "ObservationService requires a graph_backend instance."
            )
    async def _get_fact_predicate_counts(
        self,
        project_id: UUID,
        organization_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Get predicate frequency for each entity (as subject).

        Queries the ``facts`` table for predicate aggregates, then resolves
        entity names via ``GraphBackend.resolve_entity_names``.

        Args:
            project_id: Project scope.
            organization_id: Organisation scope — required for graph backend
                entity-name resolution.

        Returns:
            List of dicts with keys: entity_id, entity_name, entity_type,
            predicate, predicate_count, total_facts.

        Raises:
            GraphBackendUnavailableError: If the backend is unavailable.
        """
        if self._backend is None:
            raise GraphBackendUnavailableError(
                "Graph backend is not available. "
                "ObservationService requires a graph_backend instance."
            )
        if self._db is None:
            raise GraphBackendUnavailableError(
                "An AsyncSession is required for fact predicate queries."
            )
        if organization_id is None:
            raise ValueError(
                "organization_id is required when using GraphBackend for "
                "entity name resolution."
            )

        # Facts live in PostgreSQL — queried via the repository layer
        # (services contain zero SQLAlchemy/text()).
        from repositories.fact_repository import FactRepository  # noqa: PLC0415

        rows = await FactRepository(self._db).get_fact_predicate_counts(
            project_id, organization_id, datetime.now(UTC)
        )

        if not rows:
            return []

        # Extract unique entity IDs (may be str or UUID — handle both)
        def _ensure_uuid(val: object) -> UUID:
            return val if isinstance(val, UUID) else UUID(str(val))

        unique_entity_ids = list({_ensure_uuid(row["entity_id"]) for row in rows})

        # Resolve entity names via graph backend ABC
        entity_names = await self._backend.resolve_entity_names(
            org_id=organization_id,
            project_id=project_id,
            entity_ids=unique_entity_ids,
        )

        # Merge names into results
        output: list[dict[str, Any]] = []
        for row in rows:
            eid_str = str(row["entity_id"])
            name_info = entity_names.get(eid_str, {})
            output.append({
                "entity_id": UUID(eid_str),
                "entity_name": name_info.get("name", "unknown"),
                "entity_type": name_info.get("entity_type", "unknown"),
                "predicate": row["predicate"],
                "predicate_count": row["predicate_count"],
                "total_facts": row["total_facts"],
            })

        return output
