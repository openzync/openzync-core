"""Unit tests for FactService — business logic with mocked dependencies."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.facts import FactTriple
from services.fact_service import FactService


@pytest.mark.unit
class TestFactService:
    """FactService unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")

    @pytest.fixture
    def service(self) -> FactService:
        mock_db = AsyncMock(spec=AsyncSession)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # no cached idempotency
        mock_fact_repo = AsyncMock()
        mock_fact_repo.create.return_value = MagicMock(id=UUID("00000000-0000-0000-0000-000000000099"))
        mock_user_repo = AsyncMock()
        mock_user_repo.get_by_uuid.return_value = MagicMock(
            id=UUID("00000000-0000-0000-0000-000000000002"),
            metadata_={},
        )
        mock_session_repo = AsyncMock()

        s = FactService(
            db=mock_db,
            redis_client=mock_redis,
            fact_repo=mock_fact_repo,
            user_repo=mock_user_repo,
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
            user_uuid=self.USER_ID,
            facts=[],
        )
        assert result.status == "accepted"
