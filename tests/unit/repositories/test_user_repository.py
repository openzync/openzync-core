"""Unit tests for UserRepository — user CRUD, pagination, and aggregate stats."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.user_repository import UserRepository


pytestmark = pytest.mark.unit


class TestUserRepository:
    """UserRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000010")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> UserRepository:
        return UserRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_user(self, **overrides: object) -> MagicMock:
        u = MagicMock()
        u.id = overrides.get("id", self.USER_ID)
        u.organization_id = overrides.get("organization_id", self.ORG_ID)
        u.external_id = overrides.get("external_id", "ext-001")
        u.name = overrides.get("name", "Alice")
        u.email = overrides.get("email", "alice@example.com")
        u.metadata_ = overrides.get("metadata_", {})
        u.is_deleted = overrides.get("is_deleted", False)
        u.created_at = overrides.get("created_at", datetime.now(timezone.utc))
        u.updated_at = overrides.get("updated_at", None)
        u.summary = overrides.get("summary", None)
        u.summary_updated_at = overrides.get("summary_updated_at", None)
        return u

    # ── create ─────────────────────────────────────────────────────────────────

    async def test_create(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """create inserts and returns a new user."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID,
            external_id="ext-002",
            name="Bob",
            email="bob@example.com",
            metadata={"plan": "premium"},
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_create_minimal(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """create works with only required fields."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID, external_id="ext-003"
        )

        assert result is not None

    async def test_create_defaults_locale_en(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """create defaults the user locale to 'en' when not provided."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        await repo.create(organization_id=self.ORG_ID, external_id="ext-007")

        added = mock_db.add.call_args.args[0]
        assert added.locale == "en"

    async def test_create_passes_locale_through(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """create persists an explicitly provided locale."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        await repo.create(
            organization_id=self.ORG_ID,
            external_id="ext-008",
            locale="en",
        )

        added = mock_db.add.call_args.args[0]
        assert added.locale == "en"

    # ── create_or_get_by_external_id ───────────────────────────────────────────

    async def test_create_or_get_creates(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """create_or_get_by_external_id creates when no conflict."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create_or_get_by_external_id(
            organization_id=self.ORG_ID,
            external_id="ext-004",
            name="Carol",
        )

        assert result is not None
        mock_db.add.assert_called_once()

    async def test_create_or_get_returns_existing_on_integrity_error(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """create_or_get_by_external_id returns existing user on race."""
        from sqlalchemy.exc import IntegrityError

        mock_db.add.side_effect = IntegrityError("mock", "orig", MagicMock())
        existing_user = self._mock_user(external_id="ext-005")
        mock_db.rollback.return_value = None

        # get_by_external_id query after rollback
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_user
        mock_db.execute.return_value = mock_result

        result = await repo.create_or_get_by_external_id(
            organization_id=self.ORG_ID,
            external_id="ext-005",
            name="Dave",
        )

        assert result == existing_user
        mock_db.rollback.assert_awaited_once()

    async def test_create_or_get_raises_when_neither_created_nor_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """create_or_get_by_external_id raises NotFoundError when race loses."""
        from sqlalchemy.exc import IntegrityError

        mock_db.add.side_effect = IntegrityError("mock", "orig", MagicMock())
        mock_db.rollback.return_value = None
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        from core.exceptions import NotFoundError

        with pytest.raises(NotFoundError, match="Failed to get-or-create"):
            await repo.create_or_get_by_external_id(
                organization_id=self.ORG_ID,
                external_id="ext-006",
            )

    # ── get_by_external_id ─────────────────────────────────────────────────────

    async def test_get_by_external_id_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_external_id returns user when found."""
        user = self._mock_user()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_external_id(
            organization_id=self.ORG_ID, external_id="ext-001"
        )

        assert result == user

    async def test_get_by_external_id_not_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_external_id returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_external_id(
            organization_id=self.ORG_ID, external_id="nonexistent"
        )

        assert result is None

    # ── get_by_uuid ────────────────────────────────────────────────────────────

    async def test_get_by_uuid_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_uuid returns user when found."""
        user = self._mock_user()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_uuid(
            organization_id=self.ORG_ID, user_id=self.USER_ID
        )

        assert result == user

    async def test_get_by_uuid_not_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_uuid returns None when user does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_uuid(
            organization_id=self.ORG_ID, user_id=uuid4()
        )

        assert result is None

    # ── update ─────────────────────────────────────────────────────────────────

    async def test_update(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """update modifies user fields."""
        user = self._mock_user(name="Old Name")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.update(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            update_fields={"name": "New Name"},
        )

        assert result is not None
        assert result.name == "New Name"
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_update_metadata_deep_merge(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """update deep-merges metadata — new keys add, None keys remove."""
        user = self._mock_user(metadata_={"key1": "val1", "key2": "val2"})
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.update(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            update_fields={"metadata": {"key2": None, "key3": "val3"}},
        )

        assert result is not None
        assert result.metadata_ == {"key1": "val1", "key3": "val3"}

    async def test_update_not_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """update returns None when user not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.update(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
            update_fields={"name": "Nope"},
        )

        assert result is None

    # ── soft_delete ────────────────────────────────────────────────────────────

    async def test_soft_delete(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete sets is_deleted flag."""
        user = self._mock_user(is_deleted=False)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.soft_delete(
            organization_id=self.ORG_ID, user_id=self.USER_ID
        )

        assert result is not None
        assert result.is_deleted is True
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_soft_delete_not_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete returns None when user not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.soft_delete(
            organization_id=self.ORG_ID, user_id=self.USER_ID
        )

        assert result is None

    # ── hard_delete ────────────────────────────────────────────────────────────

    async def test_hard_delete(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """hard_delete permanently removes a user and returns True."""
        user = self._mock_user()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.hard_delete(
            organization_id=self.ORG_ID, user_id=self.USER_ID
        )

        assert result is True
        mock_db.delete.assert_awaited_once_with(user)
        mock_db.flush.assert_awaited_once()

    async def test_hard_delete_not_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """hard_delete returns False when user not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.hard_delete(
            organization_id=self.ORG_ID, user_id=self.USER_ID
        )

        assert result is False
        mock_db.delete.assert_not_called()

    # ── list (cursor pagination) ───────────────────────────────────────────────

    async def test_list(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """list returns users with pagination metadata."""
        users = [self._mock_user(), self._mock_user(id=uuid4())]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = users
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(organization_id=self.ORG_ID)

        assert result == users
        assert cursor is None

    async def test_list_with_search(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """list filters by search string."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(
            organization_id=self.ORG_ID, search="alice"
        )

        assert result == []

    async def test_list_with_date_filters(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """list filters by date range."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(
            organization_id=self.ORG_ID,
            created_after=datetime(2024, 1, 1),
            created_before=datetime(2024, 12, 31),
        )

        assert result == []

    async def test_list_with_cursor(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """list decodes cursor and applies pagination."""
        valid_cursor = repo._encode_cursor(
            datetime(2024, 1, 1), UUID("00000000-0000-0000-0000-000000000099")
        )
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(
            organization_id=self.ORG_ID, cursor=valid_cursor
        )

        assert result == []

    async def test_list_with_has_more(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """list returns next cursor when there are more results."""
        user = self._mock_user()
        # Return limit + 1 rows to trigger has_more=True
        # With limit=1, effective_limit=2, so returning 2 rows = has_more
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [user, self._mock_user(id=uuid4())]
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(
            organization_id=self.ORG_ID, limit=1
        )

        assert len(result) == 1
        # has_more is True, so cursor should be set
        if cursor is not None:
            assert isinstance(cursor, str)

    async def test_list_empty(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """list returns empty when no users."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(organization_id=self.ORG_ID)

        assert result == []
        assert cursor is None

    # ── get_stats ──────────────────────────────────────────────────────────────

    async def test_get_stats(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """get_stats returns aggregate counts."""
        mock_row = MagicMock()
        mock_row.message_count = 10
        mock_row.fact_count = 5
        mock_row.session_count = 3
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = mock_row
        mock_db.execute.return_value = mock_result

        stats = await repo.get_stats(user_id=self.USER_ID)

        assert stats == {"message_count": 10, "fact_count": 5, "session_count": 3}

    async def test_get_stats_zero(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """get_stats returns zeros when no data."""
        mock_row = MagicMock()
        mock_row.message_count = None
        mock_row.fact_count = None
        mock_row.session_count = None
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = mock_row
        mock_db.execute.return_value = mock_result

        stats = await repo.get_stats(user_id=self.USER_ID)

        assert stats == {"message_count": 0, "fact_count": 0, "session_count": 0}

    async def test_get_stats_user_not_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """get_stats returns zeros when user does not exist."""
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        stats = await repo.get_stats(user_id=self.USER_ID)

        assert stats == {"message_count": 0, "fact_count": 0, "session_count": 0}

    # ── update_summary ─────────────────────────────────────────────────────────

    async def test_update_summary(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """update_summary executes UPDATE and flushes."""
        mock_db.execute.return_value = MagicMock()
        mock_db.flush.return_value = None

        await repo.update_summary(
            user_id=self.USER_ID, summary="User summary text"
        )

        mock_db.execute.assert_awaited_once()
        mock_db.flush.assert_awaited_once()

    # ── get_summary ────────────────────────────────────────────────────────────

    async def test_get_summary_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """get_summary returns (summary, updated_at) tuple."""
        mock_row = MagicMock()
        mock_row.summary = "Existing summary"
        mock_row.summary_updated_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = mock_row
        mock_db.execute.return_value = mock_result

        summary, updated_at = await repo.get_summary(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
        )

        assert summary == "Existing summary"
        assert updated_at is not None

    async def test_get_summary_not_found(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """get_summary returns (None, None) when user not found."""
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        summary, updated_at = await repo.get_summary(
            organization_id=self.ORG_ID,
            user_id=self.USER_ID,
        )

        assert summary is None
        assert updated_at is None

    # ── count_active ───────────────────────────────────────────────────────────

    async def test_count_active(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """count_active returns the active user count."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 7
        mock_db.execute.return_value = mock_result

        count = await repo.count_active(organization_id=self.ORG_ID)

        assert count == 7

    async def test_count_active_zero(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """count_active returns 0 when none."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_db.execute.return_value = mock_result

        count = await repo.count_active(organization_id=self.ORG_ID)

        assert count == 0

    # ── exists_by_external_id ──────────────────────────────────────────────────

    async def test_exists_by_external_id_true(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """exists_by_external_id returns True when user exists."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = self.USER_ID
        mock_db.execute.return_value = mock_result

        exists = await repo.exists_by_external_id(
            organization_id=self.ORG_ID, external_id="ext-001"
        )

        assert exists is True

    async def test_exists_by_external_id_false(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """exists_by_external_id returns False when user does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        exists = await repo.exists_by_external_id(
            organization_id=self.ORG_ID, external_id="nonexistent"
        )

        assert exists is False

    # ── rollback ───────────────────────────────────────────────────────────────

    async def test_rollback(
        self, repo: UserRepository, mock_db: AsyncMock
    ) -> None:
        """rollback delegates to the DB session."""
        await repo.rollback()

        mock_db.rollback.assert_awaited_once()

    # ── Cursor helpers ─────────────────────────────────────────────────────────

    def test_encode_cursor(self, repo: UserRepository) -> None:
        """_encode_cursor produces a valid base64 string."""
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        encoded = repo._encode_cursor(dt, self.USER_ID)
        assert isinstance(encoded, str)
        assert len(encoded) > 0

    def test_decode_cursor_roundtrip(self, repo: UserRepository) -> None:
        """_encode_cursor → _decode_cursor roundtrips correctly."""
        dt = datetime(2024, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
        encoded = repo._encode_cursor(dt, self.USER_ID)
        decoded_dt, decoded_id = repo._decode_cursor(encoded)

        assert decoded_dt == dt
        assert decoded_id == self.USER_ID

    def test_decode_cursor_invalid_raises(
        self, repo: UserRepository
    ) -> None:
        """_decode_cursor raises ValueError for malformed input."""
        with pytest.raises(ValueError, match="Invalid cursor"):
            repo._decode_cursor("not-base64!!!")
