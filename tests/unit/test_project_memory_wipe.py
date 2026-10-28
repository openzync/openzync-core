"""Project memory wipe: a storage-config failure raises loudly.

The blob sweep must never be silently skipped when the storage config is
unreadable. The wipe raises (→ 502) after the already-committed soft-deletes,
leaving the rows GC-visible instead of pretending the sweep happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ExternalServiceError
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


class TestWipeStorageFailureLoud:
    """The blob sweep must never be silently skipped on config failure."""

    @pytest.mark.asyncio
    async def test_storage_config_failure_raises_after_soft_delete(self) -> None:
        service = _memory_service()
        service._project_repo = AsyncMock()
        service._project_repo.get_by_id.return_value = MagicMock()
        service._episode_repo.soft_delete_by_project.return_value = 5
        service._fact_repo.soft_delete_by_project.return_value = 3
        service._blob_repo = AsyncMock()
        service._resolve_storage_config = AsyncMock(
            side_effect=ExternalServiceError("Blob storage config unreadable")
        )

        with (
            patch.object(service, "_invalidate_context_cache"),
            pytest.raises(ExternalServiceError, match="unreadable"),
        ):
            await service.delete_project_memory(
                org_id=ORG_ID,
                project_id=PROJECT_ID,
                confirm=str(PROJECT_ID),
                actor_id=USER_ID,
            )

        assert ExternalServiceError.status_code == 502
        # Rows were already committed before the sweep — preserved, GC-visible.
        service._episode_repo.soft_delete_by_project.assert_awaited_once()
        service._fact_repo.soft_delete_by_project.assert_awaited_once()
        service._db.commit.assert_awaited()
        # The sweep never started — no page walk on an unknown config.
        service._blob_repo.get_by_project_after.assert_not_awaited()
