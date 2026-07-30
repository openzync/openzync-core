"""Unit tests for ApiKeyRepository — all DB access for API keys."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.api_key_repository import ApiKeyRepository


pytestmark = pytest.mark.unit


class TestApiKeyRepository:
    """ApiKeyRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
    KEY_ID = UUID("00000000-0000-0000-0000-000000000010")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> ApiKeyRepository:
        return ApiKeyRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_key(self, **overrides: object) -> MagicMock:
        key = MagicMock()
        key.id = overrides.get("id", self.KEY_ID)
        key.organization_id = overrides.get("organization_id", self.ORG_ID)
        key.project_id = overrides.get("project_id", self.PROJECT_ID)
        key.name = overrides.get("name", "Test Key")
        key.lookup_hash = overrides.get("lookup_hash", "abc123")
        key.key_hash = overrides.get("key_hash", "def456")
        key.salt = overrides.get("salt", "salt123")
        key.prefix = overrides.get("prefix", "oz_test_")
        key.scopes = overrides.get("scopes", ["read", "write"])
        key.is_revoked = overrides.get("is_revoked", False)
        key.last_used_at = overrides.get("last_used_at", None)
        key.created_at = overrides.get("created_at", None)
        key.created_by = overrides.get("created_by", None)
        return key

    # ── list_by_org ────────────────────────────────────────────────────────────

    async def test_list_by_org_returns_all_keys(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """list_by_org returns all non-revoked keys for an org."""
        keys = [self._mock_key(), self._mock_key(id=uuid4())]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = keys
        mock_db.execute.return_value = mock_result

        result = await repo.list_by_org(organization_id=self.ORG_ID)

        assert list(result) == keys
        mock_db.execute.assert_awaited_once()

    async def test_list_by_org_includes_revoked(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """list_by_org returns revoked keys when include_revoked=True."""
        keys = [self._mock_key(is_revoked=True), self._mock_key()]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = keys
        mock_db.execute.return_value = mock_result

        result = await repo.list_by_org(
            organization_id=self.ORG_ID, include_revoked=True
        )

        assert len(result) == 2

    async def test_list_by_org_filters_by_project(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """list_by_org filters keys by project_id when provided."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.list_by_org(
            organization_id=self.ORG_ID, project_id=self.PROJECT_ID
        )

        assert result == []
        mock_db.execute.assert_awaited_once()

    async def test_list_by_org_empty(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """list_by_org returns empty list when no keys exist."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result = await repo.list_by_org(organization_id=self.ORG_ID)

        assert result == []

    # ── get_by_id ──────────────────────────────────────────────────────────────

    async def test_get_by_id_found(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns the key when found."""
        key = self._mock_key()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = key
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(
            organization_id=self.ORG_ID, key_id=self.KEY_ID
        )

        assert result == key

    async def test_get_by_id_not_found(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id returns None when key does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(
            organization_id=self.ORG_ID, key_id=self.KEY_ID
        )

        assert result is None

    async def test_get_by_id_with_project_scope(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_id filters by project_id when provided."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_id(
            organization_id=self.ORG_ID,
            key_id=self.KEY_ID,
            project_id=self.PROJECT_ID,
        )

        assert result is None
        mock_db.execute.assert_awaited_once()

    # ── create ─────────────────────────────────────────────────────────────────

    async def test_create_returns_key(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """create inserts and returns a new ApiKey."""
        key = self._mock_key()
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        # After refresh, the mock key is returned
        async def _side_effect(*args: object, **kwargs: object) -> None:
            mock_db.add.assert_called_once()

        mock_db.refresh.side_effect = _side_effect

        result = await repo.create(
            organization_id=self.ORG_ID,
            lookup_hash="abc123",
            key_hash="def456",
            salt="salt123",
            prefix="oz_test_",
            name="Test Key",
            scopes=["read", "write"],
            project_id=self.PROJECT_ID,
            created_by=uuid4(),
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_create_minimal(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """create works with minimal arguments."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        await repo.create(
            organization_id=self.ORG_ID,
            lookup_hash="abc",
            key_hash="def",
            salt="salt",
            prefix="oz_test_",
            name="Minimal",
        )

        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()

    # ── revoke ─────────────────────────────────────────────────────────────────

    async def test_revoke_sets_flag(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """revoke marks the key as revoked and returns it."""
        key = self._mock_key(is_revoked=False)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = key
        mock_db.execute.return_value = mock_result

        result = await repo.revoke(
            organization_id=self.ORG_ID, key_id=self.KEY_ID
        )

        assert result is not None
        assert result.is_revoked is True
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_revoke_not_found_returns_none(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """revoke returns None when key does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.revoke(
            organization_id=self.ORG_ID, key_id=self.KEY_ID
        )

        assert result is None
        mock_db.flush.assert_not_called()

    async def test_revoke_with_project_scope(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """revoke filters by project_id when provided."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        await repo.revoke(
            organization_id=self.ORG_ID,
            key_id=self.KEY_ID,
            project_id=self.PROJECT_ID,
        )

        mock_db.execute.assert_awaited_once()

    # ── get_by_lookup_hash ─────────────────────────────────────────────────────

    async def test_get_by_lookup_hash_found(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_lookup_hash returns key when hash matches."""
        key = self._mock_key()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = key
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_lookup_hash("abc123")

        assert result == key

    async def test_get_by_lookup_hash_not_found(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_lookup_hash returns None when no match."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_lookup_hash("nonexistent")

        assert result is None

    # ── update_last_used ───────────────────────────────────────────────────────

    async def test_update_last_used(
        self, repo: ApiKeyRepository, mock_db: AsyncMock
    ) -> None:
        """update_last_used executes update and flushes."""
        mock_db.execute.return_value = MagicMock()

        await repo.update_last_used(key_id=self.KEY_ID)

        mock_db.execute.assert_awaited_once()
        mock_db.flush.assert_awaited_once()
