"""Integration tests for SessionRepository — CRUD with real PostgreSQL.

Requires testcontainers PostgreSQL.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.project_repository import ProjectRepository
from repositories.session_repository import SessionRepository
from repositories.user_repository import UserRepository


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
class TestSessionRepository:
    """SessionRepository CRUD tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    async def _seed_user(self, repo: UserRepository) -> UUID:
        user = await repo.create(
            organization_id=self.ORG_ID,
            external_id="session_test_user",
            name="Session Tester",
        )
        return user.id

    async def _seed_project(
        self, user_repo: UserRepository, project_repo: ProjectRepository
    ) -> tuple[UUID, UUID]:
        """Create a user and a project, return (user_id, project_id)."""
        user_id = await self._seed_user(user_repo)
        project = await project_repo.create(
            organization_id=self.ORG_ID,
            name="Session Test Project",
            created_by=user_id,
        )
        await project_repo.add_member(
            project_id=project.id, user_id=user_id, role="owner"
        )
        return user_id, project.id

    async def test_create_session(self, engine) -> None:
        """Creating a session returns the session with generated fields."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            session = await session_repo.create(
                organization_id=self.ORG_ID,
                project_id=project_id,
                created_by=user_id,
                external_id="test_session",
            )
            assert session.id is not None
            assert session.external_id == "test_session"
            assert session.is_active is True
            assert session.is_deleted is False

    async def test_get_or_create_default_creates(self, engine) -> None:
        """get_or_create_default creates __default__ if not exists."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            session = await session_repo.get_or_create_default(
                self.ORG_ID, project_id, created_by=user_id
            )
            assert session.external_id == "__default__"
            assert session.id is not None

    async def test_get_or_create_default_idempotent(self, engine) -> None:
        """get_or_create_default returns same session on second call."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            s1 = await session_repo.get_or_create_default(
                self.ORG_ID, project_id, created_by=user_id
            )
            s2 = await session_repo.get_or_create_default(
                self.ORG_ID, project_id, created_by=user_id
            )
            assert s1.id == s2.id

    async def test_get_by_external_id_found(self, engine) -> None:
        """get_by_external_id returns the matching session."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            created = await session_repo.create(
                organization_id=self.ORG_ID,
                project_id=project_id,
                created_by=user_id,
                external_id="find_me",
            )
            found = await session_repo.get_by_external_id(
                self.ORG_ID, project_id, "find_me"
            )
            assert found is not None
            assert found.id == created.id

    async def test_get_by_external_id_not_found(self, engine) -> None:
        """get_by_external_id returns None for non-existent session."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            found = await session_repo.get_by_external_id(
                self.ORG_ID, project_id, "nonexistent"
            )
            assert found is None

    async def test_list_sessions(self, engine) -> None:
        """List returns sessions for a project."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            await session_repo.create(
                organization_id=self.ORG_ID,
                project_id=project_id,
                created_by=user_id,
                external_id="session_1",
            )
            await session_repo.create(
                organization_id=self.ORG_ID,
                project_id=project_id,
                created_by=user_id,
                external_id="session_2",
            )

            sessions, cursor = await session_repo.list(
                self.ORG_ID, project_id, limit=10
            )
            # __default__ is excluded by default
            assert len(sessions) == 2
            assert cursor is None  # no more pages

    async def test_close_session(self, engine) -> None:
        """Close marks a session as closed."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            session = await session_repo.create(
                organization_id=self.ORG_ID,
                project_id=project_id,
                created_by=user_id,
                external_id="close_me",
            )
            closed = await session_repo.close(self.ORG_ID, session.id)
            assert closed is not None
            assert closed.closed_at is not None

    async def test_soft_delete_session(self, engine) -> None:
        """Soft delete marks session as deleted."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            session = await session_repo.create(
                organization_id=self.ORG_ID,
                project_id=project_id,
                created_by=user_id,
                external_id="delete_me",
            )
            deleted = await session_repo.soft_delete(self.ORG_ID, session.id)
            assert deleted is not None
            assert deleted.is_deleted is True

    async def test_update_metadata(self, engine) -> None:
        """update_metadata deep-merges new values."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            session = await session_repo.create(
                organization_id=self.ORG_ID,
                project_id=project_id,
                created_by=user_id,
                external_id="meta_test",
                metadata={"key1": "value1"},
            )
            updated = await session_repo.update_metadata(
                self.ORG_ID, session.id, {"key2": "value2"}
            )
            assert updated is not None
            meta = updated.metadata_ or {}
            assert meta.get("key1") == "value1"
            assert meta.get("key2") == "value2"

    async def test_message_count(self, engine) -> None:
        """message_count returns 0 for empty session."""
        async with AsyncSession(engine) as db:
            user_repo = UserRepository(db)
            project_repo = ProjectRepository(db)
            session_repo = SessionRepository(db)
            user_id, project_id = await self._seed_project(user_repo, project_repo)

            session = await session_repo.create(
                organization_id=self.ORG_ID,
                project_id=project_id,
                created_by=user_id,
                external_id="count_test",
            )
            count = await session_repo.message_count(session.id)
            assert count == 0
