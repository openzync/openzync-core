"""PII redaction fail-closed: a dead redactor rejects the ingest.

When the redactor raises, ingest raises ``PIIUnavailableError`` (→ 503)
and zero episodes are persisted — no unredacted text ever reaches storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import PIIUnavailableError
from schemas.memory import Message
from services.idempotency_service import ContentHashClaim
from services.memory_service import MemoryService

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _mock_idempotency_service() -> AsyncMock:
    """Benign idempotency double — every ingest proceeds unless overridden."""
    from services.idempotency_service import IdempotencyResult, IdempotencyStatus

    idem = AsyncMock()
    idem.check_idempotency_key.return_value = IdempotencyResult(
        status=IdempotencyStatus.NEW
    )
    idem.check_content_hash.return_value = None
    idem.compute_content_hash = MagicMock(return_value="abc123")
    idem.claim_content_hash.return_value = ContentHashClaim(won=True, winner="")
    return idem


def _memory_service() -> MemoryService:
    mock_db = AsyncMock(spec=AsyncSession)
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True
    mock_redis.setex.return_value = True
    mock_redis.scan.return_value = (0, [])
    mock_redis.delete.return_value = 0
    return MemoryService(
        db=mock_db,
        redis_client=mock_redis,
        episode_repo=AsyncMock(),
        session_repo=AsyncMock(),
        user_repo=AsyncMock(),
        fact_repo=AsyncMock(),
        idempotency_service=_mock_idempotency_service(),
        dedup_repo=AsyncMock(),
    )


class TestPiiFailClosed:
    """A redaction failure rejects the ingest — nothing is persisted."""

    @pytest.mark.asyncio
    async def test_dead_redactor_raises_503_with_zero_episodes(self) -> None:
        service = _memory_service()
        service._session_repo.get_by_external_id.return_value = MagicMock(
            id=uuid4(), external_id="session-abc"
        )
        mock_pii = AsyncMock()
        mock_pii.process_message.side_effect = RuntimeError("redactor down")

        with (
            patch.object(service, "_enqueue_arq_tasks"),
            patch.object(service, "_invalidate_context_cache"),
            patch.object(service, "_get_org_pii_config", return_value={"mode": "mask"}),
            patch("services.pii_service.PIIService", return_value=mock_pii),
            pytest.raises(PIIUnavailableError),
        ):
            await service.ingest(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                created_by=USER_ID,
                session_external_id="session-abc",
                messages=[
                    Message(role="user", content="Message 0"),
                    Message(role="assistant", content="Message 1"),
                ],
            )

        assert PIIUnavailableError.status_code == 503
        service._episode_repo.batch_create.assert_not_awaited()
