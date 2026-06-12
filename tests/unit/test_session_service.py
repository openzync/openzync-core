"""Unit tests for SessionService — business logic with mocked repository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.exceptions import ConflictError, NotFoundError
from repositories.session_repository import SessionRepository
from services.session_service import SessionService


@pytest.mark.unit
class TestSessionService:
    """SessionService unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")

    def _make_mock_session(self, **kwargs) -> AsyncMock:
        session = AsyncMock()
        session.id = kwargs.get("id", uuid4())
        session.organization_id = kwargs.get("org_id", self.ORG_ID)
        session.user_id = kwargs.get("user_id", self.USER_ID)
        session.external_id = kwargs.get("external_id", "test-session")
        session.metadata_ = kwargs.get("metadata", {})
        session.is_active = kwargs.get("is_active", True)
        session.is_deleted = kwargs.get("is_deleted", False)
        session.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
        session.updated_at = kwargs.get("updated_at", datetime.now(timezone.utc))
        session.closed_at = kwargs.get("closed_at", None)
        return session

    def _make_service(self) -> tuple[SessionService, AsyncMock]:
        """Create a SessionService with a mocked repository."""
        mock_repo = AsyncMock(spec=SessionRepository)
        service = SessionService(repo=mock_repo)
        return service, mock_repo

    @pytest.mark.asyncio
    async def test_create_session_success(self) -> None:
        """Creating a session returns the response."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_external_id.return_value = None
        mock_repo.create.return_value = self._make_mock_session(
            external_id="test-session"
        )

        result = await service.create_session(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            external_id="test-session",
        )
        assert result.external_id == "test-session"
        mock_repo.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_session_duplicate_raises_conflict(self) -> None:
        """Creating a session with existing external_id raises ConflictError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_external_id.return_value = self._make_mock_session()

        with pytest.raises(ConflictError):
            await service.create_session(
                organization_id=self.ORG_ID,
                user_id=self.USER_ID,
                external_id="duplicate-session",
            )
        mock_repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_session_found(self) -> None:
        """Getting a session returns the response."""
        service, mock_repo = self._make_service()
        mock_session = self._make_mock_session(external_id="test-session")
        mock_repo.get_by_uuid.return_value = mock_session
        mock_repo.get_stats.return_value = {
            "message_count": 5,
            "fact_count": 3,
            "last_message_at": datetime.now(timezone.utc),
        }

        session_id = mock_session.id
        result = await service.get_session(
            org_id=self.ORG_ID,
            session_id=session_id,
            user_id=self.USER_ID,
        )
        assert result.external_id == "test-session"

    @pytest.mark.asyncio
    async def test_get_session_not_found_raises_404(self) -> None:
        """Getting a non-existent session raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_uuid.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_session(
                org_id=self.ORG_ID,
                session_id=uuid4(),
                user_id=self.USER_ID,
            )

    @pytest.mark.asyncio
    async def test_delete_session(self) -> None:
        """Deleting a session calls soft_delete."""
        service, mock_repo = self._make_service()
        mock_repo.soft_delete.return_value = self._make_mock_session()

        session_id = uuid4()
        await service.delete_session(
            org_id=self.ORG_ID,
            session_id=session_id,
            user_id=self.USER_ID,
        )
        mock_repo.soft_delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_session_not_found_raises_404(self) -> None:
        """Deleting a non-existent session raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.soft_delete.return_value = None

        with pytest.raises(NotFoundError):
            await service.delete_session(
                org_id=self.ORG_ID,
                session_id=uuid4(),
            )
