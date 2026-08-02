"""Unit tests for AuditLogRepository — append-only audit log access."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.audit_log_repository import AuditLogRepository


pytestmark = pytest.mark.unit


class TestAuditLogRepository:
    """AuditLogRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> AuditLogRepository:
        return AuditLogRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_entry(self, **overrides: object) -> MagicMock:
        entry = MagicMock()
        entry.id = overrides.get("id", uuid4())
        entry.organization_id = overrides.get("organization_id", self.ORG_ID)
        entry.actor_id = overrides.get("actor_id", "user-abc")
        entry.actor_type = overrides.get("actor_type", "user")
        entry.action = overrides.get("action", "session.create")
        entry.resource_type = overrides.get("resource_type", "session")
        entry.resource_id = overrides.get("resource_id", "sess-123")
        entry.details = overrides.get("details", {"key": "val"})
        entry.ip_address = overrides.get("ip_address", "127.0.0.1")
        entry.created_at = overrides.get("created_at", None)
        return entry

    # ── create ─────────────────────────────────────────────────────────────────

    async def test_create_inserts_and_commits(
        self, repo: AuditLogRepository, mock_db: AsyncMock
    ) -> None:
        """create inserts an entry, commits, refreshes, and returns it."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.commit.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID,
            actor_id="user-abc",
            actor_type="user",
            action="session.create",
            resource_type="session",
            resource_id="sess-123",
            details={"key": "val"},
            ip_address="127.0.0.1",
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.commit.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_create_with_minimal_fields(
        self, repo: AuditLogRepository, mock_db: AsyncMock
    ) -> None:
        """create works with minimal arguments."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.commit.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID,
            actor_id=None,
            actor_type=None,
            action="system.startup",
            resource_type="system",
            resource_id=None,
            details=None,
            ip_address=None,
        )

        assert result is not None
        mock_db.add.assert_called_once()

    # ── list ───────────────────────────────────────────────────────────────────

    async def test_list_without_filters(
        self, repo: AuditLogRepository, mock_db: AsyncMock
    ) -> None:
        """list returns entries and total count."""
        entries = [self._mock_entry(), self._mock_entry()]
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all.return_value = entries
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 2

        async def _execute_side(stmt: object, **kwargs: object) -> MagicMock:
            # Return count for count queries, list for list queries
            return mock_count_result

        mock_db.execute.side_effect = lambda *a, **kw: (
            mock_count_result if hasattr(a[0], 'count') else mock_list_result
        )

        # We'll use two sequential calls instead
        mock_db.execute.side_effect = None
        mock_db.execute.side_effect = [mock_count_result, mock_list_result]

        result, total = await repo.list(organization_id=self.ORG_ID)

        assert result == entries
        assert total == 2
        assert mock_db.execute.await_count == 2

    async def test_list_with_filters(
        self, repo: AuditLogRepository, mock_db: AsyncMock
    ) -> None:
        """list filters by action, actor_id, and actor_type."""
        entries = [self._mock_entry(action="session.create")]
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all.return_value = entries
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 1
        mock_db.execute.side_effect = [mock_count_result, mock_list_result]

        result, total = await repo.list(
            organization_id=self.ORG_ID,
            action="session.create",
            actor_id="user-abc",
            actor_type="user",
        )

        assert result == entries
        assert total == 1

    async def test_list_with_pagination(
        self, repo: AuditLogRepository, mock_db: AsyncMock
    ) -> None:
        """list respects limit and offset."""
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all.return_value = []
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0
        mock_db.execute.side_effect = [mock_count_result, mock_list_result]

        result, total = await repo.list(
            organization_id=self.ORG_ID, limit=10, offset=5
        )

        assert result == []
        assert total == 0

    async def test_list_with_exclude_prefix(
        self, repo: AuditLogRepository, mock_db: AsyncMock
    ) -> None:
        """list excludes actions matching the given prefixes."""
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all.return_value = []
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0
        mock_db.execute.side_effect = [mock_count_result, mock_list_result]

        result, total = await repo.list(
            organization_id=self.ORG_ID,
            exclude_prefix="system.,health.",
        )

        assert result == []

    async def test_list_with_resource_filters(
        self, repo: AuditLogRepository, mock_db: AsyncMock
    ) -> None:
        """list filters by resource_type and resource_id."""
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all.return_value = []
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0
        mock_db.execute.side_effect = [mock_count_result, mock_list_result]

        result, total = await repo.list(
            organization_id=self.ORG_ID,
            resource_type="session",
            resource_id="sess-123",
        )

        assert result == []
        assert total == 0

    async def test_list_with_date_range(
        self, repo: AuditLogRepository, mock_db: AsyncMock
    ) -> None:
        """list filters by created_after and created_before."""
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all.return_value = []
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0
        mock_db.execute.side_effect = [mock_count_result, mock_list_result]

        result, total = await repo.list(
            organization_id=self.ORG_ID,
            created_after="2024-01-01T00:00:00",
            created_before="2024-12-31T23:59:59",
        )

        assert result == []

    async def test_list_empty_returns_empty(
        self, repo: AuditLogRepository, mock_db: AsyncMock
    ) -> None:
        """list returns empty list and zero count when no entries match."""
        mock_list_result = MagicMock()
        mock_list_result.scalars.return_value.all.return_value = []
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0
        mock_db.execute.side_effect = [mock_count_result, mock_list_result]

        result, total = await repo.list(organization_id=self.ORG_ID)

        assert result == []
        assert total == 0
