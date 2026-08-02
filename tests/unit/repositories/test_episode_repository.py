"""Unit tests for EpisodeRepository — episode CRUD and search."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.episode_repository import EpisodeRepository


pytestmark = pytest.mark.unit


class TestEpisodeRepository:
    """EpisodeRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000003")
    USER_ID = UUID("00000000-0000-0000-0000-000000000004")
    EPISODE_ID = UUID("00000000-0000-0000-0000-000000000010")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> EpisodeRepository:
        return EpisodeRepository(db=mock_db)

    # ── Mock Helpers ───────────────────────────────────────────────────────────

    def _mock_episode(self, **overrides: object) -> MagicMock:
        ep = MagicMock()
        ep.id = overrides.get("id", self.EPISODE_ID)
        ep.organization_id = overrides.get("organization_id", self.ORG_ID)
        ep.session_id = overrides.get("session_id", self.SESSION_ID)
        ep.user_id = overrides.get("user_id", self.USER_ID)
        ep.role = overrides.get("role", "user")
        ep.content = overrides.get("content", "Hello")
        ep.metadata_ = overrides.get("metadata_", {})
        ep.sequence_number = overrides.get("sequence_number", 0)
        ep.enrichment_status = overrides.get("enrichment_status", 0)
        ep.is_deleted = overrides.get("is_deleted", False)
        ep.created_at = overrides.get("created_at", None)
        ep.updated_at = overrides.get("updated_at", None)
        return ep

    def _mock_episode_row(self, **overrides: object) -> MagicMock:
        """Mock a raw row with ._mapping for batch_create."""
        row = MagicMock()
        row._mapping = {
            "id": overrides.get("id", self.EPISODE_ID),
            "organization_id": overrides.get("organization_id", self.ORG_ID),
            "session_id": overrides.get("session_id", self.SESSION_ID),
            "user_id": overrides.get("user_id", self.USER_ID),
            "role": overrides.get("role", "user"),
            "content": overrides.get("content", "Hello"),
            "metadata": overrides.get("metadata", {}),
            "embedding": overrides.get("embedding", None),
            "token_count": overrides.get("token_count", None),
            "sequence_number": overrides.get("sequence_number", 0),
            "enrichment_status": overrides.get("enrichment_status", 0),
            "is_deleted": overrides.get("is_deleted", False),
            "created_at": overrides.get("created_at", None),
            "updated_at": overrides.get("updated_at", None),
        }
        return row

    # ── batch_create ───────────────────────────────────────────────────────────

    async def test_batch_create_empty(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create returns empty list when no messages."""
        result = await repo.batch_create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            session_id=self.SESSION_ID,
            user_id=self.USER_ID,
            messages=[],
        )

        assert result == []
        mock_db.execute.assert_not_called()

    async def test_batch_create_inserts_messages(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """batch_create inserts messages and returns ORM instances."""
        messages = [
            {"role": "user", "content": "Hello", "metadata": {}},
            {"role": "assistant", "content": "Hi there!", "metadata": {}},
        ]
        mock_db.execute.return_value = MagicMock()
        mock_db.execute.return_value.fetchall.return_value = [
            self._mock_episode_row(id=uuid4(), role="user", sequence_number=0),
            self._mock_episode_row(id=uuid4(), role="assistant", sequence_number=1),
        ]

        result = await repo.batch_create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            session_id=self.SESSION_ID,
            user_id=self.USER_ID,
            messages=messages,
        )

        assert len(result) == 2
        mock_db.execute.assert_awaited_once()

    # ── get_by_session_id ──────────────────────────────────────────────────────

    async def test_get_by_session_id(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_session_id returns episodes with pagination."""
        episodes = [self._mock_episode() for _ in range(3)]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = episodes
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.get_by_session_id(
            session_id=self.SESSION_ID, limit=10
        )

        assert len(result) == 3
        assert cursor is None

    async def test_get_by_session_id_with_cursor(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_session_id decodes cursor and filters."""
        episodes = [self._mock_episode(sequence_number=5)]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = episodes
        mock_db.execute.return_value = mock_result

        encoded = EpisodeRepository._encode_cursor(3, self.EPISODE_ID)
        result, cursor = await repo.get_by_session_id(
            session_id=self.SESSION_ID, cursor=encoded, limit=10
        )

        assert len(result) == 1
        assert cursor is None

    async def test_get_by_session_id_empty(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_session_id returns empty list when no episodes."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.get_by_session_id(
            session_id=self.SESSION_ID, limit=10
        )

        assert result == []
        assert cursor is None

    # ── get_by_project_id ──────────────────────────────────────────────────────

    async def test_get_by_project_id(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_project_id returns episodes for a project."""
        episodes = [self._mock_episode()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = episodes
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.get_by_project_id(
            project_id=self.PROJECT_ID, limit=10
        )

        assert len(result) == 1

    async def test_get_by_project_id_with_cursor(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_project_id decodes cursor."""
        episodes = [self._mock_episode(sequence_number=3)]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = episodes
        mock_db.execute.return_value = mock_result

        encoded = EpisodeRepository._encode_cursor(1, self.EPISODE_ID)
        result, cursor = await repo.get_by_project_id(
            project_id=self.PROJECT_ID, cursor=encoded, limit=10
        )

        assert len(result) == 1

    # ── get_next_sequence ──────────────────────────────────────────────────────

    async def test_get_next_sequence(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_next_sequence returns next seq number."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 5
        mock_db.execute.return_value = mock_result

        seq = await repo.get_next_sequence(session_id=self.SESSION_ID)

        assert seq == 5

    async def test_get_next_sequence_default(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_next_sequence returns 0 when no episodes."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_db.execute.return_value = mock_result

        seq = await repo.get_next_sequence(session_id=self.SESSION_ID)

        assert seq == 0

    # ── get_content_batch ──────────────────────────────────────────────────────

    async def test_get_content_batch(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_content_batch returns content for episode IDs."""
        row_1 = MagicMock()
        row_1.id = self.EPISODE_ID
        row_1.content = "Hello"
        row_1.role = "user"
        row_2 = MagicMock()
        row_2.id = UUID("00000000-0000-0000-0000-000000000011")
        row_2.content = "Hi"
        row_2.role = "assistant"
        mock_db.execute.return_value = MagicMock()
        mock_db.execute.return_value.all.return_value = [row_1, row_2]

        result = await repo.get_content_batch(
            episode_ids=[self.EPISODE_ID, UUID("00000000-0000-0000-0000-000000000011")]
        )

        assert len(result) == 2
        assert result[self.EPISODE_ID] == ("Hello", "user")

    async def test_get_content_batch_empty(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_content_batch returns empty dict for empty input."""
        result = await repo.get_content_batch(episode_ids=[])

        assert result == {}

    async def test_get_content_batch_with_org(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_content_batch filters by org_id."""
        mock_db.execute.return_value = MagicMock()
        mock_db.execute.return_value.all.return_value = []

        result = await repo.get_content_batch(
            episode_ids=[self.EPISODE_ID], org_id=self.ORG_ID
        )

        assert result == {}

    # ── get_by_id ──────────────────────────────────────────────────────────────

    async def test_get_by_id_found(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns episode when found."""
        ep = self._mock_episode()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ep
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(episode_id=self.EPISODE_ID)

        assert result == ep

    async def test_get_by_id_not_found(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(episode_id=self.EPISODE_ID)

        assert result is None

    # ── get_by_id_for_update ───────────────────────────────────────────────────

    async def test_get_by_id_for_update(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id_for_update returns episode with FOR UPDATE lock."""
        ep = self._mock_episode()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ep
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id_for_update(episode_id=self.EPISODE_ID)

        assert result == ep

    async def test_get_by_id_for_update_not_found(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id_for_update returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id_for_update(episode_id=self.EPISODE_ID)

        assert result is None

    # ── update_enrichment_status ───────────────────────────────────────────────

    async def test_update_enrichment_status(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """update_enrichment_status executes update."""
        mock_db.execute.return_value = MagicMock()

        await repo.update_enrichment_status(
            episode_id=self.EPISODE_ID, bitmask=7
        )

        mock_db.execute.assert_awaited_once()

    # ── apply_enrichment_bits ──────────────────────────────────────────────────

    async def test_apply_enrichment_bits(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """apply_enrichment_bits ORs bits into enrichment_status."""
        mock_db.execute.return_value = MagicMock()

        await repo.apply_enrichment_bits(
            episode_id=self.EPISODE_ID, bitmask=3
        )

        mock_db.execute.assert_awaited_once()

    # ── soft_delete_by_project ─────────────────────────────────────────────────

    async def test_soft_delete_by_project(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete_by_project marks episodes as deleted."""
        mock_result = MagicMock()
        mock_result.rowcount = 5
        mock_db.execute.return_value = mock_result

        count = await repo.soft_delete_by_project(project_id=self.PROJECT_ID)

        assert count == 5

    async def test_soft_delete_by_project_zero(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete_by_project returns 0 when no episodes match."""
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_db.execute.return_value = mock_result

        count = await repo.soft_delete_by_project(project_id=self.PROJECT_ID)

        assert count == 0

    # ── search_by_vector ───────────────────────────────────────────────────────

    async def test_search_by_vector(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """search_by_vector returns ranked results."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            (str(self.EPISODE_ID), "Hello", "user", "2024-01-01", 0.95),
        ]
        mock_db.execute.return_value = mock_result

        results = await repo.search_by_vector(
            embedding=[0.1, 0.2, 0.3],
            project_id=self.PROJECT_ID,
            limit=10,
        )

        assert len(results) == 1
        assert results[0]["score"] == 0.95
        assert results[0]["content"] == "Hello"

    async def test_search_by_vector_empty(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """search_by_vector returns empty list when no matches."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_db.execute.return_value = mock_result

        results = await repo.search_by_vector(
            embedding=[0.1, 0.2, 0.3],
            project_id=self.PROJECT_ID,
            limit=10,
        )

        assert results == []

    # ── search_by_bm25 ─────────────────────────────────────────────────────────

    async def test_search_by_bm25(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """search_by_bm25 returns ranked full-text results."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            (str(self.EPISODE_ID), "Hello world", "user", "2024-01-01", 0.85),
        ]
        mock_db.execute.return_value = mock_result

        results = await repo.search_by_bm25(
            query="hello",
            project_id=self.PROJECT_ID,
            org_id=self.ORG_ID,
            limit=10,
        )

        assert len(results) == 1
        assert results[0]["score"] == 0.85

    async def test_search_by_bm25_empty(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """search_by_bm25 returns empty list when no matches."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_db.execute.return_value = mock_result

        results = await repo.search_by_bm25(
            query="nonexistent",
            project_id=self.PROJECT_ID,
            org_id=self.ORG_ID,
            limit=10,
        )

        assert results == []

    # ── count_by_project ───────────────────────────────────────────────────────

    async def test_count_by_project(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """count_by_project returns episode count."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 42
        mock_db.execute.return_value = mock_result

        count = await repo.count_by_project(project_id=self.PROJECT_ID)

        assert count == 42

    async def test_count_by_project_zero(
        self, repo: EpisodeRepository, mock_db: AsyncMock
    ) -> None:
        """count_by_project returns 0 when no episodes."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_db.execute.return_value = mock_result

        count = await repo.count_by_project(project_id=self.PROJECT_ID)

        assert count == 0

    # ── Cursor helpers ─────────────────────────────────────────────────────────

    def test_encode_decode_cursor_roundtrip(
        self,
    ) -> None:
        """_encode_cursor and _decode_cursor are inverses."""
        seq = 42
        ep_id = self.EPISODE_ID
        encoded = EpisodeRepository._encode_cursor(seq, ep_id)
        decoded_seq, decoded_id = EpisodeRepository._decode_cursor(encoded)

        assert decoded_seq == seq
        assert decoded_id == ep_id

    def test_decode_cursor_invalid_raises(
        self,
    ) -> None:
        """_decode_cursor raises ValueError for invalid input."""
        with pytest.raises(ValueError, match="Invalid episode cursor"):
            EpisodeRepository._decode_cursor("!!!invalid!!!")
