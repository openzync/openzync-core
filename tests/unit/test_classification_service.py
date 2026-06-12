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

    def _make_service(self) -> tuple[ClassificationService, AsyncMock]:
        mock_repo = AsyncMock(spec=DialogClassificationRepository)
        mock_user_repo = AsyncMock(spec=UserRepository)
        mock_session_repo = AsyncMock(spec=SessionRepository)
        mock_episode_repo = AsyncMock(spec=EpisodeRepository)

        # Stub user + session lookup so they pass
        mock_user_repo.get_by_uuid.return_value = MagicMock(
            id=uuid4(), organization_id=self.ORG_ID,
        )
        mock_session_repo.get_by_uuid.return_value = MagicMock(
            id=uuid4(), is_deleted=False,
        )

        service = ClassificationService(
            repo=mock_repo,
            user_repo=mock_user_repo,
            session_repo=mock_session_repo,
            episode_repo=mock_episode_repo,
        )
        return service, mock_repo

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
        service, mock_repo = self._make_service()
        mock_repo.get_by_session.return_value = []

        result = await service.get_classifications_for_session(
            org_id=self.ORG_ID,
            user_id=uuid4(),
            session_id=uuid4(),
        )
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_get_classifications_for_session_with_results(self) -> None:
        """Classifications include intent and emotion from DB."""
        service, mock_repo = self._make_service()
        mock_cls = self._mock_classification(intent="greeting", emotion="positive")
        mock_repo.get_by_session.return_value = [mock_cls]

        result = await service.get_classifications_for_session(
            org_id=self.ORG_ID,
            user_id=uuid4(),
            session_id=uuid4(),
        )
        assert len(result) == 1
        assert result[0].intent == "greeting"
        assert result[0].emotion == "positive"
