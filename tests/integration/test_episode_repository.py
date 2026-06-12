"""Tests for EpisodeRepository — batch create, soft delete, enrichment status.

Requires testcontainers PostgreSQL.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.episode_repository import EpisodeRepository
from repositories.user_repository import UserRepository
from repositories.session_repository import SessionRepository


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
class TestEpisodeRepository:
    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    async def _seed_user_and_session(
        self, db: AsyncSession
    ) -> tuple[UUID, UUID]:
        user_repo = UserRepository(db)
        session_repo = SessionRepository(db)
        user = await user_repo.create(
            organization_id=self.ORG_ID,
            external_id="episode_test_user",
        )
        session = await session_repo.create(
            organization_id=self.ORG_ID,
            user_id=user.id,
            external_id="episode_test_session",
        )
        return user.id, session.id

    async def test_batch_create(self, engine) -> None:
        """batch_create inserts multiple episodes and returns them."""
        async with AsyncSession(engine) as db:
            user_id, session_id = await self._seed_user_and_session(db)
            repo = EpisodeRepository(db)
            messages = [
                {"role": "user", "content": "Hello", "metadata": {}},
                {"role": "assistant", "content": "Hi there!", "metadata": {}},
            ]
            episodes = await repo.batch_create(
                organization_id=self.ORG_ID,
                session_id=session_id,
                user_id=user_id,
                messages=messages,
            )
            assert len(episodes) == 2
            for ep in episodes:
                assert ep.id is not None
                assert ep.sequence_number >= 0
                assert ep.enrichment_status == 0
                assert ep.is_deleted is False

    async def test_batch_create_empty(self, engine) -> None:
        """batch_create with empty list returns empty list."""
        async with AsyncSession(engine) as db:
            repo = EpisodeRepository(db)
            result = await repo.batch_create(
                organization_id=self.ORG_ID,
                session_id=UUID("00000000-0000-0000-0000-000000000001"),
                user_id=UUID("00000000-0000-0000-0000-000000000001"),
                messages=[],
            )
            assert result == []
