"""Integration tests for UserRepository — CRUD with real PostgreSQL.

Requires testcontainers PostgreSQL (provided by ``tests/integration/conftest.py``).
"""
# ruff: noqa: S105, S106 — password fixtures are by design

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.user_repository import ClaimedInvite, UserRepository

if TYPE_CHECKING:
    from models.user import User


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
class TestUserRepository:
    """UserRepository CRUD + list + pagination tests."""

    async def _seed_user(
        self, repo: UserRepository, ext_id: str = "test_user"
    ) -> User:
        email = f"{ext_id}@example.com"
        return await repo.create(
            external_id=ext_id,
            name="Test User",
            email=email,
        )

    async def test_create_user(self, engine) -> None:
        """Creating a user returns the user with generated fields."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            user = await self._seed_user(repo)

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
            user = await self._seed_user(repo)

            found = await repo.get_by_external_id(user.external_id)
            assert found is not None
            assert found.id == user.id

    async def test_get_by_external_id_not_found(self, engine) -> None:
        """get_by_external_id returns None for non-existent user."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            found = await repo.get_by_external_id("nonexistent")
            assert found is None

    async def test_get_by_uuid_found(self, engine) -> None:
        """get_by_uuid returns the user for the correct org."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            user = await self._seed_user(repo)

            found = await repo.get_by_uuid(user.id)
            assert found is not None
            assert found.id == user.id

    async def test_get_by_uuid_unknown_id_returns_none(self, engine) -> None:
        """get_by_uuid returns None for an unknown id."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            user = await self._seed_user(repo)

            found = await repo.get_by_uuid(UUID("00000000-0000-0000-0000-000000000099"))
            assert found is None

    async def test_update_user(self, engine) -> None:
        """Update modifies only specified fields."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            user = await self._seed_user(repo)

            updated = await repo.update(user.id, {"name": "Updated Name"})
            assert updated is not None
            assert updated.name == "Updated Name"
            assert updated.email == user.email  # unchanged

    async def test_update_not_found(self, engine) -> None:
        """Update on non-existent user returns None."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            fake_id = UUID("00000000-0000-0000-0000-000000000099")
            result = await repo.update(fake_id, {"name": "Whatever"})
            assert result is None

    async def test_soft_delete(self, engine) -> None:
        """Soft delete sets is_deleted=True."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            user = await self._seed_user(repo)

            deleted = await repo.soft_delete(user.id)
            assert deleted is not None
            assert deleted.is_deleted is True

    async def test_hard_delete(self, engine) -> None:
        """Hard delete removes the row permanently."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            user = await self._seed_user(repo)

            result = await repo.hard_delete(user.id)
            assert result is True

            # Verify gone
            found = await repo.get_by_uuid(user.id)
            assert found is None

    async def test_list_pagination(self, engine) -> None:
        """List returns paginated results with cursor."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)

            # Create 5 users
            for i in range(5):
                await self._seed_user(repo, ext_id=f"user_{i}")

            users, cursor = await repo.list(user_id=uuid4(), limit=3)
            assert len(users) == 3
            assert cursor is not None

            # Next page
            users2, cursor2 = await repo.list(user_id=uuid4(), limit=3, cursor=cursor)
            assert len(users2) > 0
            assert len(users2) <= 3

    async def test_exists_by_external_id(self, engine) -> None:
        """exists_by_external_id returns True for existing user."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            await self._seed_user(repo)

            assert await repo.exists_by_external_id("test_user") is True
            assert await repo.exists_by_external_id("nonexistent") is False

    async def test_get_stats_empty(self, engine) -> None:
        """get_stats returns zeros for user with no data."""
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            user = await self._seed_user(repo)

            stats = await repo.get_stats(user.id)
            assert stats == {"message_count": 0, "fact_count": 0, "session_count": 0}

    async def test_cursor_encode_decode_roundtrip(self, engine) -> None:
        """Cursor encoding and decoding round-trips correctly."""
        now = datetime.now(UTC)
        uid = UUID("12345678-1234-5678-1234-567812345678")

        cursor = UserRepository._encode_cursor(
            "created_at", "desc", now.isoformat(), uid
        )
        sort_by, sort_dir, value, decoded_id = UserRepository._decode_cursor(cursor)

        assert sort_by == "created_at"
        assert sort_dir == "desc"
        assert datetime.fromisoformat(value).replace(tzinfo=UTC) == now
        assert decoded_id == uid

    async def test_claim_invite_returns_identity_and_clears_token(self, engine) -> None:
        """claim_invite returns the claimed identity and flips the row live.

        The winning UPDATE ... RETURNING must hand back exactly the identity
        the accept flow needs (id, org, role), leave ``invite_token_hash``
        NULL, set the password hash and mark the email verified — and a
        second claim of the same token must miss (replay-safe).
        """
        async with AsyncSession(engine) as db:
            repo = UserRepository(db)
            token_hash = hashlib.sha256(b"integration-claim-token").hexdigest()
            user = await repo.create(
                external_id="pending_invite_claim",
                email="pending@example.com",
                name="Pending Invitee",
                role="member",
                password_hash=None,
                invite_token_hash=token_hash,
            )

            cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=72)
            claimed = await repo.claim_invite(
                token_hash=token_hash,
                password_hash="hashed-password",
                cutoff=cutoff,
            )

            assert claimed is not None
            assert isinstance(claimed, ClaimedInvite)
            assert claimed.id == user.id
            assert claimed.role == "member"

            # The UPDATE ... RETURNING leaves the identity-mapped instance
            # stale — refresh to read the claimed row's new state.
            await db.refresh(user)
            assert user.invite_token_hash is None
            assert user.password_hash == "hashed-password"
            assert user.is_email_verified is True

            # Replay: the token hash is now NULL, so the claim misses.
            again = await repo.claim_invite(
                token_hash=token_hash,
                password_hash="hashed-password",
                cutoff=cutoff,
            )
            assert again is None
