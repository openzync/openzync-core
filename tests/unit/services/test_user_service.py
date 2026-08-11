"""Unit tests for UserService — user CRUD, get-or-create with race handling.

All external dependencies (repository) are mocked at the service boundary.
The ``_user_to_dict`` helper is tested directly — pure transformation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.exceptions import ConflictError, NotFoundError, ValidationError
from schemas.users import UserListResponse, UserResponse, UserResponseWithStats
from services.user_service import UserService


@pytest.mark.unit
class TestUserService:
    """Unit tests for ``UserService`` — CRUD, get-or-create, pagination."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000010")
    EXTERNAL_ID = "user_abc123"
    OTHER_USER_ID = UUID("00000000-0000-0000-0000-0000000000aa")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[UserService, AsyncMock]:
        """Create ``UserService`` with mocked repository."""
        mock_repo = AsyncMock()
        service = UserService(repo=mock_repo)
        return service, mock_repo

    def _make_service_with_webhook(
        self,
    ) -> tuple[UserService, AsyncMock, AsyncMock]:
        """Create ``UserService`` with mocked repository and webhook service."""
        mock_repo = AsyncMock()
        mock_webhook = AsyncMock()
        service = UserService(repo=mock_repo, webhook_service=mock_webhook)
        return service, mock_repo, mock_webhook

    def _make_user(
        self,
        user_id: UUID | None = None,
        org_id: UUID | None = None,
        external_id: str | None = None,
        name: str | None = "Alice",
        email: str | None = "alice@example.com",
        metadata: dict | None = None,
        is_active: bool = True,
        is_deleted: bool = False,
        role: str = "member",
    ) -> MagicMock:
        """Build a MagicMock mimicking a User ORM model."""
        user = MagicMock()
        user.id = user_id or self.USER_ID
        user.organization_id = org_id or self.ORG_ID
        user.external_id = external_id or self.EXTERNAL_ID
        user.name = name
        user.email = email
        user.metadata_ = metadata or {}
        user.role = role
        user.is_active = is_active
        user.is_deleted = is_deleted
        user.created_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
        user.updated_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
        return user

    def _make_stats(
        self,
        message_count: int = 5,
        fact_count: int = 3,
        session_count: int = 2,
    ) -> dict[str, int]:
        """Return a mock stats dict as returned by ``repo.get_stats``."""
        return {
            "message_count": message_count,
            "fact_count": fact_count,
            "session_count": session_count,
        }

    # ── create_user ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_create_user_success_no_webhook(self) -> None:
        """``create_user`` returns a ``UserResponse`` without emitting webhooks
        when no webhook service is configured."""
        service, mock_repo = self._make_service()
        mock_repo.exists_by_external_id.return_value = False
        mock_repo.create.return_value = self._make_user(
            metadata={"source": "onboarding"},
        )

        result = await service.create_user(
            organization_id=self.ORG_ID,
            external_id=self.EXTERNAL_ID,
            name="Alice",
            email="alice@example.com",
            metadata={"source": "onboarding"},
        )

        assert isinstance(result, UserResponse)
        assert result.external_id == self.EXTERNAL_ID
        assert result.name == "Alice"
        assert result.email == "alice@example.com"
        assert result.metadata == {"source": "onboarding"}
        assert result.organization_id == self.ORG_ID
        mock_repo.exists_by_external_id.assert_awaited_once_with(
            self.ORG_ID, self.EXTERNAL_ID
        )
        mock_repo.create.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            external_id=self.EXTERNAL_ID,
            name="Alice",
            email="alice@example.com",
            metadata={"source": "onboarding"},
            role="member",
        )

    @pytest.mark.asyncio
    async def test_create_user_with_webhook_emits_event(self) -> None:
        """``create_user`` emits ``USER_CREATED`` via webhook service when
        ``webhook_service`` is configured."""
        service, mock_repo, mock_webhook = self._make_service_with_webhook()
        mock_repo.exists_by_external_id.return_value = False
        user = self._make_user()
        mock_repo.create.return_value = user

        result = await service.create_user(
            organization_id=self.ORG_ID,
            external_id=self.EXTERNAL_ID,
        )

        assert isinstance(result, UserResponse)
        mock_webhook.emit.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            event_type="user.created",
            payload={
                "user_id": str(user.id),
                "external_id": self.EXTERNAL_ID,
            },
        )

    @pytest.mark.asyncio
    async def test_create_user_duplicate_external_id_raises(self) -> None:
        """``create_user`` raises ``ConflictError`` when a user with the
        given ``external_id`` already exists in the organization."""
        service, mock_repo = self._make_service()
        mock_repo.exists_by_external_id.return_value = True

        with pytest.raises(ConflictError) as exc:
            await service.create_user(
                organization_id=self.ORG_ID,
                external_id=self.EXTERNAL_ID,
            )

        assert self.EXTERNAL_ID in str(exc.value)
        assert str(self.ORG_ID) in str(exc.value)
        mock_repo.create.assert_not_awaited()

    # ── _user_to_dict ───────────────────────────────────────────────────────

    def test_user_to_dict_maps_metadata_correctly(self) -> None:
        """``_user_to_dict`` converts the ``metadata_`` attribute to
        ``metadata`` key in the output dict."""
        service, _ = self._make_service()
        user = self._make_user(metadata={"plan": "pro"})

        result = service._user_to_dict(user)

        assert result["metadata"] == {"plan": "pro"}
        assert "metadata_" not in result
        assert result["id"] == self.USER_ID
        assert result["organization_id"] == self.ORG_ID
        assert result["external_id"] == self.EXTERNAL_ID
        assert result["name"] == "Alice"
        assert result["email"] == "alice@example.com"
        assert result["is_active"] is True
        assert result["is_deleted"] is False
        assert result["created_at"] is not None
        assert result["updated_at"] is not None

    def test_user_to_dict_handles_none_metadata(self) -> None:
        """``_user_to_dict`` returns an empty dict for ``metadata`` when
        the ORM attribute is ``None``."""
        service, _ = self._make_service()
        user = self._make_user(metadata=None)

        result = service._user_to_dict(user)

        assert result["metadata"] == {}

    # ── get_or_create_user ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_or_create_user_fast_path_existing(self) -> None:
        """``get_or_create_user`` returns the existing user without creating
        when the user is found by ``external_id``."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_external_id.return_value = self._make_user()

        result = await service.get_or_create_user(
            organization_id=self.ORG_ID,
            external_id=self.EXTERNAL_ID,
        )

        assert isinstance(result, UserResponse)
        assert result.external_id == self.EXTERNAL_ID
        mock_repo.get_by_external_id.assert_awaited_once_with(
            self.ORG_ID, self.EXTERNAL_ID
        )
        mock_repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_or_create_user_creates_new(self) -> None:
        """``get_or_create_user`` creates and returns a new user when no
        existing user is found (no race condition)."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_external_id.return_value = None
        mock_repo.create.return_value = self._make_user()

        result = await service.get_or_create_user(
            organization_id=self.ORG_ID,
            external_id=self.EXTERNAL_ID,
            name="Alice",
            email="alice@example.com",
        )

        assert isinstance(result, UserResponse)
        assert result.external_id == self.EXTERNAL_ID
        mock_repo.get_by_external_id.assert_awaited_once_with(
            self.ORG_ID, self.EXTERNAL_ID
        )
        mock_repo.create.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            external_id=self.EXTERNAL_ID,
            name="Alice",
            email="alice@example.com",
            metadata=None,
        )

    @pytest.mark.asyncio
    async def test_get_or_create_user_integrity_error_race(self) -> None:
        """``get_or_create_user`` handles ``IntegrityError`` by rolling back
        and refetching when a concurrent insert wins the race."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_external_id.side_effect = [
            None,  # First call — not found
            self._make_user(),  # Refetch after rollback — found
        ]
        mock_repo.create.side_effect = __import__(
            "sqlalchemy"
        ).exc.IntegrityError("stmt", {}, Exception("unique constraint"))

        with patch.object(service, "_webhook_service", None):
            result = await service.get_or_create_user(
                organization_id=self.ORG_ID,
                external_id=self.EXTERNAL_ID,
            )

        assert isinstance(result, UserResponse)
        assert result.external_id == self.EXTERNAL_ID
        mock_repo.rollback.assert_awaited_once()
        assert mock_repo.get_by_external_id.await_count == 2
        mock_repo.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_or_create_user_integrity_error_then_not_found(
        self,
    ) -> None:
        """``get_or_create_user`` raises ``NotFoundError`` when
        ``IntegrityError`` occurs but the refetch returns ``None``
        (should never happen — indicates DB inconsistency)."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_external_id.side_effect = [None, None]
        mock_repo.create.side_effect = __import__(
            "sqlalchemy"
        ).exc.IntegrityError("stmt", {}, Exception("unique constraint"))

        with pytest.raises(NotFoundError) as exc:
            await service.get_or_create_user(
                organization_id=self.ORG_ID,
                external_id=self.EXTERNAL_ID,
            )

        assert self.EXTERNAL_ID in str(exc.value)
        assert "IntegrityError" in str(exc.value)
        mock_repo.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_or_create_user_new_emits_webhook(self) -> None:
        """``get_or_create_user`` emits ``USER_CREATED`` webhook when a new
        user is created and ``webhook_service`` is configured."""
        service, mock_repo, mock_webhook = self._make_service_with_webhook()
        mock_repo.get_by_external_id.return_value = None
        user = self._make_user()
        mock_repo.create.return_value = user

        result = await service.get_or_create_user(
            organization_id=self.ORG_ID,
            external_id=self.EXTERNAL_ID,
        )

        assert isinstance(result, UserResponse)
        mock_webhook.emit.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            event_type="user.created",
            payload={
                "user_id": str(user.id),
                "external_id": self.EXTERNAL_ID,
            },
        )

    # ── get_user ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_user_success_with_stats(self) -> None:
        """``get_user`` returns a ``UserResponseWithStats`` with aggregate
        counts when the user exists and is not deleted."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_uuid.return_value = self._make_user()
        mock_repo.get_stats.return_value = self._make_stats(
            message_count=10, fact_count=4, session_count=3
        )

        result = await service.get_user(
            organization_id=self.ORG_ID, user_id=self.USER_ID
        )

        assert isinstance(result, UserResponseWithStats)
        assert result.id == self.USER_ID
        assert result.message_count == 10
        assert result.fact_count == 4
        assert result.session_count == 3
        mock_repo.get_by_uuid.assert_awaited_once_with(
            self.ORG_ID, self.USER_ID
        )
        mock_repo.get_stats.assert_awaited_once_with(self.USER_ID)

    @pytest.mark.asyncio
    async def test_get_user_deleted_raises_not_found(self) -> None:
        """``get_user`` raises ``NotFoundError`` when the user is
        soft-deleted (``is_deleted`` is ``True``)."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_uuid.return_value = self._make_user(is_deleted=True)

        with pytest.raises(NotFoundError):
            await service.get_user(
                organization_id=self.ORG_ID, user_id=self.USER_ID
            )

        mock_repo.get_stats.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_user_not_found_raises(self) -> None:
        """``get_user`` raises ``NotFoundError`` when the user does not
        exist."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_uuid.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_user(
                organization_id=self.ORG_ID, user_id=self.USER_ID
            )

    # ── update_user ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_user_success(self) -> None:
        """``update_user`` returns the updated ``UserResponse``."""
        service, mock_repo = self._make_service()
        updated_user = self._make_user(name="Updated Alice")
        mock_repo.update.return_value = updated_user

        result = await service.update_user(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            update_fields={"name": "Updated Alice"},
            actor_user_id=self.USER_ID,  # self-change guard only applies to role
        )

        assert isinstance(result, UserResponse)
        assert result.name == "Updated Alice"
        mock_repo.update.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            update_fields={"name": "Updated Alice"},
        )

    @pytest.mark.asyncio
    async def test_update_user_not_found_raises(self) -> None:
        """``update_user`` raises ``NotFoundError`` when the user does not
        exist."""
        service, mock_repo = self._make_service()
        mock_repo.update.return_value = None

        with pytest.raises(NotFoundError):
            await service.update_user(
                organization_id=self.ORG_ID,
                user_id=self.USER_ID,
                update_fields={"name": "Ghost"},
                actor_user_id=self.USER_ID,
            )

    # ── delete_user ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_user_success(self) -> None:
        """``delete_user`` calls ``repo.soft_delete`` and returns ``None``
        on success."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_uuid.return_value = self._make_user()
        mock_repo.soft_delete.return_value = self._make_user()

        result = await service.delete_user(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            actor_user_id=self.OTHER_USER_ID,
        )

        assert result is None
        mock_repo.soft_delete.assert_awaited_once_with(
            organization_id=self.ORG_ID, user_id=self.USER_ID
        )

    @pytest.mark.asyncio
    async def test_delete_user_not_found_raises(self) -> None:
        """``delete_user`` raises ``NotFoundError`` when the user does not
        exist."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_uuid.return_value = None
        mock_repo.soft_delete.return_value = None

        with pytest.raises(NotFoundError):
            await service.delete_user(
                organization_id=self.ORG_ID,
                user_id=self.USER_ID,
                actor_user_id=self.OTHER_USER_ID,
            )

    # ── list_users ───────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_list_users_default_pagination(self) -> None:
        """``list_users`` returns a ``UserListResponse`` with default limit."""
        service, mock_repo = self._make_service()
        users = [self._make_user(external_id=f"user_{i}") for i in range(3)]
        mock_repo.list.return_value = (users, None)

        result = await service.list_users(organization_id=self.ORG_ID)

        assert isinstance(result, UserListResponse)
        assert len(result.data) == 3
        assert result.has_more is False
        assert result.next_cursor is None
        mock_repo.list.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            limit=50,
            cursor=None,
            search=None,
            created_after=None,
            created_before=None,
        )

    @pytest.mark.asyncio
    async def test_list_users_with_filters(self) -> None:
        """``list_users`` passes through cursor, search, and date range
        filters to the repository."""
        service, mock_repo = self._make_service()
        cursor = "some-cursor-value"
        search = "alice"
        created_after = datetime(2025, 1, 1, tzinfo=timezone.utc)
        created_before = datetime(2025, 12, 31, tzinfo=timezone.utc)

        users = [self._make_user()]
        mock_repo.list.return_value = (users, "next-cursor")

        result = await service.list_users(
            organization_id=self.ORG_ID,
            limit=10,
            cursor=cursor,
            search=search,
            created_after=created_after,
            created_before=created_before,
        )

        assert len(result.data) == 1
        assert result.has_more is True
        assert result.next_cursor == "next-cursor"
        mock_repo.list.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            limit=10,
            cursor=cursor,
            search=search,
            created_after=created_after,
            created_before=created_before,
        )

    @pytest.mark.asyncio
    async def test_list_users_invalid_limit_too_low_raises(self) -> None:
        """``list_users`` raises ``ValidationError`` when limit is less than 1."""
        service, _ = self._make_service()

        with pytest.raises(ValidationError):
            await service.list_users(organization_id=self.ORG_ID, limit=0)

    @pytest.mark.asyncio
    async def test_list_users_invalid_limit_too_high_raises(self) -> None:
        """``list_users`` raises ``ValidationError`` when limit exceeds 200."""
        service, _ = self._make_service()

        with pytest.raises(ValidationError):
            await service.list_users(organization_id=self.ORG_ID, limit=201)


@pytest.mark.unit
class TestUserServiceRoleGuards:
    """Role-change / self-delete guards on ``update_user`` and ``delete_user``.

    Observed contract:
    - Changing your own role → ``ValidationError``.
    - Deleting your own account → ``ValidationError``.
    - Demoting/deleting the LAST active admin → ``ValidationError``.
    - A successful role change invalidates the target's cached RBAC role.
    """

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    ADMIN_ID = UUID("00000000-0000-0000-0000-000000000010")
    OTHER_ADMIN_ID = UUID("00000000-0000-0000-0000-000000000011")
    MEMBER_ID = UUID("00000000-0000-0000-0000-000000000012")

    def _make_service(
        self, with_redis: bool = False,
    ) -> tuple[UserService, AsyncMock, AsyncMock | None]:
        """Build a ``UserService`` with mocked repo and optional Redis."""
        mock_repo = AsyncMock()
        mock_redis = AsyncMock() if with_redis else None
        service = UserService(repo=mock_repo, redis=mock_redis)
        return service, mock_repo, mock_redis

    def _make_user(self, role: str = "member", user_id: UUID | None = None) -> MagicMock:
        """Build a User ORM mock with the fields ``_user_to_dict`` reads."""
        user = MagicMock()
        user.id = user_id or self.MEMBER_ID
        user.organization_id = self.ORG_ID
        user.external_id = "user_x"
        user.name = "A User"
        user.email = "a@example.com"
        user.metadata_ = {}
        user.role = role
        user.is_active = True
        user.is_deleted = False
        user.created_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
        user.updated_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
        return user

    # ── Self-change guard ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_changing_own_role_rejected(self) -> None:
        """Actor changing their OWN role → ValidationError, no repo update."""
        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_uuid.return_value = MagicMock(role="admin")

        with pytest.raises(ValidationError) as exc:
            await service.update_user(
                organization_id=self.ORG_ID,
                user_id=self.ADMIN_ID,
                update_fields={"role": "member"},
                actor_user_id=self.ADMIN_ID,  # same user
            )

        assert str(exc.value) == "You cannot change your own role."
        mock_repo.update.assert_not_awaited()
        mock_repo.count_active_admins.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_promotion_also_rejected(self) -> None:
        """Self-promotion is rejected too — role changes on self are blocked."""
        service, mock_repo, _ = self._make_service()

        with pytest.raises(ValidationError):
            await service.update_user(
                organization_id=self.ORG_ID,
                user_id=self.MEMBER_ID,
                update_fields={"role": "admin"},
                actor_user_id=self.MEMBER_ID,
            )
        mock_repo.update.assert_not_awaited()

    # ── Last-admin guards ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_demoting_last_admin_rejected(self) -> None:
        """Demoting the org's last admin → ValidationError."""
        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_uuid.return_value = MagicMock(role="admin")
        mock_repo.count_active_admins.return_value = 1  # last admin

        with pytest.raises(ValidationError) as exc:
            await service.update_user(
                organization_id=self.ORG_ID,
                user_id=self.ADMIN_ID,
                update_fields={"role": "member"},
                actor_user_id=self.OTHER_ADMIN_ID,
            )

        assert "last admin" in str(exc.value)
        mock_repo.count_active_admins.assert_awaited_once_with(self.ORG_ID)
        mock_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_demotion_allowed_when_more_than_one_admin(self) -> None:
        """Demoting an admin when another active admin exists → proceeds."""
        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_uuid.return_value = MagicMock(role="admin")
        mock_repo.count_active_admins.return_value = 2
        mock_repo.update.return_value = self._make_user(role="member")

        result = await service.update_user(
            organization_id=self.ORG_ID,
            user_id=self.ADMIN_ID,
            update_fields={"role": "member"},
            actor_user_id=self.OTHER_ADMIN_ID,
        )

        assert isinstance(result, UserResponse)
        assert result.role == "member"
        mock_repo.count_active_admins.assert_awaited_once_with(self.ORG_ID)
        mock_repo.update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deleting_last_admin_rejected(self) -> None:
        """Deleting the org's last admin → ValidationError."""
        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_uuid.return_value = MagicMock(role="admin")
        mock_repo.count_active_admins.return_value = 1

        with pytest.raises(ValidationError) as exc:
            await service.delete_user(
                organization_id=self.ORG_ID,
                user_id=self.ADMIN_ID,
                actor_user_id=self.OTHER_ADMIN_ID,
            )

        assert "last admin" in str(exc.value)
        mock_repo.soft_delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deleting_self_rejected(self) -> None:
        """Deleting your own account → ValidationError."""
        service, mock_repo, _ = self._make_service()

        with pytest.raises(ValidationError) as exc:
            await service.delete_user(
                organization_id=self.ORG_ID,
                user_id=self.ADMIN_ID,
                actor_user_id=self.ADMIN_ID,
            )

        assert str(exc.value) == "You cannot delete your own account."
        mock_repo.soft_delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_admin_allowed_when_more_than_one(self) -> None:
        """Deleting an admin when another active admin exists → proceeds."""
        service, mock_repo, _ = self._make_service()
        mock_repo.get_by_uuid.return_value = MagicMock(role="admin")
        mock_repo.count_active_admins.return_value = 2
        mock_repo.soft_delete.return_value = MagicMock(role="admin")

        result = await service.delete_user(
            organization_id=self.ORG_ID,
            user_id=self.ADMIN_ID,
            actor_user_id=self.OTHER_ADMIN_ID,
        )

        assert result is None
        mock_repo.count_active_admins.assert_awaited_once_with(self.ORG_ID)
        mock_repo.soft_delete.assert_awaited_once()

    # ── Role-cache invalidation ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_role_change_invalidates_cached_role(self) -> None:
        """A successful role change invalidates the target's RBAC cache key."""
        service, mock_repo, _mock_redis = self._make_service(with_redis=True)
        mock_repo.get_by_uuid.return_value = self._make_user(role="member")
        mock_repo.update.return_value = self._make_user(role="admin")

        with patch(
            "services.user_service.invalidate_role", new=AsyncMock(),
        ) as mock_invalidate:
            await service.update_user(
                organization_id=self.ORG_ID,
                user_id=self.MEMBER_ID,
                update_fields={"role": "admin"},
                actor_user_id=self.ADMIN_ID,
            )

        mock_invalidate.assert_awaited_once_with(_mock_redis, self.MEMBER_ID)

    @pytest.mark.asyncio
    async def test_delete_invalidates_cached_role(self) -> None:
        """Deleting a user invalidates their cached RBAC role."""
        service, mock_repo, _mock_redis = self._make_service(with_redis=True)
        mock_repo.get_by_uuid.return_value = MagicMock(role="member")
        mock_repo.soft_delete.return_value = MagicMock(role="member")

        with patch(
            "services.user_service.invalidate_role", new=AsyncMock(),
        ) as mock_invalidate:
            await service.delete_user(
                organization_id=self.ORG_ID,
                user_id=self.MEMBER_ID,
                actor_user_id=self.ADMIN_ID,
            )

        mock_invalidate.assert_awaited_once_with(_mock_redis, self.MEMBER_ID)


