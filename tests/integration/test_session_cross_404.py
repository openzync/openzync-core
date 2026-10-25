"""Cross-session 404 no-leak — wrong session id never exposes facts."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.exceptions import NotFoundError

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_cross_session_fact_history_404(engine, redis_client) -> None:
    from core.config import Settings, get_settings, set_settings

    try:
        get_settings()
    except RuntimeError:
        set_settings(Settings(DATABASE_URL="postgresql+asyncpg://localhost/x", REDIS_URL="redis://localhost/0", SECRET_KEY="i" * 32, WEBHOOK_SIGNING_SECRET="j" * 32))
    from sqlalchemy.ext.asyncio import AsyncSession

    from services.fact_service import FactService

    async with AsyncSession(engine) as db:
        fact_repo = AsyncMock()
        other_project = uuid4()
        fact = AsyncMock()
        fact.project_id = other_project
        fact_repo.get_by_id.return_value = fact
        svc = FactService(
            db=db, redis_client=redis_client, fact_repo=fact_repo,
            session_repo=AsyncMock(),
        )
        with pytest.raises(NotFoundError):
            await svc.get_fact_history(
                uuid4(),
                organization_id=UUID("00000000-0000-0000-0000-000000000001"),
                project_id=UUID("00000000-0000-0000-0000-000000000003"),
            )
        fact_repo.get_fact_history.assert_not_awaited()
