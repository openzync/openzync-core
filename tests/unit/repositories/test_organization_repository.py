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

    async def test_get_pii_config(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_pii_config returns the PII sub-document."""
        mock_row = MagicMock()
        mock_row.__getitem__.return_value = {"enabled": True, "entities": ["email"]}
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = mock_row
        mock_db.execute.return_value = mock_result

        pii = await repo.get_pii_config(org_id=self.ORG_ID)

        assert pii == {"enabled": True, "entities": ["email"]}

    async def test_get_pii_config_not_set(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_pii_config returns empty dict when not configured."""
        mock_row = MagicMock()
        mock_row.__getitem__.return_value = None
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = mock_row
        mock_db.execute.return_value = mock_result

        pii = await repo.get_pii_config(org_id=self.ORG_ID)

        assert pii == {}

    async def test_get_pii_config_org_not_found(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_pii_config returns empty dict when org does not exist."""
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        pii = await repo.get_pii_config(org_id=self.ORG_ID)

        assert pii == {}

    # ── get_llm_config ─────────────────────────────────────────────────────────

    async def test_get_llm_config_from_new_config(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_llm_config reads from config->'llm' first."""
        row = MagicMock()
        row.llm = {"model": "gpt-4", "temperature": 0.7}
        result = MagicMock()
        result.one_or_none.return_value = row
        mock_db.execute.return_value = result

        llm = await repo.get_llm_config(org_id=self.ORG_ID)

        assert llm == {"model": "gpt-4", "temperature": 0.7}

    async def test_get_llm_config_falls_back_to_legacy(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_llm_config falls back to legacy llm_config column."""
        new_row = MagicMock()
        new_row.llm = None
        new_result = MagicMock()
        new_result.one_or_none.return_value = new_row

        legacy_row = MagicMock()
        legacy_row.llm_config = {"model": "gpt-3.5"}
        legacy_result = MagicMock()
        legacy_result.one_or_none.return_value = legacy_row

        mock_db.execute.side_effect = [new_result, legacy_result]

        llm = await repo.get_llm_config(org_id=self.ORG_ID)

        assert llm == {"model": "gpt-3.5"}

    async def test_get_llm_config_not_found(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_llm_config returns empty dict when not configured."""
        new_row = MagicMock()
        new_row.llm = None
        new_result = MagicMock()
        new_result.one_or_none.return_value = new_row

        legacy_row = MagicMock()
        legacy_row.llm_config = None
        legacy_result = MagicMock()
        legacy_result.one_or_none.return_value = legacy_row

        mock_db.execute.side_effect = [new_result, legacy_result]

        llm = await repo.get_llm_config(org_id=self.ORG_ID)

        assert llm == {}

    # ── get_quota ──────────────────────────────────────────────────────────────

    async def test_get_quota(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_quota returns the quota value."""
        row = MagicMock()
        row.quota = 100
        result = MagicMock()
        result.one_or_none.return_value = row
        mock_db.execute.return_value = result

        quota = await repo.get_quota(org_id=self.ORG_ID, quota_name="max_users")

        assert quota == 100

    async def test_get_quota_not_set(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_quota returns None when quota not configured."""
        row = MagicMock()
        row.quota = None
        result = MagicMock()
        result.one_or_none.return_value = row
        mock_db.execute.return_value = result

        quota = await repo.get_quota(org_id=self.ORG_ID, quota_name="nonexistent")

        assert quota is None

    async def test_get_quota_org_not_found(
        self, repo: OrganizationRepository, mock_db: AsyncMock
    ) -> None:
        """get_quota returns None when org does not exist."""
        result = MagicMock()
        result.one_or_none.return_value = None
        mock_db.execute.return_value = result

        quota = await repo.get_quota(org_id=self.ORG_ID, quota_name="max_users")

        assert quota is None
