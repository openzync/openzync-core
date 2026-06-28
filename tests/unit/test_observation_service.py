"""Unit tests for graph-topology pattern detection and description generation.

Tests :class:`services.observation_service.ObservationService` in isolation —
mocks the ``AsyncSession`` (DB) and ``ObservationRepository``.  All pattern
detection algorithms are tested with deterministic input data.

No real database, no network, no LLM calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from services.observation_service import (
    CoOccurrencePattern,
    ObservationService,
    TemporalGapPattern,
    BehavioralPattern,
    _is_burst,
    _is_monotonic,
    _stddev,
)


# ── Well-known test IDs ───────────────────────────────────────────────────────

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
ENTITY_A_ID = UUID("00000000-0000-0000-0000-00000000000a")
ENTITY_B_ID = UUID("00000000-0000-0000-0000-00000000000b")
ENTITY_C_ID = UUID("00000000-0000-0000-0000-00000000000c")
ENTITY_D_ID = UUID("00000000-0000-0000-0000-00000000000d")


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create a mocked async SQLAlchemy session."""
    return AsyncMock()


@pytest.fixture
def mock_repo() -> AsyncMock:
    """Create a mocked ObservationRepository."""
    return AsyncMock()


@pytest.fixture
def service(
    mock_db: AsyncMock,
    mock_repo: AsyncMock,
) -> ObservationService:
    """Create an ObservationService with mocked DB and repo.

    Uses default thresholds (min_co_count=3,
    min_appearances_for_temporal=3).
    """
    return ObservationService(
        db=mock_db,
        repo=mock_repo,
        min_co_count=3,
        min_appearances_for_temporal=3,
        min_gap_hours=1.0,
        co_confidence_cap=20,
    )


# ── Data builders ─────────────────────────────────────────────────────────────


def _mock_execute_result(rows: list[dict]) -> MagicMock:
    """Build a mock result for ``db.execute()``.

    The mock's ``.mappings().all()`` returns the given list of dicts.
    ``.one_or_none()`` returns the first (or None).
    ``.all()`` returns a list of :class:`Row`-like tuples.
    """
    mock_result = MagicMock()
    # For .one_or_none()
    mock_result.one_or_none.return_value = rows[0] if rows else None
    # For .one()
    mock_result.one.return_value = rows[0] if rows else None
    # For .mappings().all()
    mappings_mock = MagicMock()
    mappings_mock.all.return_value = rows
    mock_result.mappings.return_value = mappings_mock
    # For .all() (returns raw tuples)
    mock_result.all.return_value = [tuple(r.values()) for r in rows]
    return mock_result


