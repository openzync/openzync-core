"""Unit tests for StructuredExtractionRepository — read-only extraction access."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.structured_extraction_repository import (
    StructuredExtractionRepository,
)


pytestmark = pytest.mark.unit


class TestStructuredExtractionRepository:
    """StructuredExtractionRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000010")
    EPISODE_ID = UUID("00000000-0000-0000-0000-000000000020")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> StructuredExtractionRepository:
        return StructuredExtractionRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_extraction(self, **overrides: object) -> MagicMock:
        ext = MagicMock()
        ext.id = overrides.get("id", uuid4())
        ext.episode_id = overrides.get("episode_id", self.EPISODE_ID)
        ext.organization_id = overrides.get("organization_id", self.ORG_ID)
        ext.schema_name = overrides.get("schema_name", "test_schema")
        ext.result = overrides.get("result", {"field": "value"})
        ext.created_at = overrides.get("created_at", None)
        return ext

    # ── get_by_session ─────────────────────────────────────────────────────────

    async def test_get_by_session(
        self, repo: StructuredExtractionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_session returns extractions ordered by sequence number."""
        extractions = [
            self._mock_extraction(),
            self._mock_extraction(),
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = extractions
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_session(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result == extractions

    async def test_get_by_session_empty(
        self, repo: StructuredExtractionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_session returns empty list when no extractions."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_session(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result == []

    # ── get_by_episode ─────────────────────────────────────────────────────────

    async def test_get_by_episode_found(
        self, repo: StructuredExtractionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_episode returns extraction when found."""
        extraction = self._mock_extraction()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = extraction
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_episode(
            org_id=self.ORG_ID, episode_id=self.EPISODE_ID
        )

        assert result == extraction

    async def test_get_by_episode_not_found(
        self, repo: StructuredExtractionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_episode returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_episode(
            org_id=self.ORG_ID, episode_id=self.EPISODE_ID
        )

        assert result is None

    # ── count_for_session ──────────────────────────────────────────────────────

    async def test_count_for_session(
        self, repo: StructuredExtractionRepository, mock_db: AsyncMock
    ) -> None:
        """count_for_session returns the count."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 5
        mock_db.execute.return_value = mock_result

        count = await repo.count_for_session(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert count == 5

    async def test_count_for_session_zero(
        self, repo: StructuredExtractionRepository, mock_db: AsyncMock
    ) -> None:
        """count_for_session returns 0 when no extractions."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        mock_db.execute.return_value = mock_result

        count = await repo.count_for_session(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert count == 0
