"""Unit tests for StructuredExtractionService — querying extraction results.

All external dependencies (structured extraction repo, session repo) are mocked
at the service boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from core.exceptions import NotFoundError
from models.structured_extraction import StructuredExtraction
from schemas.structured_extractions import (
    StructuredExtractionListResponse,
    StructuredExtractionResponse,
)
from services.structured_extraction_service import StructuredExtractionService


@pytest.mark.unit
class TestStructuredExtractionService:
    """Unit tests for ``StructuredExtractionService`` — extraction queries."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000010")
    EPISODE_ID = UUID("00000000-0000-0000-0000-000000000020")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000030")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[StructuredExtractionService, AsyncMock, AsyncMock]:
        """Create StructuredExtractionService with mocked repositories."""
        mock_repo = AsyncMock()
        mock_repo.get_by_session = AsyncMock()
        mock_repo.get_by_episode = AsyncMock()
        mock_session_repo = AsyncMock()
        mock_session_repo.get_by_uuid = AsyncMock()
        service = StructuredExtractionService(repo=mock_repo, session_repo=mock_session_repo)
        return service, mock_repo, mock_session_repo

    def _make_extraction(
        self,
        extraction_id: UUID | None = None,
        session_id: UUID | None = None,
        episode_id: UUID | None = None,
        schema_id: UUID | None = None,
        data: dict | None = None,
    ) -> MagicMock:
        """Build a MagicMock mimicking a StructuredExtraction ORM model."""
        ext = MagicMock(spec=StructuredExtraction)
        ext.id = extraction_id or UUID("00000000-0000-0000-0000-000000000100")
        ext.session_id = session_id or self.SESSION_ID
        ext.episode_id = episode_id or self.EPISODE_ID
        ext.schema_id = schema_id
        ext.data = data or {"amount": 100.0, "currency": "USD"}
        ext.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        return ext

    def _make_session_mock(self) -> MagicMock:
        """Build a MagicMock mimicking a Session ORM model (for ownership check)."""
        session = MagicMock()
        session.id = self.SESSION_ID
        session.organization_id = self.ORG_ID
        return session

    # ── get_session_extractions ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_session_extractions_success(self) -> None:
        """``get_session_extractions`` returns a list response with extractions."""
        service, mock_repo, mock_session_repo = self._make_service()
        mock_session_repo.get_by_uuid.return_value = self._make_session_mock()
        mock_repo.get_by_session.return_value = [
            self._make_extraction(episode_id=UUID("00000000-0000-0000-0000-000000000201")),
            self._make_extraction(episode_id=UUID("00000000-0000-0000-0000-000000000202")),
        ]

        result = await service.get_session_extractions(self.ORG_ID, self.SESSION_ID)

        assert isinstance(result, StructuredExtractionListResponse)
        assert result.total == 2
        assert len(result.items) == 2
        assert isinstance(result.items[0], StructuredExtractionResponse)
        mock_session_repo.get_by_uuid.assert_awaited_once_with(
            org_id=self.ORG_ID, session_id=self.SESSION_ID, project_id=None,
        )
        mock_repo.get_by_session.assert_awaited_once_with(self.ORG_ID, self.SESSION_ID)

    @pytest.mark.asyncio
    async def test_get_session_extractions_session_not_found_raises(self) -> None:
        """``get_session_extractions`` raises NotFoundError when session missing."""
        service, mock_repo, mock_session_repo = self._make_service()
        mock_session_repo.get_by_uuid.return_value = None

        with pytest.raises(NotFoundError, match="not found"):
            await service.get_session_extractions(self.ORG_ID, self.SESSION_ID)

        mock_repo.get_by_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_session_extractions_with_project_id(self) -> None:
        """``get_session_extractions`` passes project_id to session lookup."""
        service, mock_repo, mock_session_repo = self._make_service()
        mock_session_repo.get_by_uuid.return_value = self._make_session_mock()
        mock_repo.get_by_session.return_value = []

        result = await service.get_session_extractions(
            self.ORG_ID, self.SESSION_ID, project_id=self.PROJECT_ID,
        )

        assert result.total == 0
        assert result.items == []
        mock_session_repo.get_by_uuid.assert_awaited_once_with(
            org_id=self.ORG_ID, session_id=self.SESSION_ID, project_id=self.PROJECT_ID,
        )

    @pytest.mark.asyncio
    async def test_get_session_extractions_empty_extractions(self) -> None:
        """``get_session_extractions`` returns empty list when no extractions."""
        service, mock_repo, mock_session_repo = self._make_service()
        mock_session_repo.get_by_uuid.return_value = self._make_session_mock()
        mock_repo.get_by_session.return_value = []

        result = await service.get_session_extractions(self.ORG_ID, self.SESSION_ID)

        assert result.total == 0
        assert result.items == []

    # ── get_episode_extraction ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_episode_extraction_returns_response(self) -> None:
        """``get_episode_extraction`` returns response for existing extraction."""
        service, mock_repo, mock_session_repo = self._make_service()
        mock_session_repo.get_by_uuid.return_value = self._make_session_mock()
        mock_repo.get_by_episode.return_value = self._make_extraction(
            data={"amount": 250.0},
        )

        result = await service.get_episode_extraction(
            self.ORG_ID, self.SESSION_ID, self.EPISODE_ID,
        )

        assert isinstance(result, StructuredExtractionResponse)
        assert result.data == {"amount": 250.0}
        mock_session_repo.get_by_uuid.assert_awaited_once_with(
            org_id=self.ORG_ID, session_id=self.SESSION_ID, project_id=None,
        )
        mock_repo.get_by_episode.assert_awaited_once_with(self.ORG_ID, self.EPISODE_ID)

    @pytest.mark.asyncio
    async def test_get_episode_extraction_returns_none_when_missing(self) -> None:
        """``get_episode_extraction`` returns None when no extraction exists."""
        service, mock_repo, mock_session_repo = self._make_service()
        mock_session_repo.get_by_uuid.return_value = self._make_session_mock()
        mock_repo.get_by_episode.return_value = None

        result = await service.get_episode_extraction(
            self.ORG_ID, self.SESSION_ID, self.EPISODE_ID,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_episode_extraction_session_not_found_raises(self) -> None:
        """``get_episode_extraction`` raises NotFoundError when session missing."""
        service, mock_repo, mock_session_repo = self._make_service()
        mock_session_repo.get_by_uuid.return_value = None

        with pytest.raises(NotFoundError, match="not found"):
            await service.get_episode_extraction(
                self.ORG_ID, self.SESSION_ID, self.EPISODE_ID,
            )

        mock_repo.get_by_episode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_episode_extraction_with_project_id(self) -> None:
        """``get_episode_extraction`` passes project_id to session lookup."""
        service, mock_repo, mock_session_repo = self._make_service()
        mock_session_repo.get_by_uuid.return_value = self._make_session_mock()
        mock_repo.get_by_episode.return_value = None

        result = await service.get_episode_extraction(
            self.ORG_ID, self.SESSION_ID, self.EPISODE_ID, project_id=self.PROJECT_ID,
        )

        assert result is None
        mock_session_repo.get_by_uuid.assert_awaited_once_with(
            org_id=self.ORG_ID, session_id=self.SESSION_ID, project_id=self.PROJECT_ID,
        )