def _co_row(
    entity_a_id: UUID = ENTITY_A_ID,
    entity_a_name: str = "EntityA",
    entity_b_id: UUID = ENTITY_B_ID,
    entity_b_name: str = "EntityB",
    co_count: int = 5,
) -> dict:
    return {
        "entity_a_id": entity_a_id,
        "entity_a_name": entity_a_name,
        "entity_b_id": entity_b_id,
        "entity_b_name": entity_b_name,
        "co_count": co_count,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Co-occurrence detection
# ═══════════════════════════════════════════════════════════════════════════════


class TestDetectCoOccurrences:
    """Tests for ``ObservationService.detect_co_occurrences()``."""

    async def test_empty_project_returns_empty_list(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """No graph_episode_entities → empty list."""
        mock_db.execute.return_value = _mock_execute_result([
            {"total": 0},
        ])
        result = await service.detect_co_occurrences(PROJECT_ID)
        assert result == []

    async def test_single_pair_above_threshold(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """One pair with 5 co-occurrences (threshold=3) → one pattern."""
        mock_db.execute.side_effect = [
            _mock_execute_result([{"total": 10}]),  # total episodes
            _mock_execute_result([_co_row(co_count=5)]),  # pairs
        ]
        mock_db.execute.return_value = _mock_execute_result([])  # rel IDs

        patterns = await service.detect_co_occurrences(PROJECT_ID)
        assert len(patterns) == 1
        assert patterns[0].co_count == 5
        assert patterns[0].total_episodes == 10
        assert patterns[0].entity_a_id == ENTITY_A_ID
        assert patterns[0].entity_b_id == ENTITY_B_ID

    async def test_below_threshold_excluded(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Pair with 2 co-occurrences (threshold=3) → excluded."""
        mock_db.execute.side_effect = [
            _mock_execute_result([{"total": 10}]),
            _mock_execute_result([]),  # no pairs above threshold
        ]
        patterns = await service.detect_co_occurrences(PROJECT_ID)
        assert patterns == []

    async def test_multiple_pairs_sorted_by_count(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Multiple pairs returned in descending co_count order."""
        mock_db.execute.side_effect = [
            _mock_execute_result([{"total": 20}]),
            _mock_execute_result([
                _co_row(ENTITY_A_ID, "EntityA", ENTITY_B_ID, "EntityB", co_count=10),
                _co_row(ENTITY_A_ID, "EntityA", ENTITY_C_ID, "EntityC", co_count=5),
                _co_row(ENTITY_B_ID, "EntityB", ENTITY_C_ID, "EntityC", co_count=3),
            ]),
        ]
        # Return empty rel IDs for all three queries
        mock_db.execute.return_value = _mock_execute_result([])

        patterns = await service.detect_co_occurrences(PROJECT_ID)
        assert len(patterns) == 3
        assert patterns[0].co_count == 10
        assert patterns[1].co_count == 5
        assert patterns[2].co_count == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Temporal gap analysis
# ═══════════════════════════════════════════════════════════════════════════════


class TestDetectTemporalGaps:
    """Tests for ``ObservationService.detect_temporal_gaps()``."""

    def _ts(self, days_ago: float) -> datetime:
        """Build a UTC timestamp relative to now."""
        return datetime.now(timezone.utc) - timedelta(days=days_ago)

    async def test_single_appearance_no_analysis(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Entity with 1 appearance is below min_appearances=3."""
        mock_db.execute.return_value = _mock_execute_result([
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(1)},
        ])
        patterns = await service.detect_temporal_gaps(PROJECT_ID)
        assert patterns == []

    async def test_periodic_pattern_detected(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Entity with 4 appearances at ~7-day intervals → 'periodic'."""
        mock_db.execute.return_value = _mock_execute_result([
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(21)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(14)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(7)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(0)},
        ])
        patterns = await service.detect_temporal_gaps(PROJECT_ID)
        assert len(patterns) == 1
        assert patterns[0].pattern_type == "periodic"
        assert patterns[0].appearance_count == 4
        # 7 days = 168 hours
        assert abs(patterns[0].mean_gap_hours - 168) < 5

    async def test_widening_gaps_detected(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Entity with gaps 1d, 3d, 7d → 'widening'."""
        mock_db.execute.return_value = _mock_execute_result([
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(11)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(10)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(7)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(0)},
        ])
        patterns = await service.detect_temporal_gaps(PROJECT_ID)
        assert len(patterns) == 1
        assert patterns[0].pattern_type == "widening"

    async def test_narrowing_gaps_detected(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Entity with gaps 7d, 3d, 1d → 'narrowing'."""
        mock_db.execute.return_value = _mock_execute_result([
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(11)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(4)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(1)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(0)},
        ])
        patterns = await service.detect_temporal_gaps(PROJECT_ID)
        assert len(patterns) == 1
        assert patterns[0].pattern_type == "narrowing"

    async def test_irregular_pattern_fallback(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Random gaps with no clear pattern → 'irregular'."""
        mock_db.execute.return_value = _mock_execute_result([
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(30)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(15)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(14)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(1)},
        ])
        patterns = await service.detect_temporal_gaps(PROJECT_ID)
        assert len(patterns) == 1
        # 30-15=15d, 15-14=1d, 14-1=13d — not a clear monotonic/periodic pattern
        assert patterns[0].pattern_type in ("irregular", "burst")

    async def test_multiple_entities(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Two entities with different patterns are both returned."""
        mock_db.execute.return_value = _mock_execute_result([
            # Entity A: periodic (7-day gaps)
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(21)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(14)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(7)},
            {"entity_id": ENTITY_A_ID, "entity_name": "EntityA",
             "episode_created_at": self._ts(0)},
            # Entity B: widening
            {"entity_id": ENTITY_B_ID, "entity_name": "EntityB",
             "episode_created_at": self._ts(10)},
            {"entity_id": ENTITY_B_ID, "entity_name": "EntityB",
             "episode_created_at": self._ts(9)},
            {"entity_id": ENTITY_B_ID, "entity_name": "EntityB",
             "episode_created_at": self._ts(5)},
            {"entity_id": ENTITY_B_ID, "entity_name": "EntityB",
             "episode_created_at": self._ts(0)},
        ])
        patterns = await service.detect_temporal_gaps(PROJECT_ID)
        assert len(patterns) == 2
        pattern_types = {p.entity_id: p.pattern_type for p in patterns}
        assert pattern_types[ENTITY_A_ID] == "periodic"
        assert pattern_types[ENTITY_B_ID] == "widening"


# ═══════════════════════════════════════════════════════════════════════════════
# Behavioral pattern detection
# ═══════════════════════════════════════════════════════════════════════════════


class TestDetectBehavioralPatterns:
    """Tests for ``ObservationService.detect_behavioral_patterns()``."""

    async def test_no_facts_returns_empty(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """No facts for the project → empty list."""
        mock_db.execute.return_value = _mock_execute_result([])
        patterns = await service.detect_behavioral_patterns(PROJECT_ID)
        assert patterns == []

    async def test_single_entity_with_frequent_predicate(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Entity with 5 'upgrades' predicates → pattern detected."""
        mock_db.execute.return_value = _mock_execute_result([
            {
                "entity_id": ENTITY_A_ID,
                "entity_name": "EntityA",
                "entity_type": "Person",
                "predicate": "upgrades",
                "predicate_count": 5,
                "total_facts": 8,
            },
        ])
        patterns = await service.detect_behavioral_patterns(PROJECT_ID)
        assert len(patterns) == 1
        assert patterns[0].entity_id == ENTITY_A_ID
        assert patterns[0].frequent_predicates == {"upgrades": 5}
        assert patterns[0].total_facts == 8

    async def test_multiple_predicates_sorted(
        self,
        service: ObservationService,
        mock_db: AsyncMock,
    ) -> None:
        """Entity with multiple predicates → sorted by count descending."""
        mock_db.execute.return_value = _mock_execute_result([
            {
                "entity_id": ENTITY_A_ID,
                "entity_name": "EntityA",
                "entity_type": "Person",
                "predicate": "upgrades",
                "predicate_count": 5,
                "total_facts": 10,
            },
            {
                "entity_id": ENTITY_A_ID,
                "entity_name": "EntityA",
                "entity_type": "Person",
                "predicate": "mentions",
                "predicate_count": 3,
                "total_facts": 10,
            },
            {
                "entity_id": ENTITY_A_ID,
                "entity_name": "EntityA",
                "entity_type": "Person",
                "predicate": "references",
                "predicate_count": 2,
                "total_facts": 10,
            },
        ])
        patterns = await service.detect_behavioral_patterns(PROJECT_ID)
        assert len(patterns) == 1
        preds = list(patterns[0].frequent_predicates.items())
        assert preds[0] == ("upgrades", 5)
        assert preds[1] == ("mentions", 3)
        assert preds[2] == ("references", 2)


# ═══════════════════════════════════════════════════════════════════════════════
# Description generation
# ═══════════════════════════════════════════════════════════════════════════════


class TestDescriptionGeneration:
    """Tests for template-based description fallback."""

    def test_co_occurrence_description(self, service: ObservationService) -> None:
        """Template description includes both entity names and count."""
        pattern = CoOccurrencePattern(
            entity_a_id=ENTITY_A_ID,
            entity_a_name="Alice",
            entity_b_id=ENTITY_B_ID,
            entity_b_name="Bob",
            co_count=7,
            total_episodes=20,
        )
        desc = service.build_co_occurrence_description(pattern)
        assert "Alice" in desc
        assert "Bob" in desc
        assert "7" in desc
        assert "20" in desc

    def test_co_occurrence_llm_content_used(self, service: ObservationService) -> None:
        """When LLM content is provided, it is used instead of template."""
        pattern = CoOccurrencePattern(
            entity_a_id=ENTITY_A_ID,
            entity_a_name="Alice",
            entity_b_id=ENTITY_B_ID,
            entity_b_name="Bob",
            co_count=7,
            total_episodes=20,
        )
        llm_text = "Alice and Bob collaborate frequently on support tickets."
        desc = service.build_co_occurrence_description(pattern, llm_content=llm_text)
        assert desc == llm_text

    def test_temporal_periodic_description(self, service: ObservationService) -> None:
        """Template description for periodic pattern includes cadence."""
        pattern = TemporalGapPattern(
            entity_id=ENTITY_A_ID,
            entity_name="Alice",
            appearance_count=5,
            pattern_type="periodic",
            mean_gap_hours=168.0,
            stddev_gap_hours=5.0,
            min_gap_hours=160.0,
            max_gap_hours=175.0,
            span_days=28.0,
        )
        desc = service.build_temporal_description(pattern)
        assert "Alice" in desc
        assert "regular intervals" in desc
        assert "168.0" in desc

    def test_behavioral_description(self, service: ObservationService) -> None:
        """Template description mentions top predicate."""
        pattern = BehavioralPattern(
            entity_id=ENTITY_A_ID,
            entity_name="Alice",
            entity_type="Person",
            frequent_predicates={"upgrades": 5, "mentions": 3},
            total_facts=8,
            description_hint="Alice frequently upgrades.",
        )
        desc = service.build_behavioral_description(pattern)
        assert "Alice" in desc
        assert "upgrades" in desc
        assert "5" in desc


# ═══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════════


class TestStddev:
    """Tests for the ``_stddev`` helper."""

    def test_single_value(self) -> None:
        assert _stddev([5.0], 5.0) == 0.0

    def test_identical_values(self) -> None:
        assert _stddev([3.0, 3.0, 3.0], 3.0) == 0.0

    def test_known_values(self) -> None:
        # Population stddev of [1, 2, 3, 4] = sqrt(1.25) ≈ 1.118
        result = _stddev([1.0, 2.0, 3.0, 4.0], 2.5)
        assert abs(result - 1.118) < 0.01

    def test_empty_list(self) -> None:
        assert _stddev([], 0.0) == 0.0


class TestIsMonotonic:
    """Tests for the ``_is_monotonic`` helper."""

    def test_clearly_increasing(self) -> None:
        assert _is_monotonic([1.0, 2.0, 4.0, 8.0], increasing=True)

    def test_clearly_decreasing(self) -> None:
        assert _is_monotonic([8.0, 4.0, 2.0, 1.0], increasing=False)

    def test_not_monotonic(self) -> None:
        assert not _is_monotonic([1.0, 8.0, 2.0, 5.0], increasing=True)
        assert not _is_monotonic([5.0, 2.0, 8.0, 1.0], increasing=False)

    def test_short_list_returns_false(self) -> None:
        assert not _is_monotonic([1.0, 2.0], increasing=True)

    def test_tie_not_increasing(self) -> None:
        assert not _is_monotonic([2.0, 2.0, 3.0], increasing=True)


class TestIsBurst:
    """Tests for the ``_is_burst`` helper."""

    def test_burst_pattern_detected(self) -> None:
        """Quick appearances then long gap → burst."""
        gaps = [0.1, 0.2, 0.5, 48.0, 0.3, 0.1]
        assert _is_burst(gaps, min_gap_threshold=0.5, max_burst_window=6)

    def test_regular_gaps_not_burst(self) -> None:
        """Consistent gaps → not burst."""
        gaps = [24.0, 24.0, 24.0, 24.0]
        assert not _is_burst(gaps)

    def test_short_list_not_burst(self) -> None:
        assert not _is_burst([1.0, 2.0])
        assert not _is_burst([])

    def test_all_short_gaps_not_burst(self) -> None:
        """All gaps within burst window but no long gaps → not burst."""
        gaps = [0.1, 0.2, 0.3, 0.4]
        assert not _is_burst(gaps, max_burst_window=6)
