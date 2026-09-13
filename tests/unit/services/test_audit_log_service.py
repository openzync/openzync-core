"""Unit tests for AuditLogService — audit log creation and querying.

The repository is mocked at the service boundary. The service's ``log_action``
validates input before delegating to the repo.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from services.audit_log_service import AuditLogService


@pytest.mark.unit
class TestAuditLogService:
    """Unit tests for ``AuditLogService`` — recording and querying audit entries."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = "user_abc123"

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[AuditLogService, AsyncMock]:
        """Create an AuditLogService with mocked DB session.

        The service creates its own ``AuditLogRepository`` internally — we
        patch the repository class to return a mock.
        """
        mock_db = AsyncMock()
        mock_repo = AsyncMock()

        with patch(
            "services.audit_log_service.AuditLogRepository",
            return_value=mock_repo,
        ):
            service = AuditLogService(db=mock_db)

        # Store reference to the mock repo for assertions
        service._repo = mock_repo  # type: ignore[assignment]
        return service, mock_repo

    # ── log_action ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_log_action_creates_entry(self) -> None:
        """``log_action`` validates input and delegates to repository."""
        service, mock_repo = self._make_service()

        await service.log_action(
            organization_id=self.ORG_ID,
            actor_id=self.USER_ID,
            actor_type="user",
            action="session.create",
            resource_type="session",
            resource_id="sess-001",
            details={"ip": "1.2.3.4"},
            ip_address="1.2.3.4",
        )

        mock_repo.create.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            actor_id=self.USER_ID,
            actor_type="user",
            action="session.create",
            resource_type="session",
            resource_id="sess-001",
            details={"ip": "1.2.3.4"},
            ip_address="1.2.3.4",
        )

    @pytest.mark.asyncio
    async def test_log_action_no_user_context(self) -> None:
        """``log_action`` accepts None for nullable fields (unauthenticated actions)."""
        service, mock_repo = self._make_service()

        await service.log_action(
            organization_id=None,
            actor_id=None,
            actor_type=None,
            action="system.health_check",
            resource_type="system",
            resource_id=None,
            details=None,
            ip_address=None,
        )

        mock_repo.create.assert_awaited_once_with(
            organization_id=None,
            actor_id=None,
            actor_type=None,
            action="system.health_check",
            resource_type="system",
            resource_id=None,
            details=None,
            ip_address=None,
        )

    @pytest.mark.asyncio
    async def test_log_action_invalid_actor_type_raises_value_error(self) -> None:
        """``log_action`` raises ValueError for invalid actor_type."""
        service, _mock_repo = self._make_service()

        with pytest.raises(ValueError, match="Invalid actor_type"):
            await service.log_action(
                organization_id=self.ORG_ID,
                actor_id="bot-1",
                actor_type="robot",  # not in valid set
                action="session.create",
                resource_type="session",
            )

    @pytest.mark.asyncio
    async def test_log_action_all_valid_actor_types(self) -> None:
        """``log_action`` accepts all valid actor_type values."""
        service, mock_repo = self._make_service()

        for actor_type in ("user", "api_key", "system"):
            await service.log_action(
                organization_id=self.ORG_ID,
                actor_id=f"id-{actor_type}",
                actor_type=actor_type,
                action="test.action",
                resource_type="test",
            )

        assert mock_repo.create.await_count == 3

    # ── query_logs ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_query_logs_returns_entries_and_total(self) -> None:
        """``query_logs`` returns (entries, total) from repository."""
        service, mock_repo = self._make_service()

        mock_entry = MagicMock()
        mock_entry.id = "log-1"
        mock_repo.list.return_value = ([mock_entry], 1)

        entries, total = await service.query_logs(
            organization_id=self.ORG_ID,
            action="session.create",
            limit=50,
            offset=0,
        )

        assert len(entries) == 1
        assert total == 1
        mock_repo.list.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            action="session.create",
            actor_id=None,
            actor_type=None,
            resource_type=None,
            resource_id=None,
            status_code=None,
            exclude_prefix=None,
            created_after=None,
            created_before=None,
            limit=50,
            offset=0,
        )

    @pytest.mark.asyncio
    async def test_query_logs_with_filters(self) -> None:
        """``query_logs`` passes all filters through to repository."""
        service, mock_repo = self._make_service()
        mock_repo.list.return_value = ([], 0)

        await service.query_logs(
            organization_id=self.ORG_ID,
            action="user.login",
            actor_id=self.USER_ID,
            actor_type="user",
            resource_type="session",
            resource_id="sess-001",
            limit=10,
            offset=5,
        )

        mock_repo.list.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            action="user.login",
            actor_id=self.USER_ID,
            actor_type="user",
            resource_type="session",
            resource_id="sess-001",
            status_code=None,
            exclude_prefix=None,
            created_after=None,
            created_before=None,
            limit=10,
            offset=5,
        )

    @pytest.mark.asyncio
    async def test_query_logs_empty_returns_zero_count(self) -> None:
        """``query_logs`` returns empty list and zero count for no matches."""
        service, mock_repo = self._make_service()
        mock_repo.list.return_value = ([], 0)

        entries, total = await service.query_logs(
            organization_id=UUID("00000000-0000-0000-0000-000000000999"),
        )

        assert entries == []
        assert total == 0
