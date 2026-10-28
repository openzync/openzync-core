"""Ingest post-commit cache-purge failure stays accepted (mocked, no I/O)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.memory import Message
from services.idempotency_service import (
    ContentHashClaim,
    IdempotencyResult,
    IdempotencyStatus,
)
from services.memory_service import MemoryService

pytestmark = pytest.mark.unit

ORG_A = UUID("00000000-0000-0000-0000-0000000000a1")
USER_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


def _memory_service(**overrides: object) -> MemoryService:
    """MemoryService with benign mocked repos (mirrors test_memory_service)."""
    mock_db = AsyncMock(spec=AsyncSession)
    mock_redis = AsyncMock()
    mock_redis.scan.return_value = (0, [])
    idem = AsyncMock()
    idem.check_idempotency_key.return_value = IdempotencyResult(
        status=IdempotencyStatus.NEW
    )
    idem.check_content_hash.return_value = None
    idem.compute_content_hash = MagicMock(return_value="abc123")
    kwargs: dict[str, object] = {
        "db": mock_db,
        "redis_client": mock_redis,
        "episode_repo": AsyncMock(),
        "session_repo": AsyncMock(),
        "user_repo": AsyncMock(),
        "fact_repo": AsyncMock(),
        "idempotency_service": idem,
        "dedup_repo": AsyncMock(),
        "project_repo": AsyncMock(),
        "blob_repo": AsyncMock(),
    }
    kwargs.update(overrides)
    return MemoryService(**kwargs)  # type: ignore[arg-type]


def _sample_messages(count: int = 2) -> list[Message]:
    return [
        Message(role="user" if i % 2 == 0 else "assistant", content=f"Message {i}")
        for i in range(count)
    ]


def _happy_ingest_mocks(service: MemoryService) -> None:
    """Prime a MemoryService mock set for the ingest happy path."""
    service._session_repo.get_by_external_id.return_value = MagicMock(
        id=uuid4(), external_id="session-abc"
    )
    service._idem.claim_content_hash.return_value = ContentHashClaim(
        won=True, winner="pending"
    )
    service._dedup_repo.insert_or_none.return_value = True
    service._episode_repo.batch_create.return_value = []


class TestPostCommitPurgeFailure:
    """Post-commit purge failure → still accepted, webhooks still emitted."""

    @pytest.mark.asyncio
    async def test_purge_failure_still_accepted_and_emits(self) -> None:
        """Redis purge raising after commit → accepted response + 2 emits."""
        webhook_service = AsyncMock()
        service = _memory_service(webhook_service=webhook_service)
        _happy_ingest_mocks(service)

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(
                service,
                "_invalidate_context_cache",
                side_effect=RuntimeError("redis down"),
            ),
            patch.object(service, "_get_org_pii_config", return_value={}),
        ):
            result = await service.ingest(
                org_id=ORG_A,
                project_id=PROJECT_ID,
                created_by=USER_ID,
                session_external_id="session-abc",
                messages=_sample_messages(),
            )

        assert result.status == "accepted"
        assert webhook_service.emit.await_count == 2
