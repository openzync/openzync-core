"""Unit tests for StructuredExtractionService — mocked dependencies."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from services.structured_extraction_service import StructuredExtractionService


@pytest.mark.unit
class TestStructuredExtractionService:
    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    def _make_service(self) -> tuple[StructuredExtractionService, AsyncMock]:
        mock_repo = AsyncMock()
        mock_user_repo = AsyncMock()
        mock_session_repo = AsyncMock()

        mock_user_repo.get_by_uuid.return_value = MagicMock(
            id=uuid4(), organization_id=self.ORG_ID,
        )
        mock_session_repo.get_by_uuid.return_value = MagicMock(
            id=uuid4(), is_deleted=False,
        )

        service = StructuredExtractionService(
            repo=mock_repo,
            user_repo=mock_user_repo,
            session_repo=mock_session_repo,
        )
        return service, mock_repo

    @pytest.mark.asyncio
    async def test_get_session_extractions_returns_empty_for_no_data(
        self,
    ) -> None:
        """Empty session returns no extractions."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_session.return_value = []

        result = await service.get_session_extractions(
            org_id=self.ORG_ID, user_id=uuid4(), session_id=uuid4(),
        )
        assert len(result.items) == 0
        assert result.total == 0
