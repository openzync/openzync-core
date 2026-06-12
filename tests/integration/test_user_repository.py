"""Integration tests for UserRepository — CRUD with real PostgreSQL.

Requires testcontainers PostgreSQL (provided by ``tests/integration/conftest.py``).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User
from repositories.user_repository import UserRepository


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
class TestUserRepository:
    """UserRepository CRUD + list + pagination tests."""

    async def _seed_user(
        self, repo: UserRepository, org_id: UUID, ext_id: str = "test_user"
    ) -> User:
        email = f"{ext_id}@example.com"
        return await repo.create(
            organization_id=org_id,
            external_id=ext_id,
            name="Test User",
            email=email,
        )

    async def test_create_user(self, engine) -> None:
        """Creating a user returns the user with generated fields."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            user = await self._seed_user(repo, org_id)

            assert user.id is not None
            assert user.external_id == "test_user"
            assert user.name == "Test User"
            assert user.email == "test_user@example.com"
            assert user.created_at is not None
            assert user.updated_at is not None

    async def test_get_by_external_id_found(self, engine) -> None:
        """get_by_external_id returns the matching user."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            user = await self._seed_user(repo, org_id)

            found = await repo.get_by_external_id(org_id, user.external_id)
            assert found is not None
            assert found.id == user.id

    async def test_get_by_external_id_not_found(self, engine) -> None:
        """get_by_external_id returns None for non-existent user."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            found = await repo.get_by_external_id(org_id, "nonexistent")
            assert found is None

    async def test_get_by_uuid_found(self, engine) -> None:
        """get_by_uuid returns the user for the correct org."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            user = await self._seed_user(repo, org_id)

            found = await repo.get_by_uuid(org_id, user.id)
            assert found is not None
            assert found.id == user.id

    async def test_get_by_uuid_wrong_org(self, engine) -> None:
        """get_by_uuid returns None for wrong org (tenant isolation)."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            user = await self._seed_user(repo, org_id)

            wrong_org = UUID("00000000-0000-0000-0000-000000000099")
            found = await repo.get_by_uuid(wrong_org, user.id)
            assert found is None

    async def test_update_user(self, engine) -> None:
        """Update modifies only specified fields."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            user = await self._seed_user(repo, org_id)

            updated = await repo.update(org_id, user.id, {"name": "Updated Name"})
            assert updated is not None
            assert updated.name == "Updated Name"
            assert updated.email == user.email  # unchanged

    async def test_update_not_found(self, engine) -> None:
        """Update on non-existent user returns None."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            fake_id = UUID("00000000-0000-0000-0000-000000000099")
            result = await repo.update(org_id, fake_id, {"name": "Whatever"})
            assert result is None

    async def test_soft_delete(self, engine) -> None:
        """Soft delete sets is_deleted=True."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            user = await self._seed_user(repo, org_id)

            deleted = await repo.soft_delete(org_id, user.id)
            assert deleted is not None
            assert deleted.is_deleted is True

    async def test_hard_delete(self, engine) -> None:
        """Hard delete removes the row permanently."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            user = await self._seed_user(repo, org_id)

            result = await repo.hard_delete(org_id, user.id)
            assert result is True

            # Verify gone
            found = await repo.get_by_uuid(org_id, user.id)
            assert found is None

    async def test_list_pagination(self, engine) -> None:
        """List returns paginated results with cursor."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")

            # Create 5 users
            for i in range(5):
                await self._seed_user(repo, org_id, ext_id=f"user_{i}")

            users, cursor = await repo.list(org_id, limit=3)
            assert len(users) == 3
            assert cursor is not None

            # Next page
            users2, cursor2 = await repo.list(org_id, limit=3, cursor=cursor)
            assert len(users2) > 0
            assert len(users2) <= 3

    async def test_exists_by_external_id(self, engine) -> None:
        """exists_by_external_id returns True for existing user."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            await self._seed_user(repo, org_id)

            assert await repo.exists_by_external_id(org_id, "test_user") is True
            assert await repo.exists_by_external_id(org_id, "nonexistent") is False

    async def test_get_stats_empty(self, engine) -> None:
        """get_stats returns zeros for user with no data."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            org_id = UUID("00000000-0000-0000-0000-000000000001")
            user = await self._seed_user(repo, org_id)

            stats = await repo.get_stats(user.id)
            assert stats == {"message_count": 0, "fact_count": 0, "session_count": 0}

    async def test_cursor_encode_decode_roundtrip(self, engine) -> None:
        """Cursor encoding and decoding round-trips correctly."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        uid = UUID("12345678-1234-5678-1234-567812345678")

        cursor = UserRepository._encode_cursor(now, uid)
        decoded_at, decoded_id = UserRepository._decode_cursor(cursor)

        assert decoded_at.replace(tzinfo=timezone.utc) == now
        assert decoded_id == uid
