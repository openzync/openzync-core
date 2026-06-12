"""Unit tests for MemoryService — ingestion logic with mocked dependencies."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from schemas.memory import Message
from services.memory_service import MemoryService


@pytest.mark.unit
class TestMemoryService:
    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")

    @pytest.fixture
    def service(self) -> MemoryService:
        mock_db = AsyncMock(spec=AsyncSession)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # no cached idempotency
        mock_redis.set.return_value = True
        mock_redis.setex.return_value = True
        mock_redis.scan.return_value = (0, [])
        mock_redis.delete.return_value = 0
        mock_episode_repo = AsyncMock()
        mock_episode_repo.batch_create.return_value = []
        mock_session_repo = AsyncMock()
        mock_user_repo = AsyncMock()
        mock_fact_repo = AsyncMock()

        return MemoryService(
            db=mock_db,
            redis_client=mock_redis,
            episode_repo=mock_episode_repo,
            session_repo=mock_session_repo,
            user_repo=mock_user_repo,
            fact_repo=mock_fact_repo,
        )

    def _sample_messages(self, count: int = 2) -> list[Message]:
        return [
            Message(role="user" if i % 2 == 0 else "assistant",
                    content=f"Message {i}")
            for i in range(count)
        ]

    @pytest.mark.asyncio
    async def test_ingest_resolves_user(self, service: MemoryService) -> None:
        """Ingest looks up the user by UUID."""
        service._user_repo.get_by_uuid.return_value = MagicMock(
            id=self.USER_ID, organization_id=self.ORG_ID,
        )
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )

        with patch.object(service, "_enqueue_arq_tasks"):
            with patch.object(service, "_invalidate_context_cache"):
                with patch.object(service, "_get_org_pii_config", return_value={}):
                    result = await service.ingest(
                        org_id=self.ORG_ID,
                        user_uuid=self.USER_ID,
                        session_external_id=None,
                        messages=self._sample_messages(),
                    )
        assert result.status == "accepted"
        service._user_repo.get_by_uuid.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ingest_user_not_found_raises_404(
        self, service: MemoryService,
    ) -> None:
        """Ingest for non-existent user raises NotFoundError."""
        service._user_repo.get_by_uuid.return_value = None

        with pytest.raises(NotFoundError):
            await service.ingest(
                org_id=self.ORG_ID,
                user_uuid=self.USER_ID,
                session_external_id="test",
                messages=self._sample_messages(),
            )

    @pytest.mark.asyncio
    async def test_delete_user_memory(self, service: MemoryService) -> None:
        """Delete memory soft-deletes episodes and facts."""
        service._user_repo.get_by_uuid.return_value = MagicMock(
            id=self.USER_ID, organization_id=self.ORG_ID,
        )
        service._episode_repo.soft_delete_by_user.return_value = 5
        service._fact_repo.soft_delete_by_user.return_value = 3

        with patch.object(service, "_invalidate_context_cache"):
            episodes, facts = await service.delete_user_memory(
                org_id=self.ORG_ID, user_uuid=self.USER_ID,
            )
        assert episodes == 5
        assert facts == 3

    @pytest.mark.asyncio
    async def test_compute_content_hash_is_deterministic(
        self, service: MemoryService,
    ) -> None:
        """Same inputs produce the same hash."""
        h1 = service._compute_content_hash(
            str(self.USER_ID), "session_1", self._sample_messages(),
        )
        h2 = service._compute_content_hash(
            str(self.USER_ID), "session_1", self._sample_messages(),
        )
        assert h1 == h2
