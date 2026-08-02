"""Unit tests for DialogClassificationRepository — read-only classification access."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.dialog_classification_repository import (
    DialogClassificationRepository,
)


pytestmark = pytest.mark.unit


class TestDialogClassificationRepository:
    """DialogClassificationRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000010")
    EPISODE_ID = UUID("00000000-0000-0000-0000-000000000020")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(
        self, mock_db: AsyncMock
    ) -> DialogClassificationRepository:
        return DialogClassificationRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_classification(self, **overrides: object) -> MagicMock:
        c = MagicMock()
        c.id = overrides.get("id", uuid4())
        c.episode_id = overrides.get("episode_id", self.EPISODE_ID)
        c.organization_id = overrides.get("organization_id", self.ORG_ID)
        c.label = overrides.get("label", "support")
        c.confidence = overrides.get("confidence", 0.95)
        return c

    # ── get_by_session ─────────────────────────────────────────────────────────

    async def test_get_by_session(
        self, repo: DialogClassificationRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_session returns classifications for all episodes in a session."""
        classifications = [
            self._mock_classification(),
            self._mock_classification(),
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = classifications
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_session(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result == classifications

    async def test_get_by_session_empty(
        self, repo: DialogClassificationRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_session returns empty list when no classifications exist."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_session(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result == []

    # ── get_by_episode ─────────────────────────────────────────────────────────

    async def test_get_by_episode_found(
        self, repo: DialogClassificationRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_episode returns classification when found."""
        classification = self._mock_classification()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = classification
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_episode(
            org_id=self.ORG_ID, episode_id=self.EPISODE_ID
        )

        assert result == classification

    async def test_get_by_episode_not_found(
        self, repo: DialogClassificationRepository, mock_db: AsyncMock
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
        self, repo: DialogClassificationRepository, mock_db: AsyncMock
    ) -> None:
        """count_for_session returns the count of classifications."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 5
        mock_db.execute.return_value = mock_result

        count = await repo.count_for_session(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert count == 5

    async def test_count_for_session_zero(
        self, repo: DialogClassificationRepository, mock_db: AsyncMock
    ) -> None:
        """count_for_session returns 0 when no classifications."""
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        mock_db.execute.return_value = mock_result

        count = await repo.count_for_session(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert count == 0
