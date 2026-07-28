"""Unit tests for ClassificationService — mocked repositories."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from repositories.dialog_classification_repository import (
    DialogClassificationRepository,
)
from repositories.episode_repository import EpisodeRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository
from services.classification_service import ClassificationService


@pytest.mark.unit
class TestClassificationService:
    """ClassificationService unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    def _make_service(
        self,
    ) -> tuple[
        ClassificationService,
        AsyncMock,
        AsyncMock,
        AsyncMock,
    ]:
        mock_repo = AsyncMock(spec=DialogClassificationRepository)
        mock_session_repo = AsyncMock(spec=SessionRepository)
        mock_episode_repo = AsyncMock(spec=EpisodeRepository)

        # Stub episode content batch fetch (returns empty by default)
        mock_episode_repo.get_content_batch.return_value = {}

        # Stub session lookup so they pass
        mock_session_repo.get_by_uuid.return_value = MagicMock(
            id=uuid4(), is_deleted=False,
        )

        service = ClassificationService(
            repo=mock_repo,
            session_repo=mock_session_repo,
            episode_repo=mock_episode_repo,
        )
        return service, mock_repo, mock_session_repo, mock_episode_repo

    def _mock_classification(self, **kwargs) -> MagicMock:
        """Create a mock dialog classification ORM object."""
        m = MagicMock()
        m.id = kwargs.get("id", uuid4())
        m.intent = kwargs.get("intent", "greeting")
        m.emotion = kwargs.get("emotion", "positive")
        m.valence = kwargs.get("valence", "positive")
        m.arousal = kwargs.get("arousal", "medium")
        m.confidence = kwargs.get("confidence", 0.95)
        m.raw = kwargs.get("raw", {})
        m.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
        m.updated_at = kwargs.get("updated_at", datetime.now(timezone.utc))
        m.episode_id = kwargs.get("episode_id", uuid4())
        return m

    @pytest.mark.asyncio
    async def test_get_classifications_for_session_returns_list(self) -> None:
        """Getting classifications returns a list."""
        service, mock_repo, _mock_session_repo, _mock_episode_repo = (
            self._make_service()
        )
        mock_repo.get_by_session.return_value = []

        result = await service.get_classifications_for_session(
            org_id=self.ORG_ID,
            session_id=uuid4(),
        )
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_get_classifications_for_session_with_results(self) -> None:
        """Classifications include intent and emotion from DB."""
        service, mock_repo, _mock_session_repo, mock_episode_repo = (
            self._make_service()
        )
        mock_cls = self._mock_classification(intent="greeting", emotion="positive")
        mock_repo.get_by_session.return_value = [mock_cls]

        result = await service.get_classifications_for_session(
            org_id=self.ORG_ID,
            session_id=uuid4(),
        )
        assert len(result) == 1
        assert result[0].intent == "greeting"
        assert result[0].emotion == "positive"

        # Verify episode content was fetched and injected
        mock_episode_repo.get_content_batch.assert_awaited_once()
        assert result[0].message == ""
        assert result[0].role == ""

    @pytest.mark.asyncio
    async def test_get_classifications_for_session_with_message_content(
        self,
    ) -> None:
        """Episode content and role are injected into classification responses."""
        service, mock_repo, _mock_session_repo, mock_episode_repo = (
            self._make_service()
        )
        episode_id = uuid4()

        mock_cls = self._mock_classification(
            intent="question",
            emotion="frustration",
            episode_id=episode_id,
        )
        mock_cls.valence = "negative"
        mock_cls.arousal = "high"
        mock_repo.get_by_session.return_value = [mock_cls]

        # Provide real episode content
        mock_episode_repo.get_content_batch.return_value = {
            episode_id: ("Hello, I need help!", "user"),
        }

        result = await service.get_classifications_for_session(
            org_id=self.ORG_ID,
            session_id=uuid4(),
        )

        assert len(result) == 1
        assert result[0].message == "Hello, I need help!"
        assert result[0].role == "user"
        mock_episode_repo.get_content_batch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_classification_for_episode_with_message(
        self,
    ) -> None:
        """Single episode classification includes message and role."""
        service, mock_repo, _mock_session_repo, mock_episode_repo = (
            self._make_service()
        )
        episode_id = uuid4()

        mock_cls = self._mock_classification(
            intent="command",
            emotion="neutral",
            episode_id=episode_id,
        )
        mock_cls.valence = "neutral"
        mock_cls.arousal = "low"
        mock_cls.confidence = 0.88

        mock_repo.get_by_episode.return_value = mock_cls
        mock_episode_repo.get_content_batch.return_value = {
            episode_id: ("Turn off the lights please.", "user"),
        }

        result = await service.get_classification_for_episode(
            org_id=self.ORG_ID,
            episode_id=episode_id,
        )

        assert result is not None
        assert result.message == "Turn off the lights please."
        assert result.role == "user"
