"""Observation service — facade composing focused detection collaborators.

Sub-package layout (one responsibility per module)::

    patterns.py      detection value types (patterns) + gap math
    detectors.py     co-occurrence / temporal / behavioral detection
    descriptions.py  LLM-optional observation text builders
    persistence.py   observation writes + detection data-access reads

``ObservationService`` keeps the original import path and public API
(``run_full_project_scan`` plus the pattern types); workers are
unchanged. ``services/observation_service.py`` re-exports it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ExternalServiceError
from models.graph_observation import ObservationType
from services.observations.descriptions import DescriptionMixin
from services.observations.detectors import DetectionMixin
from services.observations.patterns import (
    _DEFAULT_CO_CONFIDENCE_CAP,
    _DEFAULT_MIN_APPEARANCES_FOR_TEMPORAL,
    _DEFAULT_MIN_CO_COUNT,
    _DEFAULT_MIN_GAP_HOURS,
    BehavioralPattern,
    CoOccurrencePattern,
    TemporalGapPattern,
    _is_burst,
    _is_monotonic,
    _stddev,
)
from services.observations.persistence import PersistenceMixin

if TYPE_CHECKING:
    from packages.graph_backend.interface import GraphBackend

logger = logging.getLogger(__name__)


class ObservationService(DetectionMixin, DescriptionMixin, PersistenceMixin):
    """Graph-topology pattern detection and observation persistence.

    Uses ``GraphBackend`` ABC for all graph operations (co-occurrence
    queries, temporal timestamps, relationship evidence, observation
    persistence).  Non-graph queries (episode counts, fact predicate
    counts) are executed directly via the injected ``AsyncSession``.

    Raises ``GraphBackendUnavailableError`` if the ``graph_backend`` is
    ``None`` when a graph operation is attempted.

    Args:
        graph_backend: A ``GraphBackend`` instance for graph operations.
            If ``None``, methods raise ``GraphBackendUnavailableError``.
        db: An async SQLAlchemy session for non-graph queries (episode
            counts, fact predicate counts).  May be ``None`` only when
            those code paths are never exercised.
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
        graph_backend: GraphBackend | None = None,
        db: AsyncSession | None = None,
        *,
        min_co_count: int = _DEFAULT_MIN_CO_COUNT,
        min_appearances_for_temporal: int = _DEFAULT_MIN_APPEARANCES_FOR_TEMPORAL,
        min_gap_hours: float = _DEFAULT_MIN_GAP_HOURS,
        co_confidence_cap: int = _DEFAULT_CO_CONFIDENCE_CAP,
    ) -> None:
        self._backend = graph_backend
        self._db = db
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
        1. Validate backend availability.
        2. Detect co-occurrences
        3. Detect temporal gaps
        4. Detect behavioral patterns
        5. Generate descriptions (LLM or template)
        6. Persist via backend.upsert_observation()

        Args:
            project_id: Project scope.
            organization_id: Organization scope (for RLS).
            llm_backend: Optional LLM backend for content generation.
                If ``None``, template-based descriptions are used.

        Returns:
            A dict with counts per observation type:
            ``{"co_occurrence": N, "temporal_pattern": M, ...}``

        Raises:
            GraphBackendUnavailableError: If the graph backend is not
                configured.
        """
        self._assert_backend()
        counts: dict[str, int] = {}

        # ── 1. Co-occurrence (fail-open) ─────────────────────────────────────
        # A graph-timeout here must not abort temporal/behavioral
        # detection — record zero and continue.
        try:
            co_pairs = await self.detect_co_occurrences(project_id, organization_id)
            count = await self._persist_co_occurrences(
                co_pairs, project_id, organization_id, llm_backend,
            )
        except ExternalServiceError as exc:
            logger.warning(
                "observation_service.co_occurrences_skipped",
                project_id=str(project_id),
                organization_id=str(organization_id),
                min_co_count=self._min_co_count,
                error=str(exc),
            )
            count = 0
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


__all__ = [
    "BehavioralPattern",
    "CoOccurrencePattern",
    "ObservationService",
    "TemporalGapPattern",
    "_is_burst",
    "_is_monotonic",
    "_stddev",
]
