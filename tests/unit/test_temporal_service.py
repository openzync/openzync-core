"""Unit tests for the temporal validation service (Phase 3a).

Tests the warn-only consistency checks in
:class:`services.temporal_service.TemporalValidationService`.

All tests mock ``FactRepository`` — no database required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from models.fact import Fact
from services.temporal_service import TemporalValidationService


# ── Fixtures ────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_repo() -> AsyncMock:
    """Create a mocked FactRepository."""
    return AsyncMock()


@pytest.fixture
def service(mock_repo: AsyncMock) -> TemporalValidationService:
    """Create a TemporalValidationService with mocked repository."""
    return TemporalValidationService(mock_repo)


def _make_fact(
    fact_id: str = "00000000-0000-0000-0000-000000000001",
    subject: str = "Alice",
    predicate: str = "likes",
    obj: str = "hiking",
    source_episode_id: str = "00000000-0000-0000-0000-000000000010",
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> Fact:
    """Build a Fact ORM instance with the given attributes."""
    f = Fact(
        id=UUID(fact_id),
        project_id=UUID("00000000-0000-0000-0000-000000000020"),
        organization_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000030"),
        content=f"{subject} {predicate} {obj}",
        subject=subject,
        predicate=predicate,
        object=obj,
        subject_type="literal",
        object_type="literal",
        confidence=1.0,
        source_episode_id=UUID(source_episode_id) if source_episode_id else None,
        valid_from=valid_from,
        valid_to=valid_to,
        embedding=[],
    )
    # Simulate server-generated fields
    f.invalid_at = None
    return f


# ── Test class ─────────────────────────────────────────────────────────────────


class TestTemporalValidationService:
    """Warn-only temporal consistency checks."""

    # ── check_project_temporal_consistency ─────────────────────────────────

    async def test_no_overlap_returns_empty(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """No overlapping triples → empty warnings list."""
        mock_repo.get_all_active_for_project.return_value = [
            _make_fact(
                subject="A", predicate="knows", obj="B",
                source_episode_id="00000000-0000-0000-0000-000000000010",
                valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 6, 30, tzinfo=timezone.utc),
            ),
            _make_fact(
                subject="C", predicate="knows", obj="D",
                source_episode_id="00000000-0000-0000-0000-000000000011",
                valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 6, 30, tzinfo=timezone.utc),
            ),
        ]
        warnings = await service.check_project_temporal_consistency(
            UUID("00000000-0000-0000-0000-000000000020"),
        )
        assert warnings == []

    async def test_cross_episode_overlap_detected(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """Same triple, different episodes, overlapping ranges → warning."""
        mock_repo.get_all_active_for_project.return_value = [
            _make_fact(
                fact_id="00000000-0000-0000-0000-000000000001",
                subject="A", predicate="knows", obj="B",
                source_episode_id="00000000-0000-0000-0000-000000000010",
                valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 6, 30, tzinfo=timezone.utc),
            ),
            _make_fact(
                fact_id="00000000-0000-0000-0000-000000000002",
                subject="A", predicate="knows", obj="B",
                source_episode_id="00000000-0000-0000-0000-000000000011",
                valid_from=datetime(2024, 3, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 9, 30, tzinfo=timezone.utc),
            ),
        ]
        warnings = await service.check_project_temporal_consistency(
            UUID("00000000-0000-0000-0000-000000000020"),
        )
        assert len(warnings) == 1
        assert warnings[0]["code"] == "overlap"
        assert "A" in warnings[0]["message"]

    async def test_same_episode_skipped(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """Same triple AND same episode → no warning (exclusion constraint
        already handles this case)."""
        mock_repo.get_all_active_for_project.return_value = [
            _make_fact(
                fact_id="00000000-0000-0000-0000-000000000001",
                subject="A", predicate="knows", obj="B",
                source_episode_id="00000000-0000-0000-0000-000000000010",
                valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 6, 30, tzinfo=timezone.utc),
            ),
            _make_fact(
                fact_id="00000000-0000-0000-0000-000000000002",
                subject="A", predicate="knows", obj="B",
                source_episode_id="00000000-0000-0000-0000-000000000010",
                valid_from=datetime(2024, 3, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 9, 30, tzinfo=timezone.utc),
            ),
        ]
        warnings = await service.check_project_temporal_consistency(
            UUID("00000000-0000-0000-0000-000000000020"),
        )
        assert warnings == []

    async def test_non_overlapping_same_triple_different_episode(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """Same triple, different episodes, disjoint ranges → no warning."""
        mock_repo.get_all_active_for_project.return_value = [
            _make_fact(
                subject="A", predicate="knows", obj="B",
                source_episode_id="00000000-0000-0000-0000-000000000010",
                valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 3, 31, tzinfo=timezone.utc),
            ),
            _make_fact(
                subject="A", predicate="knows", obj="B",
                source_episode_id="00000000-0000-0000-0000-000000000011",
                valid_from=datetime(2024, 4, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 6, 30, tzinfo=timezone.utc),
            ),
        ]
        warnings = await service.check_project_temporal_consistency(
            UUID("00000000-0000-0000-0000-000000000020"),
        )
        assert warnings == []

    # ── check_fact_ranges ─────────────────────────────────────────────────

    async def test_valid_ranges_no_warnings(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """All facts have valid ranges → empty warnings."""
        mock_repo.get_all_active_for_project.return_value = [
            _make_fact(
                valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 6, 30, tzinfo=timezone.utc),
            ),
            _make_fact(
                valid_from=datetime(2024, 3, 1, tzinfo=timezone.utc),
                valid_to=None,  # open-ended
            ),
        ]
        warnings = await service.check_fact_ranges(
            UUID("00000000-0000-0000-0000-000000000020"),
        )
        assert warnings == []

    async def test_invalid_range_detected(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """valid_to < valid_from → invalid_range warning."""
        mock_repo.get_all_active_for_project.return_value = [
            _make_fact(
                fact_id="00000000-0000-0000-0000-000000000001",
                valid_from=datetime(2024, 6, 1, tzinfo=timezone.utc),
                valid_to=datetime(2024, 1, 1, tzinfo=timezone.utc),  # before valid_from
            ),
        ]
        warnings = await service.check_fact_ranges(
            UUID("00000000-0000-0000-0000-000000000020"),
        )
        assert len(warnings) == 1
        assert warnings[0]["code"] == "invalid_range"

    async def test_future_date_detected(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """valid_from more than 24h in the future → future_date warning."""
        from datetime import timedelta

        far_future = datetime.now(timezone.utc) + timedelta(hours=48)
        mock_repo.get_all_active_for_project.return_value = [
            _make_fact(
                fact_id="00000000-0000-0000-0000-000000000001",
                valid_from=far_future,
            ),
        ]
        warnings = await service.check_fact_ranges(
            UUID("00000000-0000-0000-0000-000000000020"),
        )
        assert len(warnings) == 1
        assert warnings[0]["code"] == "future_date"

    async def test_future_date_within_threshold_no_warning(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """valid_from just a few hours in the future → no warning."""
        from datetime import timedelta

        near_future = datetime.now(timezone.utc) + timedelta(hours=2)
        mock_repo.get_all_active_for_project.return_value = [
            _make_fact(
                fact_id="00000000-0000-0000-0000-000000000001",
                valid_from=near_future,
            ),
        ]
        warnings = await service.check_fact_ranges(
            UUID("00000000-0000-0000-0000-000000000020"),
        )
        assert warnings == []

    # ── validate_batch ────────────────────────────────────────────────────

    async def test_batch_no_overlap(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """No overlapping triples in batch → empty warnings."""
        facts = [
            {"subject": "A", "predicate": "knows", "object": "B",
             "valid_from": datetime(2024, 1, 1, tzinfo=timezone.utc),
             "valid_to": datetime(2024, 6, 30, tzinfo=timezone.utc)},
            {"subject": "C", "predicate": "knows", "object": "D",
             "valid_from": datetime(2024, 1, 1, tzinfo=timezone.utc),
             "valid_to": datetime(2024, 6, 30, tzinfo=timezone.utc)},
        ]
        warnings = await service.validate_batch(facts)
        assert warnings == []

    async def test_batch_self_overlap_detected(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """Same triple, overlapping ranges in batch → warning."""
        facts = [
            {"subject": "A", "predicate": "knows", "object": "B",
             "source_episode_id": "00000000-0000-0000-0000-000000000010",
             "valid_from": datetime(2024, 1, 1, tzinfo=timezone.utc),
             "valid_to": datetime(2024, 6, 30, tzinfo=timezone.utc)},
            {"subject": "A", "predicate": "knows", "object": "B",
             "source_episode_id": "00000000-0000-0000-0000-000000000010",
             "valid_from": datetime(2024, 3, 1, tzinfo=timezone.utc),
             "valid_to": datetime(2024, 9, 30, tzinfo=timezone.utc)},
        ]
        warnings = await service.validate_batch(facts)
        assert len(warnings) == 1
        assert warnings[0]["code"] == "batch_overlap"

    async def test_batch_different_episodes_no_overlap(
        self, service: TemporalValidationService, mock_repo: AsyncMock
    ) -> None:
        """Same triple, different source_episode_id → no batch warning
        (cross-episode dedup is handled by project-level scan)."""
        facts = [
            {"subject": "A", "predicate": "knows", "object": "B",
             "source_episode_id": "00000000-0000-0000-0000-000000000010",
             "valid_from": datetime(2024, 1, 1, tzinfo=timezone.utc)},
            {"subject": "A", "predicate": "knows", "object": "B",
             "source_episode_id": "00000000-0000-0000-0000-000000000011",
             "valid_from": datetime(2024, 1, 1, tzinfo=timezone.utc)},
        ]
        warnings = await service.validate_batch(facts)
        assert warnings == []

    # ── _ranges_overlap helper ─────────────────────────────────────────────

    def test_ranges_overlap_both_null(self, service) -> None:
        """Both ranges fully open (NULL,NULL) → overlap."""
        assert service._ranges_overlap(None, None, None, None)

    def test_ranges_overlap_partial(self, service) -> None:
        """Partial overlap returns True."""
        a_s = datetime(2024, 1, 1, tzinfo=timezone.utc)
        a_e = datetime(2024, 6, 30, tzinfo=timezone.utc)
        b_s = datetime(2024, 3, 1, tzinfo=timezone.utc)
        b_e = datetime(2024, 9, 30, tzinfo=timezone.utc)
        assert service._ranges_overlap(a_s, a_e, b_s, b_e)

    def test_ranges_no_overlap_adjacent(self, service) -> None:
        """Adjacent ranges (end=start) → no overlap ('[)' semantics)."""
        a_s = datetime(2024, 1, 1, tzinfo=timezone.utc)
        a_e = datetime(2024, 6, 1, tzinfo=timezone.utc)
        b_s = datetime(2024, 6, 1, tzinfo=timezone.utc)
        b_e = datetime(2024, 12, 31, tzinfo=timezone.utc)
        assert not service._ranges_overlap(a_s, a_e, b_s, b_e)

    def test_ranges_no_overlap_disjoint(self, service) -> None:
        """Disjoint ranges → no overlap."""
        a_s = datetime(2024, 1, 1, tzinfo=timezone.utc)
        a_e = datetime(2024, 3, 31, tzinfo=timezone.utc)
        b_s = datetime(2024, 6, 1, tzinfo=timezone.utc)
        b_e = datetime(2024, 12, 31, tzinfo=timezone.utc)
        assert not service._ranges_overlap(a_s, a_e, b_s, b_e)

    def test_ranges_open_ended_overlap(self, service) -> None:
        """Open-ended range (valid_to=None) overlaps any range starting
        before its end."""
        a_s = datetime(2024, 1, 1, tzinfo=timezone.utc)
        a_e = None  # open-ended
        b_s = datetime(2024, 6, 1, tzinfo=timezone.utc)
        b_e = datetime(2024, 12, 31, tzinfo=timezone.utc)
        assert service._ranges_overlap(a_s, a_e, b_s, b_e)

    def test_ranges_contained(self, service) -> None:
        """One range fully inside another → overlap."""
        outer_s = datetime(2024, 1, 1, tzinfo=timezone.utc)
        outer_e = datetime(2024, 12, 31, tzinfo=timezone.utc)
        inner_s = datetime(2024, 3, 1, tzinfo=timezone.utc)
        inner_e = datetime(2024, 6, 30, tzinfo=timezone.utc)
        assert service._ranges_overlap(outer_s, outer_e, inner_s, inner_e)
        assert service._ranges_overlap(inner_s, inner_e, outer_s, outer_e)