class TestUserServiceSuperadminSurface:
    """update_member_role — the platform superadmin cross-org role change."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000010")

    def _make_service(self) -> tuple[UserService, AsyncMock]:
        """Create ``UserService`` with mocked repository and redis."""
        mock_repo = AsyncMock()
        service = UserService(repo=mock_repo, redis=AsyncMock())
        return service, mock_repo

    def _make_user(self) -> MagicMock:
        user = MagicMock()
        user.id = self.USER_ID
        user.organization_id = self.ORG_ID
        user.role = "admin"
        return user

    @pytest.mark.asyncio
    async def test_update_member_role_persists_and_returns_user(self) -> None:
        """update_member_role delegates to repo.update with the role field."""
        service, mock_repo = self._make_service()
        mock_repo.update.return_value = self._make_user()

        user = await service.update_member_role(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            role="admin",
        )

        assert user.role == "admin"
        mock_repo.update.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            update_fields={"role": "admin"},
        )

    @pytest.mark.asyncio
    async def test_update_member_role_missing_user_raises_not_found(self) -> None:
        """A user not in the org → NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.update.return_value = None

        with pytest.raises(NotFoundError):
            await service.update_member_role(
                organization_id=self.ORG_ID,
                user_id=self.USER_ID,
                role="member",
            )

        mock_repo.update.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            update_fields={"role": "member"},
        )

    @pytest.mark.asyncio
    async def test_update_member_role_invalidates_cached_role(self) -> None:
        """The role cache is invalidated after the change."""
        service, mock_repo = self._make_service()
        mock_repo.update.return_value = self._make_user()

        with patch(
            "services.user_service.invalidate_role", new=AsyncMock()
        ) as mock_invalidate:
            await service.update_member_role(
                organization_id=self.ORG_ID,
                user_id=self.USER_ID,
                role="member",
            )

        mock_invalidate.assert_awaited_once_with(ANY, self.USER_ID)
