"""Unit tests for UserService — business logic with mocked repository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from core.exceptions import ConflictError, NotFoundError
from services.user_service import UserService


@pytest.mark.unit
class TestUserService:
    """UserService unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")
    USER_ID_STR = "00000000-0000-0000-0000-000000000002"

    def _make_service(self) -> tuple[UserService, AsyncMock]:
        mock_repo = AsyncMock()
        service = UserService(repo=mock_repo)
        return service, mock_repo

    def _mock_user(self, **kwargs) -> MagicMock:
        """Mock a User ORM object with the attributes the service accesses."""
        user = MagicMock()
        user.id = kwargs.get("id", self.USER_ID)
        user.organization_id = kwargs.get("org_id", self.ORG_ID)
        user.external_id = kwargs.get("external_id", "test-user")
        user.name = kwargs.get("name", "Test User")
        user.email = kwargs.get("email", "test@example.com")
        user.metadata_ = kwargs.get("metadata", {})
        user.is_active = kwargs.get("is_active", True)
        user.is_deleted = kwargs.get("is_deleted", False)
        user.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
        user.updated_at = kwargs.get("updated_at", datetime.now(timezone.utc))
        return user

    @pytest.mark.asyncio
    async def test_create_user(self) -> None:
        """Creating a user returns the response."""
        service, mock_repo = self._make_service()
        mock_repo.exists_by_external_id.return_value = False
        mock_repo.create.return_value = self._mock_user(external_id="new-user")

        result = await service.create_user(
            organization_id=self.ORG_ID,
            external_id="new-user",
            name="New User",
        )
        assert result.external_id == "new-user"
        mock_repo.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_user_duplicate_raises_conflict(self) -> None:
        """Creating a duplicate user raises ConflictError."""
        service, mock_repo = self._make_service()
        mock_repo.exists_by_external_id.return_value = True

        with pytest.raises(ConflictError):
            await service.create_user(
                organization_id=self.ORG_ID,
                external_id="existing-user",
            )

    @pytest.mark.asyncio
    async def test_get_user_found(self) -> None:
        """Getting a user by UUID returns the response."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_uuid.return_value = self._mock_user()
        mock_repo.get_stats.return_value = {
            "message_count": 0, "fact_count": 0, "session_count": 0,
        }

        result = await service.get_user(self.ORG_ID, self.USER_ID)
        assert result.id == self.USER_ID

    @pytest.mark.asyncio
    async def test_get_user_not_found_raises_404(self) -> None:
        """Getting a non-existent user raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_uuid.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_user(self.ORG_ID, uuid4())

    @pytest.mark.asyncio
    async def test_delete_user(self) -> None:
        """Deleting a user calls soft_delete (returns None on success)."""
        service, mock_repo = self._make_service()
        mock_repo.soft_delete.return_value = self._mock_user()

        # delete_user returns None on success (raises on failure)
        result = await service.delete_user(self.ORG_ID, self.USER_ID)
        assert result is None
        mock_repo.soft_delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_user_not_found_raises_404(self) -> None:
        """Deleting a non-existent user raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.soft_delete.return_value = None

        with pytest.raises(NotFoundError):
            await service.delete_user(self.ORG_ID, uuid4())

    @pytest.mark.asyncio
    async def test_update_user(self) -> None:
        """Updating a user returns the updated response."""
        service, mock_repo = self._make_service()
        mock_repo.update.return_value = self._mock_user(name="Updated Name")

        result = await service.update_user(
            self.ORG_ID, self.USER_ID, {"name": "Updated Name"}
        )
        assert result.name == "Updated Name"

    @pytest.mark.asyncio
    async def test_list_users_paginated(self) -> None:
        """Listing users returns paginated response."""
        service, mock_repo = self._make_service()
        mock_users = [self._mock_user(external_id=f"user-{i}") for i in range(3)]
        mock_repo.list.return_value = (mock_users, "next-cursor")

        result = await service.list_users(self.ORG_ID, limit=3)
        assert len(result.data) == 3
        assert result.next_cursor == "next-cursor"
