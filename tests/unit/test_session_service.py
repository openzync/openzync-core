"""Unit tests for SessionService — business logic with mocked repository."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core.events import EventType
from core.exceptions import ConflictError, NotFoundError, ValidationError
from repositories.session_repository import SessionRepository
from services.session_service import SessionService
from services.webhook_service import WebhookService


@pytest.mark.unit
class TestSessionService:
    """SessionService unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")

    def _make_mock_session(self, **kwargs) -> AsyncMock:
        session = AsyncMock()
        session.id = kwargs.get("id", uuid4())
        session.organization_id = kwargs.get("org_id", self.ORG_ID)
        session.project_id = kwargs.get("project_id", self.PROJECT_ID)
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

    # ── Existing tests (unchanged) ──────────────────────────────────────────

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
            project_id=self.PROJECT_ID,
            created_by=self.USER_ID,
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
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
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
            project_id=self.PROJECT_ID,
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
                project_id=self.PROJECT_ID,
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
            project_id=self.PROJECT_ID,
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

    # ═════════════════════════════════════════════════════════════════════════
    # New tests: create with webhook
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_create_session_with_webhook(self) -> None:
        """Creating a session emits SESSION_CREATED via webhook service."""
        mock_repo = AsyncMock(spec=SessionRepository)
        mock_webhook = AsyncMock(spec=WebhookService)
        mock_webhook.emit = AsyncMock()
        service = SessionService(repo=mock_repo, webhook_service=mock_webhook)

        mock_repo.get_by_external_id.return_value = None
        mock_session = self._make_mock_session(external_id="with-webhook")
        mock_repo.create.return_value = mock_session

        result = await service.create_session(
            organization_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            created_by=self.USER_ID,
            external_id="with-webhook",
        )

        assert result.external_id == "with-webhook"
        mock_webhook.emit.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            event_type=EventType.SESSION_CREATED,
            payload={
                "session_id": str(mock_session.id),
                "project_id": str(self.PROJECT_ID),
                "created_by": str(self.USER_ID),
                "external_id": "with-webhook",
            },
        )

    # ═════════════════════════════════════════════════════════════════════════
    # New tests: get by external_id
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_get_session_by_external_id_found(self) -> None:
        """Getting a session by external_id returns stats-enriched response."""
        service, mock_repo = self._make_service()
        mock_session = self._make_mock_session(external_id="ext-123")
        mock_repo.get_by_external_id.return_value = mock_session
        mock_repo.get_stats.return_value = {
            "message_count": 10,
            "fact_count": 4,
            "pending_enrichment_count": 2,
        }
        mock_repo.get_observation_count.return_value = 42

        result = await service.get_session_by_external_id(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            external_id="ext-123",
        )

        assert result.external_id == "ext-123"
        assert result.message_count == 10
        assert result.fact_count == 4
        assert result.observation_count == 42
        mock_repo.get_stats.assert_awaited_once_with(mock_session.id)

    @pytest.mark.asyncio
    async def test_get_session_by_external_id_not_found(self) -> None:
        """Getting a non-existent external_id raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_external_id.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_session_by_external_id(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                external_id="nonexistent",
            )

    # ═════════════════════════════════════════════════════════════════════════
    # New tests: get by UUID (alias)
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_get_session_by_uuid(self) -> None:
        """get_session_by_uuid is an alias for get_session."""
        service, mock_repo = self._make_service()
        mock_session = self._make_mock_session()
        mock_repo.get_by_uuid.return_value = mock_session
        mock_repo.get_stats.return_value = {
            "message_count": 0,
            "fact_count": 0,
            "pending_enrichment_count": 0,
        }
        mock_repo.get_observation_count.return_value = 0

        result = await service.get_session_by_uuid(
            org_id=self.ORG_ID,
            session_id=mock_session.id,
            project_id=self.PROJECT_ID,
        )

        assert result.id == mock_session.id
        assert result.external_id == mock_session.external_id
        mock_repo.get_by_uuid.assert_awaited_once()

    # ═════════════════════════════════════════════════════════════════════════
    # New tests: list sessions
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_list_sessions_success(self) -> None:
        """Listing sessions returns paginated results with stats."""
        service, mock_repo = self._make_service()
        sessions = [
            self._make_mock_session(external_id="sess-1"),
            self._make_mock_session(external_id="sess-2"),
        ]
        mock_repo.list.return_value = (sessions, "next-cursor-value")
        mock_repo.batch_get_stats.return_value = {
            sessions[0].id: {"message_count": 5, "fact_count": 3},
            sessions[1].id: {"message_count": 2, "fact_count": 1},
        }

        result = await service.list_sessions(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            limit=50,
        )

        assert len(result.data) == 2
        assert result.next_cursor == "next-cursor-value"
        assert result.has_more is True
        assert result.data[0].external_id == "sess-1"
        assert result.data[0].message_count == 5
        assert result.data[0].fact_count == 3
        assert result.data[1].message_count == 2
        assert result.data[1].fact_count == 1
        mock_repo.batch_get_stats.assert_awaited_once_with(
            [s.id for s in sessions], self.ORG_ID
        )

    @pytest.mark.asyncio
    async def test_list_sessions_validation_error(self) -> None:
        """list_sessions raises ValidationError for out-of-range limit."""
        service, mock_repo = self._make_service()

        with pytest.raises(ValidationError, match="limit must be between 1 and 200"):
            await service.list_sessions(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                limit=0,
            )

        with pytest.raises(ValidationError, match="limit must be between 1 and 200"):
            await service.list_sessions(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                limit=201,
            )

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self) -> None:
        """Listing sessions with no results returns empty page."""
        service, mock_repo = self._make_service()
        mock_repo.list.return_value = ([], None)

        result = await service.list_sessions(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
        )

        assert result.data == []
        assert result.next_cursor is None
        assert result.has_more is False

    @pytest.mark.asyncio
    async def test_list_sessions_with_cursor(self) -> None:
        """Listing sessions passes cursor through to repository."""
        service, mock_repo = self._make_service()
        sessions = [self._make_mock_session(external_id="cursor-sess")]
        mock_repo.list.return_value = (sessions, "next-page-cursor")
        mock_repo.batch_get_stats.return_value = {
            sessions[0].id: {"message_count": 0, "fact_count": 0},
        }

        result = await service.list_sessions(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            cursor="test-cursor",
            limit=25,
        )

        mock_repo.list.assert_awaited_once_with(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            limit=25,
            cursor="test-cursor",
            include_closed=False,
        )
        assert len(result.data) == 1

    # ═════════════════════════════════════════════════════════════════════════
    # New tests: get messages
    # ═════════════════════════════════════════════════════════════════════════

    def _make_mock_episode(self, **kwargs) -> AsyncMock:
        """Create a mock episode (message) with the given attributes."""
        ep = AsyncMock()
        ep.id = kwargs.get("id", uuid4())
        ep.role = kwargs.get("role", "user")
        ep.content = kwargs.get("content", "Hello")
        ep.metadata_ = kwargs.get("metadata", {})
        ep.token_count = kwargs.get("token_count", 10)
        ep.sequence_number = kwargs.get("sequence_number", 0)
        ep.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
        return ep

    @pytest.mark.asyncio
    async def test_get_messages_success(self) -> None:
        """Getting messages returns paginated message response."""
        service, mock_repo = self._make_service()
        mock_session = self._make_mock_session()
        mock_repo.get_by_uuid.return_value = mock_session

        episodes = [
            self._make_mock_episode(
                sequence_number=0, role="user", content="First"
            ),
            self._make_mock_episode(
                sequence_number=1, role="assistant", content="Second"
            ),
        ]
        mock_repo.get_messages.return_value = (episodes, "next-msg-cursor")

        result = await service.get_messages(
            org_id=self.ORG_ID,
            session_id=mock_session.id,
            limit=100,
        )

        assert len(result.data) == 2
        assert result.next_cursor == "next-msg-cursor"
        assert result.has_more is True
        assert result.data[0].role == "user"
        assert result.data[0].content == "First"
        assert result.data[1].role == "assistant"
        assert result.data[1].content == "Second"

    @pytest.mark.asyncio
    async def test_get_messages_session_not_found(self) -> None:
        """Getting messages for a non-existent session raises NotFoundError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_uuid.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_messages(
                org_id=self.ORG_ID,
                session_id=uuid4(),
            )

    @pytest.mark.asyncio
    async def test_get_messages_validation_error(self) -> None:
        """get_messages raises ValidationError for out-of-range limit."""
        service, mock_repo = self._make_service()

        with pytest.raises(
            ValidationError, match="limit must be between 1 and 500"
        ):
            await service.get_messages(
                org_id=self.ORG_ID,
                session_id=uuid4(),
                limit=0,
            )

    @pytest.mark.asyncio
    async def test_get_messages_with_blobs(self) -> None:
        """Getting messages includes blob attachments when present."""
        service, mock_repo = self._make_service()
        mock_session = self._make_mock_session()
        mock_repo.get_by_uuid.return_value = mock_session

        episode = self._make_mock_episode(
            sequence_number=0, role="user", content="With blobs"
        )
        mock_repo.get_messages.return_value = ([episode], None)

        # The service accesses self._db inside get_messages for blob loading.
        service._db = AsyncMock()

        blob_id = uuid4()
        mock_blob = AsyncMock(spec=["id", "file_name", "mime_type", "file_size"])
        mock_blob.id = blob_id
        mock_blob.file_name = "photo.png"
        mock_blob.mime_type = "image/png"
        mock_blob.file_size = 1024

        with patch(
            "repositories.episode_blob_repository.EpisodeBlobRepository"
        ) as mock_blob_repo_cls:
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_episode = AsyncMock(return_value=[mock_blob])
            mock_blob_repo_cls.return_value = mock_blob_repo

            result = await service.get_messages(
                org_id=self.ORG_ID,
                session_id=mock_session.id,
            )

        assert len(result.data) == 1
        msg = result.data[0]
        assert msg.role == "user"
        assert len(msg.blobs) == 1
        assert msg.blobs[0].id == blob_id
        assert msg.blobs[0].file_name == "photo.png"
        assert msg.blobs[0].mime_type == "image/png"
        assert msg.blobs[0].file_size == 1024
        mock_blob_repo.get_by_episode.assert_awaited_once_with(episode.id)

    @pytest.mark.asyncio
    async def test_get_messages_blob_load_failure(self) -> None:
        """Blob loading failure does not prevent messages from being returned."""
        service, mock_repo = self._make_service()
        mock_session = self._make_mock_session()
        mock_repo.get_by_uuid.return_value = mock_session

        episode = self._make_mock_episode(
            sequence_number=0, role="user", content="Blob fail"
        )
        mock_repo.get_messages.return_value = ([episode], None)

        service._db = AsyncMock()

        with patch(
            "repositories.episode_blob_repository.EpisodeBlobRepository"
        ) as mock_blob_repo_cls:
            mock_blob_repo = AsyncMock()
            mock_blob_repo.get_by_episode = AsyncMock(
                side_effect=RuntimeError("S3 down")
            )
            mock_blob_repo_cls.return_value = mock_blob_repo

            result = await service.get_messages(
                org_id=self.ORG_ID,
                session_id=mock_session.id,
            )

        # Messages are still returned despite blob loading failure.
        assert len(result.data) == 1
        assert result.data[0].content == "Blob fail"
        assert result.data[0].blobs == []

    # ═════════════════════════════════════════════════════════════════════════
    # New tests: delete with webhook
    # ═════════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_delete_session_with_webhook(self) -> None:
        """Deleting a session emits SESSION_CLOSED via webhook service."""
        mock_repo = AsyncMock(spec=SessionRepository)
        mock_webhook = AsyncMock(spec=WebhookService)
        mock_webhook.emit = AsyncMock()
        service = SessionService(repo=mock_repo, webhook_service=mock_webhook)

        mock_session = self._make_mock_session()
        mock_repo.soft_delete.return_value = mock_session

        session_id = mock_session.id
        await service.delete_session(
            org_id=self.ORG_ID,
            session_id=session_id,
            project_id=self.PROJECT_ID,
        )

        mock_webhook.emit.assert_awaited_once_with(
            organization_id=self.ORG_ID,
            event_type=EventType.SESSION_CLOSED,
            payload={
                "session_id": str(session_id),
                "project_id": str(mock_session.project_id),
            },
        )
