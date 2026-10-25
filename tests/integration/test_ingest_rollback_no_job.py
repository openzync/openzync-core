"""Ingest commit+queue fail -> rollback, no job (real PG/Redis via testcontainers)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_ingest_queue_fail_rolls_back(engine, redis_client) -> None:
    """ARQ down after DB writes -> exception propagates, rows rolled back."""
    from core.config import Settings, get_settings, set_settings

    try:
        get_settings()
    except RuntimeError:
        set_settings(Settings(
            DATABASE_URL="postgresql+asyncpg://localhost/x",
            REDIS_URL="redis://localhost/0",
            SECRET_KEY="i" * 32, WEBHOOK_SIGNING_SECRET="j" * 32,
        ))
    from sqlalchemy.ext.asyncio import AsyncSession

    from schemas.memory import Message
    from services.memory_service import MemoryService

    org_id = UUID("00000000-0000-0000-0000-000000000001")
    async with AsyncSession(engine) as db:
        svc = MemoryService(db=db, redis_client=redis_client)
        svc._project_repo = AsyncMock()
        svc._project_repo.get_by_id.return_value = MagicMock()
        svc._episode_repo = AsyncMock()
        svc._episode_repo.batch_create.return_value = []
        db.commit = AsyncMock(side_effect=RuntimeError("commit failed"))  # type: ignore[method-assign]
        with (
            patch.object(svc, "_resolve_session", new=AsyncMock(return_value=AsyncMock(id=UUID("00000000-0000-0000-0000-000000000010")))),
            patch.object(svc, "_enqueue_arq_tasks", new=AsyncMock()) as enq,
            patch.object(svc, "_get_org_pii_config", return_value={"mode": "off"}),
            pytest.raises(RuntimeError, match="commit failed"),
        ):
            await svc.ingest(
                org_id=org_id,
                project_id=UUID("00000000-0000-0000-0000-000000000003"),
                created_by=UUID("00000000-0000-0000-0000-000000000002"),
                session_external_id="sess-rollback",
                messages=[Message(role="user", content="hi")],
            )
        enq.assert_not_called()
