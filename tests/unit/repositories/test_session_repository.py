"""Unit tests for SessionRepository — session CRUD, cursor pagination, and stats."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.session_repository import SessionRepository


pytestmark = pytest.mark.unit


class TestSessionRepository:
    """SessionRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
    USER_ID = UUID("00000000-0000-0000-0000-000000000003")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000010")
    EPISODE_ID = UUID("00000000-0000-0000-0000-000000000020")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> SessionRepository:
        return SessionRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_session(self, **overrides: object) -> MagicMock:
        s = MagicMock()
        s.id = overrides.get("id", self.SESSION_ID)
        s.organization_id = overrides.get("organization_id", self.ORG_ID)
        s.project_id = overrides.get("project_id", self.PROJECT_ID)
        s.user_id = overrides.get("user_id", self.USER_ID)
        s.external_id = overrides.get("external_id", "ext-session-1")
        s.metadata_ = overrides.get("metadata_", {})
        s.is_deleted = overrides.get("is_deleted", False)
        s.closed_at = overrides.get("closed_at", None)
        s.created_at = overrides.get("created_at", datetime.now(timezone.utc))
        s.updated_at = overrides.get("updated_at", datetime.now(timezone.utc))
        return s

    def _mock_episode(self, **overrides: object) -> MagicMock:
        ep = MagicMock()
        ep.id = overrides.get("id", self.EPISODE_ID)
        ep.session_id = overrides.get("session_id", self.SESSION_ID)
        ep.sequence_number = overrides.get("sequence_number", 0)
        ep.is_deleted = overrides.get("is_deleted", False)
        ep.created_at = overrides.get("created_at", datetime.now(timezone.utc))
        return ep

    # ── create ─────────────────────────────────────────────────────────────────

    async def test_create(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """create inserts and returns a new session."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            created_by=self.USER_ID,
            external_id="new-session",
            metadata={"source": "api"},
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_create_minimal(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """create works with minimal required fields."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            created_by=self.USER_ID,
            external_id="minimal",
        )

        assert result is not None

    # ── get_or_create_default ──────────────────────────────────────────────────

    async def test_get_or_create_default_returns_existing(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_or_create_default returns existing default session."""
        default = self._mock_session(external_id="__default__")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = default
        mock_db.execute.return_value = mock_result

        result = await repo.get_or_create_default(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            created_by=self.USER_ID,
        )

        assert result == default
        mock_db.add.assert_not_called()

    async def test_get_or_create_default_creates_new(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_or_create_default creates a new default when none exists."""
        mock_not_found = MagicMock()
        mock_not_found.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_not_found
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.get_or_create_default(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            created_by=self.USER_ID,
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_get_or_create_default_handles_race(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_or_create_default recovers from IntegrityError race."""
        from sqlalchemy.exc import IntegrityError

        # First attempt to create — IntegrityError on flush
        mock_not_found = MagicMock()
        mock_not_found.scalar_one_or_none.return_value = None
        # Second attempt after rollback — found
        default = self._mock_session(external_id="__default__")
        mock_found = MagicMock()
        mock_found.scalar_one_or_none.return_value = default

        mock_db.execute.side_effect = [mock_not_found, mock_found]
        mock_db.add.return_value = None
        mock_db.flush.side_effect = IntegrityError("mock", "orig", MagicMock())
        mock_db.rollback.return_value = None

        result = await repo.get_or_create_default(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            created_by=self.USER_ID,
        )

        assert result == default

    # ── get_by_external_id ─────────────────────────────────────────────────────

    async def test_get_by_external_id_found(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_external_id returns session when found."""
        session = self._mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = session
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_external_id(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            external_id="ext-session-1",
        )

        assert result == session

    async def test_get_by_external_id_not_found(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_external_id returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_external_id(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            external_id="nonexistent",
        )

        assert result is None

    # ── get_by_uuid ────────────────────────────────────────────────────────────

    async def test_get_by_uuid_found(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_uuid returns session when found."""
        session = self._mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = session
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_uuid(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result == session

    async def test_get_by_uuid_not_found(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_uuid returns None when session does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_uuid(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result is None

    async def test_get_by_uuid_with_project(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_by_uuid filters by project_id when provided."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_by_uuid(
            org_id=self.ORG_ID,
            session_id=self.SESSION_ID,
            project_id=self.PROJECT_ID,
        )

        assert result is None
        mock_db.execute.assert_awaited_once()

    # ── list ───────────────────────────────────────────────────────────────────

    async def test_list(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """list returns sessions with pagination."""
        sessions = [self._mock_session(), self._mock_session(id=uuid4())]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = sessions
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(
            org_id=self.ORG_ID, project_id=self.PROJECT_ID
        )

        assert result == sessions
        assert cursor is None

    async def test_list_with_cursor(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """list decodes cursor and applies pagination."""
        valid_cursor = repo._encode_cursor(
            datetime(2024, 1, 1), UUID("00000000-0000-0000-0000-000000000099")
        )
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            cursor=valid_cursor,
        )

        assert result == []

    async def test_list_include_closed(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """list includes closed sessions when include_closed is True."""
        closed = self._mock_session(closed_at=datetime.now(timezone.utc))
        open_session = self._mock_session(id=uuid4())
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [closed, open_session]
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            include_closed=True,
        )

        assert len(result) == 2

    async def test_list_empty(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """list returns empty when no sessions."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.list(
            org_id=self.ORG_ID, project_id=self.PROJECT_ID
        )

        assert result == []
        assert cursor is None

    # ── get_messages ───────────────────────────────────────────────────────────

    async def test_get_messages(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_messages returns paginated messages for a session."""
        # Session check — found
        mock_session_check = MagicMock()
        mock_session_check.scalar_one_or_none.return_value = self.SESSION_ID
        # Messages
        episodes = [self._mock_episode(sequence_number=0)]
        mock_messages_result = MagicMock()
        mock_messages_result.scalars.return_value.all.return_value = episodes

        mock_db.execute.side_effect = [mock_session_check, mock_messages_result]

        result, cursor = await repo.get_messages(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result == episodes
        assert cursor is None

    async def test_get_messages_session_not_found(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_messages returns empty when session not in org scope."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result, cursor = await repo.get_messages(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result == []
        assert cursor is None

    async def test_get_messages_with_cursor(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_messages applies cursor pagination."""
        valid_cursor = repo._encode_message_cursor(
            5, UUID("00000000-0000-0000-0000-000000000099")
        )
        mock_session_check = MagicMock()
        mock_session_check.scalar_one_or_none.return_value = self.SESSION_ID
        mock_messages_result = MagicMock()
        mock_messages_result.scalars.return_value.all.return_value = []

        mock_db.execute.side_effect = [mock_session_check, mock_messages_result]

        result, cursor = await repo.get_messages(
            org_id=self.ORG_ID,
            session_id=self.SESSION_ID,
            cursor=valid_cursor,
        )

        assert result == []

    # ── next_sequence_number ───────────────────────────────────────────────────

    async def test_next_sequence_number(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """next_sequence_number returns incremented value."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 5
        mock_db.execute.return_value = mock_result

        seq = await repo.next_sequence_number(session_id=self.SESSION_ID)

        assert seq == 5

    async def test_next_sequence_number_first(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """next_sequence_number returns 0 for first message."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_db.execute.return_value = mock_result

        seq = await repo.next_sequence_number(session_id=self.SESSION_ID)

        assert seq == 0

    # ── update_metadata ────────────────────────────────────────────────────────

    async def test_update_metadata(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """update_metadata deep-merges into existing metadata."""
        session = self._mock_session(metadata_={"key1": "val1"})
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = session
        mock_db.execute.return_value = mock_result
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.update_metadata(
            org_id=self.ORG_ID,
            session_id=self.SESSION_ID,
            metadata={"key2": "val2", "key1": None},
        )

        assert result is not None
        assert result.metadata_ == {"key2": "val2"}
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_update_metadata_not_found(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """update_metadata returns None when session not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.update_metadata(
            org_id=self.ORG_ID,
            session_id=self.SESSION_ID,
            metadata={"key": "val"},
        )

        assert result is None

    # ── close ──────────────────────────────────────────────────────────────────

    async def test_close(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """close sets closed_at on the session."""
        session = self._mock_session(closed_at=None)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = session
        mock_db.execute.return_value = mock_result
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.close(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result is not None
        assert result.closed_at is not None
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_close_not_found(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """close returns None when session not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.close(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result is None

    # ── soft_delete ────────────────────────────────────────────────────────────

    async def test_soft_delete(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete sets is_deleted flag."""
        session = self._mock_session(is_deleted=False)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = session
        mock_db.execute.return_value = mock_result
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.soft_delete(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result is not None
        assert result.is_deleted is True
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_soft_delete_not_found(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete returns None when session not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.soft_delete(
            org_id=self.ORG_ID, session_id=self.SESSION_ID
        )

        assert result is None

    async def test_soft_delete_with_project(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """soft_delete filters by project_id when provided."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.soft_delete(
            org_id=self.ORG_ID,
            session_id=self.SESSION_ID,
            project_id=self.PROJECT_ID,
        )

        assert result is None

    # ── get_stats ──────────────────────────────────────────────────────────────

    async def test_get_stats(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_stats returns aggregate counts."""
        mock_row = MagicMock()
        mock_row.message_count = 15
        mock_row.fact_count = 7
        mock_row.last_message_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_row.pending_enrichment_count = 3
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = mock_row
        mock_db.execute.return_value = mock_result

        stats = await repo.get_stats(session_id=self.SESSION_ID)

        assert stats["message_count"] == 15
        assert stats["fact_count"] == 7
        assert stats["pending_enrichment_count"] == 3

    async def test_get_stats_session_not_found(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_stats returns zeros when session does not exist."""
        mock_result = MagicMock()
        mock_result.one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        stats = await repo.get_stats(session_id=self.SESSION_ID)

        assert stats == {
            "message_count": 0,
            "fact_count": 0,
            "last_message_at": None,
            "pending_enrichment_count": 0,
        }

    # ── get_observation_count ──────────────────────────────────────────────────

    async def test_get_observation_count(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_observation_count returns the count."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 42
        mock_db.execute.return_value = mock_result

        count = await repo.get_observation_count(
            org_id=self.ORG_ID, project_id=self.PROJECT_ID
        )

        assert count == 42

    async def test_get_observation_count_zero(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """get_observation_count returns 0 when no observations."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_db.execute.return_value = mock_result

        count = await repo.get_observation_count(
            org_id=self.ORG_ID, project_id=self.PROJECT_ID
        )

        assert count == 0

    # ── batch_get_stats ────────────────────────────────────────────────────────

    async def test_batch_get_stats(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """batch_get_stats returns stats for multiple sessions."""
        row_1 = MagicMock()
        row_1.session_id = self.SESSION_ID
        row_1.message_count = 10
        row_1.fact_count = 3
        row_2 = MagicMock()
        row_2.session_id = UUID("00000000-0000-0000-0000-000000000011")
        row_2.message_count = 5
        row_2.fact_count = 1

        mock_result = MagicMock()
        mock_result.all.return_value = [row_1, row_2]
        mock_db.execute.return_value = mock_result

        stats = await repo.batch_get_stats(
            session_ids=[self.SESSION_ID, UUID("00000000-0000-0000-0000-000000000011")],
            organization_id=self.ORG_ID,
        )

        assert stats[self.SESSION_ID]["message_count"] == 10
        assert stats[self.SESSION_ID]["fact_count"] == 3

    async def test_batch_get_stats_empty_input(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """batch_get_stats returns empty dict for empty input."""
        stats = await repo.batch_get_stats(
            session_ids=[], organization_id=self.ORG_ID
        )

        assert stats == {}

    # ── find_stale_open_sessions ───────────────────────────────────────────────

    async def test_find_stale_open_sessions(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """find_stale_open_sessions returns stale sessions."""
        stale = [self._mock_session(), self._mock_session(id=uuid4())]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = stale
        mock_db.execute.return_value = mock_result

        result = await repo.find_stale_open_sessions(
            inactivity_hours=24, batch_size=100
        )

        assert result == stale

    # ── message_count ──────────────────────────────────────────────────────────

    async def test_message_count(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """message_count returns the episode count."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 8
        mock_db.execute.return_value = mock_result

        count = await repo.message_count(session_id=self.SESSION_ID)

        assert count == 8

    async def test_message_count_zero(
        self, repo: SessionRepository, mock_db: AsyncMock
    ) -> None:
        """message_count returns 0 when no episodes."""
        mock_result = MagicMock()
        mock_result.scalar.return_value = 0
        mock_db.execute.return_value = mock_result

        count = await repo.message_count(session_id=self.SESSION_ID)

        assert count == 0

    # ── Cursor helpers ─────────────────────────────────────────────────────────

    def test_encode_decode_cursor_roundtrip(
        self, repo: SessionRepository
    ) -> None:
        """_encode_cursor and _decode_cursor round-trip correctly."""
        dt = datetime(2024, 6, 15, 12, 30, 0)
        encoded = repo._encode_cursor(dt, self.SESSION_ID)
        decoded_dt, decoded_id = repo._decode_cursor(encoded)

        assert decoded_dt == dt
        assert decoded_id == self.SESSION_ID

    def test_decode_cursor_invalid_raises(
        self, repo: SessionRepository
    ) -> None:
        """_decode_cursor raises ValueError for malformed input."""
        with pytest.raises(ValueError, match="Invalid session cursor"):
            repo._decode_cursor("not-base64!!!")

    def test_encode_decode_message_cursor_roundtrip(
        self, repo: SessionRepository
    ) -> None:
        """_encode_message_cursor and _decode_message_cursor round-trip."""
        encoded = repo._encode_message_cursor(42, self.EPISODE_ID)
        decoded_seq, decoded_id = repo._decode_message_cursor(encoded)

        assert decoded_seq == 42
        assert decoded_id == self.EPISODE_ID

    def test_decode_message_cursor_invalid_raises(
        self, repo: SessionRepository
    ) -> None:
        """_decode_message_cursor raises ValueError for malformed input."""
        with pytest.raises(ValueError, match="Invalid message cursor"):
            repo._decode_message_cursor("bad-data!!!")
