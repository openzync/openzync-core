"""Memory wipe org gate (mocked repos, no I/O)."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError, ValidationError
from services.idempotency_service import IdempotencyResult, IdempotencyStatus
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
    from unittest.mock import MagicMock as _MagicMock

    idem.compute_content_hash = _MagicMock(return_value="abc123")
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


class TestWipeOrgGate:
    """Wipe: cross-org / archived / missing → 404, zero rows touched."""

    @pytest.mark.asyncio
    async def test_missing_project_404_zero_rows(self) -> None:
        """``get_by_id → None`` → ``NotFoundError`` 404, no deletes."""
        service = _memory_service()
        service._project_repo.get_by_id.return_value = None
        service._blob_repo.get_by_project.return_value = []

        with pytest.raises(NotFoundError) as exc_info:
            await service.delete_project_memory(
                org_id=ORG_A,
                project_id=PROJECT_ID,
                confirm=str(PROJECT_ID),
                actor_id=USER_ID,
            )

        assert exc_info.value.status_code == 404
        service._episode_repo.soft_delete_by_project.assert_not_awaited()
        service._fact_repo.soft_delete_by_project.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_mismatch_422_no_lookup(self) -> None:
        """Mismatched ``confirm`` → ``ValidationError`` 422 before any read."""
        service = _memory_service()

        with pytest.raises(ValidationError) as exc_info:
            await service.delete_project_memory(
                org_id=ORG_A,
                project_id=PROJECT_ID,
                confirm=str(uuid4()),
                actor_id=USER_ID,
            )

        assert exc_info.value.status_code == 422
        service._project_repo.get_by_id.assert_not_awaited()
        service._episode_repo.soft_delete_by_project.assert_not_awaited()
