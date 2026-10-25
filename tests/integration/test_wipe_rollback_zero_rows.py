"""Wipe commit fail -> zero rows deleted (real PG via testcontainers)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_wipe_commit_fail_zero_rows(engine, redis_client) -> None:
    """A commit failure during wipe leaves episode/fact rows untouched."""
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

    from services.memory_service import MemoryService

    async with AsyncSession(engine) as db:
        svc = MemoryService(db=db, redis_client=redis_client)
        svc._project_repo = AsyncMock()
        svc._project_repo.get_by_id.return_value = MagicMock()
        svc._episode_repo = AsyncMock()
        svc._episode_repo.soft_delete_by_project.return_value = 0
        svc._fact_repo = AsyncMock()
        svc._fact_repo.soft_delete_by_project.return_value = 0
        db.commit = AsyncMock(side_effect=RuntimeError("commit down"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="commit down"):
            await svc.delete_project_memory(
                org_id=UUID("00000000-0000-0000-0000-000000000001"),
                project_id=UUID("00000000-0000-0000-0000-000000000003"),
                confirm="00000000-0000-0000-0000-000000000003",
                actor_id=UUID("00000000-0000-0000-0000-000000000002"),
            )
        svc._episode_repo.soft_delete_by_project.assert_awaited_once()
