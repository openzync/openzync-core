"""Unit tests for OrganizationRepository — org config and quota access."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.organization_repository import OrganizationRepository

pytestmark = pytest.mark.unit


class TestOrganizationRepository:
    """OrganizationRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> OrganizationRepository:
        return OrganizationRepository(db=mock_db)

    # ── session property ───────────────────────────────────────────────────────

    def test_session_property(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """session property returns the db instance."""
        assert repo.session == mock_db

    # ── get_config ─────────────────────────────────────────────────────────────

    async def test_get_config(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_config returns the config dict."""
        mock_row = MagicMock()
        mock_row.config = {"llm": {"model": "gpt-4"}, "features": {"flag": True}}
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = mock_row
        mock_db.execute.return_value = mock_result

        config = await repo.get_config(org_id=self.ORG_ID)

        assert config == {"llm": {"model": "gpt-4"}, "features": {"flag": True}}

    async def test_get_config_empty(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_config returns empty dict when no config."""
        mock_row = MagicMock()
        mock_row.config = None
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = mock_row
        mock_db.execute.return_value = mock_result

        config = await repo.get_config(org_id=self.ORG_ID)

        assert config == {}

    async def test_get_config_org_not_found(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_config returns empty dict when org does not exist."""
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        config = await repo.get_config(org_id=self.ORG_ID)

        assert config == {}

    # ── get_pii_config ─────────────────────────────────────────────────────────

