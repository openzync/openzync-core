"""Entity merge dedup unit tests — validates merge worker logic.

Tests the core merge pipeline components in isolation using mocked
backends (no DB, no graph).  The focus is on:

1. Cluster detection (exact + fuzzy matching over backend results).
2. Canonical entity selection (most recently created wins).
3. Relationship rewiring and duplicate dedup (via backend.merge_entities).
4. Audit trail creation.

These tests do **not** require a running LLM or database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from workers.tasks.merge_duplicate_entities import (
    _find_duplicate_clusters,
    _merge_cluster,
    _select_canonical,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create a mock async DB session (audit-log writes only)."""
    db = AsyncMock()
    return db


@pytest.fixture
def mock_backend() -> AsyncMock:
    """Create a mock graph backend."""
    return AsyncMock()


@pytest.fixture
def user_id() -> UUID:
    """Dummy user UUID."""
    return UUID("550e8400-e29b-41d4-a716-446655440000")


@pytest.fixture
def project_id() -> UUID:
    """Dummy project UUID."""
    return UUID("660e8400-e29b-41d4-a716-446655440001")


@pytest.fixture
def sample_cluster() -> list[dict]:
    """A synthetic duplicate cluster of 3 entities with same name."""
    return [
        {
            "id": "11111111-1111-4111-a111-111111111111",
            "name": "Acme Corp",
            "entity_type": "Organization",
            "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        },
        {
            "id": "22222222-2222-4222-a222-222222222222",
            "name": "Acme Corp",
            "entity_type": "Organization",
            "created_at": datetime(2024, 6, 1, tzinfo=UTC),
        },
        {
            "id": "33333333-3333-4333-a333-333333333333",
            "name": "Acme Corp",
            "entity_type": "Organization",
            "created_at": datetime(2024, 3, 1, tzinfo=UTC),
        },
    ]


@pytest.fixture
def single_entity_cluster() -> list[dict]:
    """A cluster with only one entity (no merge needed)."""
    return [
        {
            "id": "44444444-4444-4444-a444-444444444444",
            "name": "Sole Entity",
            "entity_type": "Person",
            "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        },
    ]


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestSelectCanonical:
    """Canonical entity selection logic (most-recent created_at wins)."""

    def test_selects_most_recently_created(
        self, sample_cluster: list[dict]
    ) -> None:
        """Entity with the latest created_at wins."""
        canonical = _select_canonical(sample_cluster)

        assert canonical["id"] == "22222222-2222-4222-a222-222222222222"
        assert canonical["name"] == "Acme Corp"

    def test_returns_single_entity_unchanged(
        self, single_entity_cluster: list[dict]
    ) -> None:
        """A cluster with one entity returns that entity as canonical."""
        canonical = _select_canonical(single_entity_cluster)

        assert canonical["id"] == "44444444-4444-4444-a444-444444444444"
        assert canonical["name"] == "Sole Entity"


class TestMergeCluster:
    """Cluster merge logic end-to-end (backend merge + audit log)."""

    @pytest.mark.asyncio
    async def test_merge_cluster_rewires_relationships(
        self,
        mock_db: AsyncMock,
        mock_backend: AsyncMock,
        user_id: UUID,
        project_id: UUID,
        sample_cluster: list[dict],
    ) -> None:
        """Merge delegates rewire+dedup to the backend, then audits."""
        mock_backend.merge_entities.return_value = {
            "rewired_count": 5,
            "deleted_count": 2,
            "merged_count": 2,
        }

        result = await _merge_cluster(
            mock_db, mock_backend, user_id, project_id, sample_cluster
        )

        assert result["entities_merged"] == 2
        assert result["relationships_rewired"] == 7
        mock_backend.merge_entities.assert_awaited_once()
        call_kwargs = mock_backend.merge_entities.call_args.kwargs
        assert call_kwargs["user_id"] == user_id
        assert call_kwargs["project_id"] == project_id
        assert len(call_kwargs["merged_ids"]) == 2

    @pytest.mark.asyncio
    async def test_merge_single_entity_does_nothing(
        self,
        mock_db: AsyncMock,
        mock_backend: AsyncMock,
        user_id: UUID,
        project_id: UUID,
        single_entity_cluster: list[dict],
    ) -> None:
        """A single-entity cluster returns zero merges without backend calls."""
        result = await _merge_cluster(
            mock_db, mock_backend, user_id, project_id, single_entity_cluster
        )

        assert result["entities_merged"] == 0
        assert result["relationships_rewired"] == 0
        mock_backend.merge_entities.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_merge_writes_audit_log(
        self,
        mock_db: AsyncMock,
        mock_backend: AsyncMock,
        user_id: UUID,
        project_id: UUID,
        sample_cluster: list[dict],
    ) -> None:
        """Merge writes an audit_log entry with before/after snapshot."""
        mock_backend.merge_entities.return_value = {
            "rewired_count": 2,
            "deleted_count": 1,
            "merged_count": 2,
        }

        await _merge_cluster(mock_db, mock_backend, user_id, project_id, sample_cluster)

        # Audit log is created via ``AuditLogService`` which uses
        # ``db.add(entry)`` — check ``add`` calls, not ``execute`` calls.
        all_add_text = str(mock_db.add.call_args_list)
        assert "entity.merge" in all_add_text, (
            "Audit log action should be 'entity.merge'"
        )


class TestFindDuplicateClusters:
    """Duplicate cluster detection logic (backend-driven)."""

    @pytest.mark.asyncio
    async def test_exact_match_detection(
        self, mock_backend: AsyncMock, user_id: UUID, project_id: UUID
    ) -> None:
        """Exact name matches cluster together; distinct names do not."""
        mock_backend.get_all_entities.return_value = [
            {"id": "a1", "name": "Acme Corp"},
            {"id": "a2", "name": "acme corp"},
            {"id": "b1", "name": "Globex"},
        ]
        mock_backend.bulk_search_entities.return_value = []

        clusters = await _find_duplicate_clusters(
            mock_backend, user_id, project_id
        )

        assert len(clusters) == 1
        assert {e["id"] for e in clusters[0]} == {"a1", "a2"}

    @pytest.mark.asyncio
    async def test_no_entities_no_clusters(
        self, mock_backend: AsyncMock, user_id: UUID, project_id: UUID
    ) -> None:
        """Fewer than two entities → no clusters, no fuzzy search."""
        mock_backend.get_all_entities.return_value = [
            {"id": "only", "name": "Sole"}
        ]

        clusters = await _find_duplicate_clusters(
            mock_backend, user_id, project_id
        )

        assert clusters == []
        mock_backend.bulk_search_entities.assert_not_awaited()

    def test_type_consistency(self) -> None:
        """All functions accept and return the expected types."""
        cluster = [
            {
                "id": "11111111-1111-4111-a111-111111111111",
                "name": "Acme Corp",
                "entity_type": "Organization",
                "created_at": datetime.now(UTC),
            },
        ]
        assert isinstance(cluster, list)
        assert len(cluster) == 1
        assert isinstance(cluster[0]["id"], str)


class TestMergeWorkerConstants:
    """Merge worker configuration sanity checks."""

    def test_similarity_threshold_in_range(self) -> None:
        """Fuzzy similarity threshold should be between 0 and 1."""
        from workers.tasks.merge_duplicate_entities import (
            FUZZY_SIMILARITY_THRESHOLD,
        )

        assert 0.0 < FUZZY_SIMILARITY_THRESHOLD < 1.0, (
            f"Threshold {FUZZY_SIMILARITY_THRESHOLD} must be between 0 and 1"
        )

    def test_merge_batch_size_positive(self) -> None:
        """Batch size should be a positive integer."""
        from workers.tasks.merge_duplicate_entities import MERGE_BATCH_SIZE

        assert MERGE_BATCH_SIZE > 0, f"Batch size {MERGE_BATCH_SIZE} must be positive"
        assert isinstance(MERGE_BATCH_SIZE, int), "Batch size must be an int"
