"""Unit tests for MemoryService — ingestion logic with mocked dependencies."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from schemas.memory import Message
from services.memory_service import MemoryService


@pytest.mark.unit
class TestMemoryService:
    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
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
        """Ingest accepts a ``created_by`` UUID directly (no user look-up)."""
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )

        with patch.object(service, "_enqueue_arq_tasks"):
            with patch.object(service, "_invalidate_context_cache"):
                with patch.object(service, "_get_org_pii_config", return_value={}):
                    result = await service.ingest(
                        org_id=self.ORG_ID,
                        project_id=self.PROJECT_ID,
                        created_by=self.USER_ID,
                        session_external_id=None,
                        messages=self._sample_messages(),
                    )
        assert result.status == "accepted"

    @pytest.mark.asyncio
    async def test_ingest_without_user_lookup_succeeds(
        self, service: MemoryService,
    ) -> None:
        """Ingest does not look up the user when ``created_by`` is a UUID.

        ``MemoryService.ingest`` passes ``created_by`` directly to the
        session resolver — it no longer calls ``user_repo.get_by_uuid``.
        """
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )

        with patch.object(service, "_enqueue_arq_tasks"):
            with patch.object(service, "_invalidate_context_cache"):
                with patch.object(service, "_get_org_pii_config", return_value={}):
                    result = await service.ingest(
                        org_id=self.ORG_ID,
                        project_id=self.PROJECT_ID,
                        created_by=self.USER_ID,
                        session_external_id="test",
                        messages=self._sample_messages(),
                    )
        assert result.status == "accepted"
        service._user_repo.get_by_uuid.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_user_memory(self, service: MemoryService) -> None:
        """Delete memory soft-deletes episodes and facts."""
        service._episode_repo.soft_delete_by_project.return_value = 5
        service._fact_repo.soft_delete_by_project.return_value = 3

        with patch.object(service, "_invalidate_context_cache"):
            episodes, facts = await service.delete_project_memory(
                org_id=self.ORG_ID, project_id=self.PROJECT_ID,
            )
        assert episodes == 5
        assert facts == 3

    @pytest.mark.asyncio
    async def test_compute_content_hash_is_deterministic(
        self, service: MemoryService,
    ) -> None:
        """Same inputs produce the same hash."""
        h1 = service._compute_content_hash(
            str(self.PROJECT_ID), "session_1", self._sample_messages(),
        )
        h2 = service._compute_content_hash(
            str(self.PROJECT_ID), "session_1", self._sample_messages(),
        )
        assert h1 == h2

    @pytest.mark.asyncio
    async def test_commit_before_enqueue(self, service: MemoryService) -> None:
        """ingest() calls db.commit() before _enqueue_arq_tasks().

        This ordering is critical for the transaction-visibility fix:
        episodes must be visible to PostgreSQL *before* ARQ enrichment
        tasks are enqueued to Redis, otherwise workers may race ahead
        and fail with EpisodeNotFoundError.
        """
        service._session_repo.get_or_create_default.return_value = MagicMock(
            id=uuid4(), external_id="__default__",
        )

        # Make batch_create return a real-looking episode list
        mock_episode = MagicMock()
        mock_episode.id = uuid4()
        mock_episode.content = "test content"
        mock_episode.role = "user"
        mock_episode.metadata_ = {}
        service._episode_repo.batch_create.return_value = [mock_episode]

        # Track call order across two different mocks (db vs service method).
        # NOTE: We use a list as a side_effect and *do not* call the original
        # mock — doing so would re-trigger the side_effect (infinite recursion).
        call_order: list[str] = []

        async def _tracked_commit() -> None:  # type: ignore[misc]
            call_order.append("commit")

        service._db.commit.side_effect = _tracked_commit

        with patch.object(service, "_enqueue_arq_tasks") as mock_enqueue:
            def _tracked_enqueue(*args: object, **kwargs: object) -> None:
                call_order.append("enqueue")
            mock_enqueue.side_effect = _tracked_enqueue

            with patch.object(service, "_invalidate_context_cache"):
                with patch.object(service, "_get_org_pii_config", return_value={}):
                    with patch.object(service, "_cache_content_hash"):
                        with patch.object(service, "_cache_idempotency"):
                            result = await service.ingest(
                                org_id=self.ORG_ID,
                                project_id=self.PROJECT_ID,
                                created_by=self.USER_ID,
                                session_external_id=None,
                                messages=self._sample_messages(),
                            )

        assert result.status == "accepted"
        assert call_order == ["commit", "enqueue"], (
            f"Expected commit before enqueue, got {call_order}"
        )
        service._db.commit.assert_awaited_once()
        mock_enqueue.assert_awaited_once()
