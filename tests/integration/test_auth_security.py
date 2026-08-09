"""Integration tests for the auth security fixes — real PostgreSQL.

Covers:
- H2: atomic refresh-token claim (exactly one winner under concurrency),
  rotation-family revocation on replay, and deactivated-user refresh.
- All require testcontainers PostgreSQL.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings, set_settings
from core.exceptions import AuthenticationError
from models.refresh_token import RefreshToken
from repositories.auth_repository import AuthRepository
from schemas.auth import TokenResponse
from services.auth_service import AuthService

pytestmark = pytest.mark.integration


def _token_hash(raw: str) -> str:
    """SHA-256 hex digest the way AuthService hashes refresh tokens."""
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _init_settings() -> None:
    """Initialise the Settings singleton (AuthService._issue_tokens needs it)."""
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/test",
        REDIS_URL="redis://localhost:6379/1",
        SECRET_KEY="a" * 32,
        WEBHOOK_SIGNING_SECRET="b" * 32,
        ENVIRONMENT="test",
    )
    set_settings(settings)


async def _make_user(db: AsyncSession, email: str) -> object:
    """Create a bootstrap user + org row and return the user."""
    repo = AuthRepository(db)
    from models.organization import Organization

    org = Organization(name=f"Sec Test {uuid4()}", plan="free")
    db.add(org)
    await db.flush()
    user = await repo.create_dashboard_user(
        organization_id=org.id,
        email=email,
        password_hash="$2b$12$hash",
        role="admin",
    )
    return user


def _make_service(db: AsyncSession) -> AuthService:
    """Build an AuthService over a real repo with mocked side-dependencies."""
    return AuthService(
        repo=AuthRepository(db),
        otp_service=AsyncMock(),
        redis=AsyncMock(),
        org_repo=AsyncMock(),
        email_service=None,
        bao_client=None,
    )


class TestRefreshTokenAtomicClaim:
    """H2 — race-free rotation."""

    async def test_atomic_claim_exactly_one_winner(self, engine) -> None:
        """Two concurrent conditional UPDATEs claim the token exactly once."""
        async with AsyncSession(engine) as db:
            user = await _make_user(db, "claim@openzync.tech")
            raw = "claim-raw-token"
            await AuthRepository(db).create_refresh_token(
                user_id=user.id,
                organization_id=user.organization_id,
                token_hash=_token_hash(raw),
                expires_at=datetime.now() + timedelta(days=7),
            )
            await db.commit()

        outcomes: list[bool] = []

        async def _claim() -> None:
            async with AsyncSession(engine) as db:
                ok = await AuthRepository(db).revoke_refresh_token_if_current(
                    _token_hash(raw)
                )
                await db.commit()
                outcomes.append(ok)

        await asyncio.gather(_claim(), _claim())

        assert sorted(outcomes) == [False, True]

    async def test_sequential_replay_revokes_family(self, engine) -> None:
        """Refresh succeeds once; replaying the rotated token revokes the
        whole family (old token + successor) and is rejected generically."""
        async with AsyncSession(engine) as db:
            user = await _make_user(db, "replay@openzync.tech")
            raw = "replay-raw-token"
            await AuthRepository(db).create_refresh_token(
                user_id=user.id,
                organization_id=user.organization_id,
                token_hash=_token_hash(raw),
                expires_at=datetime.now() + timedelta(days=7),
            )
            await db.commit()

        async with AsyncSession(engine) as db:
            service = _make_service(db)
            first = await service.refresh(raw)
            await db.commit()
            successor_hash = _token_hash(first.refresh_token)
            assert first.access_token and first.refresh_token

        # First rotation: old token revoked, successor live and chained.
        async with AsyncSession(engine) as db:
            result = await db.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == _token_hash(raw)
                )
            )
            old = result.scalar_one()
            assert old.is_revoked is True
            result = await db.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == successor_hash
                )
            )
            successor = result.scalar_one()
            assert successor.is_revoked is False
            assert old.rotated_by == successor.id

        # Replay: claim fails → family walk revokes successor → reject.
        async with AsyncSession(engine) as db:
            service = _make_service(db)
            with pytest.raises(AuthenticationError, match="invalid or has expired"):
                await service.refresh(raw)
            await db.commit()

        async with AsyncSession(engine) as db:
            result = await db.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == successor_hash
                )
            )
            assert result.scalar_one().is_revoked is True

    async def test_concurrent_double_submit_single_successor(self, engine) -> None:
        """Two simultaneous refreshes with the same token: exactly one
        succeeds, the loser triggers family revocation — no token farming."""
        async with AsyncSession(engine) as db:
            user = await _make_user(db, "double@openzync.tech")
            user_id = str(user.id)
            raw = "double-raw-token"
            await AuthRepository(db).create_refresh_token(
                user_id=user_id,
                organization_id=user.organization_id,
                token_hash=_token_hash(raw),
                expires_at=datetime.now() + timedelta(days=7),
            )
            await db.commit()

        async def _refresh() -> TokenResponse | Exception:
            async with AsyncSession(engine) as db:
                service = _make_service(db)
                try:
                    result = await service.refresh(raw)
                    await db.commit()
                    return result
                except Exception as exc:  # noqa: BLE001 — collected below
                    await db.rollback()
                    return exc

        results = await asyncio.gather(_refresh(), _refresh())
        successes = [r for r in results if isinstance(r, TokenResponse)]
        errors = [r for r in results if not isinstance(r, TokenResponse)]

        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], AuthenticationError)

        # The family is dead: no live refresh token survives a double-submit.
        async with AsyncSession(engine) as db:
            result = await db.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.is_revoked.is_(False),
                )
            )
            assert result.scalars().all() == []

    async def test_deactivated_user_cannot_refresh(self, engine) -> None:
        """Refresh is rejected once the owning user is deactivated."""
        async with AsyncSession(engine) as db:
            user = await _make_user(db, "deactivated@openzync.tech")
            user.is_active = False
            raw = "deact-raw-token"
            await AuthRepository(db).create_refresh_token(
                user_id=user.id,
                organization_id=user.organization_id,
                token_hash=_token_hash(raw),
                expires_at=datetime.now() + timedelta(days=7),
            )
            await db.commit()

        async with AsyncSession(engine) as db:
            service = _make_service(db)
            with pytest.raises(AuthenticationError, match="deactivated"):
                await service.refresh(raw)
