"""Unit tests for FactService — business logic with mocked dependencies."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from schemas.facts import FactTriple
from services.fact_service import FactService


@pytest.mark.unit
class TestFactService:
    """FactService unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")
    SESSION_ID = UUID("00000000-0000-0000-0000-000000000010")
    FACT_1_ID = UUID("00000000-0000-0000-0000-000000000100")
    FACT_2_ID = UUID("00000000-0000-0000-0000-000000000101")

    @pytest.fixture
    def service(self) -> FactService:
        mock_db = AsyncMock(spec=AsyncSession)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # no cached idempotency
        mock_fact_repo = AsyncMock()
        mock_fact_repo.create.return_value = MagicMock(id=UUID("00000000-0000-0000-0000-000000000099"))
        mock_session_repo = AsyncMock()

        s = FactService(
            db=mock_db,
            redis_client=mock_redis,
            fact_repo=mock_fact_repo,
            session_repo=mock_session_repo,
        )
        return s

    def _sample_triple(self, **kwargs) -> FactTriple:
        return FactTriple(
            subject=kwargs.get("subject", "Python"),
            predicate=kwargs.get("predicate", "is"),
            object=kwargs.get("object", "great"),
            content=kwargs.get("content", "Python is great"),
            confidence=kwargs.get("confidence", 0.95),
        )

    @pytest.mark.asyncio
    async def test_ingest_facts_empty_list_returns_accepted(self, service: FactService) -> None:
        """Ingesting an empty fact list returns accepted (schema-level validation
        catches empty lists before reaching the service)."""
        result = await service.ingest_facts(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            created_by=self.USER_ID,
            facts=[],
        )
        assert result.status == "accepted"

    @pytest.mark.asyncio
    async def test_ingest_facts_happy_path(self, service: FactService) -> None:
        """Verify successful fact ingestion with 2 sample triples and a session."""
        # Arrange
        mock_session = MagicMock(id=self.SESSION_ID)
        service._session_repo.get_by_external_id.return_value = mock_session

        mock_fact_1 = MagicMock(id=self.FACT_1_ID)
        mock_fact_2 = MagicMock(id=self.FACT_2_ID)
        service._fact_repo.batch_create.return_value = [mock_fact_1, mock_fact_2]

        mock_arq_pool = AsyncMock()
        facts = [
            self._sample_triple(subject="Alice", predicate="likes", object="hiking"),
            self._sample_triple(
                subject="Bob", predicate="works_at", object="AcmeCorp"
            ),
        ]

        with patch("services.fact_service.get_arq", return_value=mock_arq_pool):
            result = await service.ingest_facts(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                facts=facts,
                session_external_id="session-abc",
            )

        # Assert
        assert result.status == "accepted"
        assert isinstance(result.job_id, str)
        assert len(result.job_id) > 0
        assert result.accepted_count == 2
        assert "accepted" in result.message.lower()

        service._fact_repo.batch_create.assert_awaited_once()
        service._session_repo.get_by_external_id.assert_awaited_once_with(
            org_id=self.ORG_ID,
            project_id=self.PROJECT_ID,
            external_id="session-abc",
        )

    @pytest.mark.asyncio
    async def test_ingest_facts_session_not_found(self, service: FactService) -> None:
        """Verify NotFoundError is raised when session_external_id doesn't match."""
        service._session_repo.get_by_external_id.return_value = None

        facts = [self._sample_triple()]

        with pytest.raises(NotFoundError) as exc_info:
            await service.ingest_facts(
                org_id=self.ORG_ID,
                project_id=self.PROJECT_ID,
                created_by=self.USER_ID,
                facts=facts,
                session_external_id="nonexistent-session",
            )

        assert "Session" in exc_info.value.message
        assert "not found" in exc_info.value.message
        service._session_repo.get_by_external_id.assert_awaited_once()
        service._fact_repo.batch_create.assert_not_awaited()
