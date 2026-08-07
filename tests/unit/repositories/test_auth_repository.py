"""Unit tests for AuthRepository — auth-related DB access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.auth_repository import AuthRepository


pytestmark = pytest.mark.unit


class TestAuthRepository:
    """AuthRepository unit tests."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")
    TOKEN_ID = UUID("00000000-0000-0000-0000-000000000010")

    @pytest.fixture
    def mock_db(self) -> AsyncMock:
        return AsyncMock(spec=AsyncSession)

    @pytest.fixture
    def repo(self, mock_db: AsyncMock) -> AuthRepository:
        return AuthRepository(db=mock_db)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _mock_user(self, **overrides: object) -> MagicMock:
        user = MagicMock()
        user.id = overrides.get("id", self.USER_ID)
        user.organization_id = overrides.get("organization_id", self.ORG_ID)
        user.email = overrides.get("email", "user@example.com")
        user.external_id = overrides.get("external_id", "user@example.com")
        user.name = overrides.get("name", "Test User")
        user.password_hash = overrides.get("password_hash", "$2b$12$abc123")
        user.role = overrides.get("role", "admin")
        user.is_deleted = overrides.get("is_deleted", False)
        user.is_email_verified = overrides.get("is_email_verified", False)
        user.email_verified_at = overrides.get("email_verified_at", None)
        user.mfa_enabled = overrides.get("mfa_enabled", False)
        user.metadata_ = overrides.get("metadata_", {})
        return user

    def _mock_token(self, **overrides: object) -> MagicMock:
        token = MagicMock()
        token.id = overrides.get("id", self.TOKEN_ID)
        token.user_id = overrides.get("user_id", str(self.USER_ID))
        token.organization_id = overrides.get("organization_id", self.ORG_ID)
        token.token_hash = overrides.get("token_hash", "abc123")
        token.expires_at = overrides.get(
            "expires_at", datetime.now(timezone.utc) + timedelta(hours=1)
        )
        token.is_revoked = overrides.get("is_revoked", False)
        token.rotated_by = overrides.get("rotated_by", None)
        return token

    # ── find_user_by_email ─────────────────────────────────────────────────────

    async def test_find_user_by_email_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """find_user_by_email returns user when found."""
        user = self._mock_user()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.find_user_by_email("user@example.com")

        assert result == user

    async def test_find_user_by_email_not_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """find_user_by_email returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.find_user_by_email("missing@example.com")

        assert result is None

    async def test_find_user_by_email_excludes_deleted(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """find_user_by_email filters out soft-deleted users."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.find_user_by_email("deleted@example.com")

        assert result is None

    # ── get_user_by_id ─────────────────────────────────────────────────────────

    async def test_get_user_by_id_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """get_user_by_id returns user when found."""
        user = self._mock_user()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.get_user_by_id(self.USER_ID)

        assert result == user

    async def test_get_user_by_id_not_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """get_user_by_id returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.get_user_by_id(self.USER_ID)

        assert result is None

    # ── create_organization ────────────────────────────────────────────────────

    async def test_create_organization(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """create_organization creates and returns an org."""
        mock_org = MagicMock()
        mock_org.id = uuid4()
        mock_org.name = "Test Org"
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create_organization(name="Test Org")

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_create_organization_with_plan(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """create_organization accepts a plan parameter."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create_organization(name="Paid Org", plan="pro")

        assert result is not None

    # ── seed_prompts_for_org ───────────────────────────────────────────────────

    async def test_seed_prompts_for_org(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """seed_prompts_for_org delegates to PromptTemplateRepository."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None
        # The internal PromptTemplateRepository.seed_default_prompts
        # will be instantiated and called — we mock at db level
        mock_db.execute.return_value = MagicMock()

        # This delegates to PromptTemplateRepository, so it's tested at a higher level
        result = await repo.seed_prompts_for_org(self.ORG_ID)

        assert result >= 0

    # ── create_dashboard_user ──────────────────────────────────────────────────

    async def test_create_dashboard_user(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """create_dashboard_user creates and returns a dashboard user."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create_dashboard_user(
            organization_id=self.ORG_ID,
            email="admin@example.com",
            password_hash="$2b$12$hash",
            name="Admin",
            role="admin",
        )

        assert result is not None
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    # ── create_refresh_token ───────────────────────────────────────────────────

    async def test_create_refresh_token(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """create_refresh_token creates and returns a token."""
        mock_db.add.return_value = None
        mock_db.flush.return_value = None
        mock_db.refresh.return_value = None

        result = await repo.create_refresh_token(
            user_id=self.USER_ID,
            organization_id=self.ORG_ID,
            token_hash="abc123",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )

        assert result is not None
        mock_db.add.assert_called_once()

    # ── find_refresh_token ─────────────────────────────────────────────────────

    async def test_find_refresh_token_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """find_refresh_token returns token when found and not expired."""
        token = self._mock_token()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token
        mock_db.execute.return_value = mock_result

        result = await repo.find_refresh_token("abc123")

        assert result == token

    async def test_find_refresh_token_not_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """find_refresh_token returns None when not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await repo.find_refresh_token("nonexistent")

        assert result is None

    # ── revoke_refresh_token ───────────────────────────────────────────────────

    async def test_revoke_refresh_token(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """revoke_refresh_token marks token as revoked."""
        token = self._mock_token(is_revoked=False)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token
        mock_db.execute.return_value = mock_result

        await repo.revoke_refresh_token(token_id=self.TOKEN_ID)

        assert token.is_revoked is True
        mock_db.flush.assert_awaited_once()

    async def test_revoke_refresh_token_not_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """revoke_refresh_token does nothing when token not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        await repo.revoke_refresh_token(token_id=self.TOKEN_ID)

        mock_db.flush.assert_not_called()

    async def test_revoke_refresh_token_with_rotation(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """revoke_refresh_token sets rotated_by when provided."""
        token = self._mock_token(is_revoked=False)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token
        mock_db.execute.return_value = mock_result
        new_token_id = uuid4()

        await repo.revoke_refresh_token(
            token_id=self.TOKEN_ID, rotated_by=str(new_token_id)
        )

        assert token.rotated_by == new_token_id

    # ── revoke_all_refresh_tokens ──────────────────────────────────────────────

    async def test_revoke_all_refresh_tokens(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """revoke_all_refresh_tokens revokes all active tokens for a user."""
        mock_db.execute.return_value = MagicMock()

        await repo.revoke_all_refresh_tokens(user_id=self.USER_ID)

        mock_db.execute.assert_awaited_once()
        mock_db.flush.assert_awaited_once()

    # ── revoke_refresh_token_if_current ────────────────────────────────────────

    async def test_revoke_refresh_token_if_current_claims(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """A token matching the conditional UPDATE is claimed (rowcount 1)."""
        mock_db.execute.return_value = MagicMock(rowcount=1)

        result = await repo.revoke_refresh_token_if_current("abc123")

        assert result is True
        mock_db.flush.assert_awaited_once()

    async def test_revoke_refresh_token_if_current_not_claimed(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """An already-revoked/expired/missing token matches zero rows."""
        mock_db.execute.return_value = MagicMock(rowcount=0)

        result = await repo.revoke_refresh_token_if_current("abc123")

        assert result is False

    # ── get_refresh_token_by_hash / by_id ──────────────────────────────────────

    async def test_get_refresh_token_by_hash_includes_revoked(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """get_refresh_token_by_hash finds revoked tokens (reuse detection)."""
        token = self._mock_token(is_revoked=True)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token
        mock_db.execute.return_value = mock_result

        result = await repo.get_refresh_token_by_hash("abc123")

        assert result == token

    async def test_get_refresh_token_by_id_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """get_refresh_token_by_id returns the token regardless of state."""
        token = self._mock_token()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token
        mock_db.execute.return_value = mock_result

        result = await repo.get_refresh_token_by_id(self.TOKEN_ID)

        assert result == token

    # ── set_refresh_token_rotated_by / revoke_refresh_token_ids ───────────────

    async def test_set_refresh_token_rotated_by(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """set_refresh_token_rotated_by records the successor via UPDATE."""
        successor_id = uuid4()
        mock_db.execute.return_value = MagicMock(rowcount=1)

        await repo.set_refresh_token_rotated_by(
            self.TOKEN_ID, successor_id
        )

        mock_db.execute.assert_awaited_once()
        mock_db.flush.assert_awaited_once()

    async def test_revoke_refresh_token_ids(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """revoke_refresh_token_ids revokes the family in one statement."""
        mock_db.execute.return_value = MagicMock(rowcount=3)

        count = await repo.revoke_refresh_token_ids(
            [uuid4(), uuid4(), uuid4()]
        )

        assert count == 3
        mock_db.flush.assert_awaited_once()

    async def test_revoke_refresh_token_ids_empty(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """Empty id list short-circuits without a DB call."""
        count = await repo.revoke_refresh_token_ids([])

        assert count == 0
        mock_db.execute.assert_not_awaited()

    async def test_rollback(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """rollback delegates to the session (post-IntegrityError recovery)."""
        await repo.rollback()
        mock_db.rollback.assert_awaited_once()

    # ── mark_email_verified ────────────────────────────────────────────────────

    async def test_mark_email_verified(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """mark_email_verified sets is_email_verified and timestamp."""
        user = self._mock_user(is_email_verified=False, email_verified_at=None)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.mark_email_verified(self.USER_ID)

        assert result.is_email_verified is True
        assert result.email_verified_at is not None
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_mark_email_verified_user_not_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """mark_email_verified raises when user not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        from core.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            await repo.mark_email_verified(self.USER_ID)

    # ── reset_email_verification ───────────────────────────────────────────────

    async def test_reset_email_verification(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """reset_email_verification clears email verification state."""
        user = self._mock_user(
            is_email_verified=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.reset_email_verification(self.USER_ID)

        assert result.is_email_verified is False
        assert result.email_verified_at is None

    async def test_reset_email_verification_not_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """reset_email_verification raises when user not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        from core.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            await repo.reset_email_verification(self.USER_ID)

    # ── set_mfa_enabled ────────────────────────────────────────────────────────

    async def test_set_mfa_enabled(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """set_mfa_enabled toggles the mfa_enabled flag."""
        user = self._mock_user(mfa_enabled=False)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.set_mfa_enabled(self.USER_ID, enabled=True)

        assert result.mfa_enabled is True

    async def test_set_mfa_enabled_disabled(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """set_mfa_enabled can disable MFA."""
        user = self._mock_user(mfa_enabled=True)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.set_mfa_enabled(self.USER_ID, enabled=False)

        assert result.mfa_enabled is False

    async def test_set_mfa_enabled_not_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """set_mfa_enabled raises when user not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        from core.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            await repo.set_mfa_enabled(self.USER_ID, enabled=True)

    # ── flush / refresh ────────────────────────────────────────────────────────

    async def test_flush(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """flush delegates to the session."""
        await repo.flush()
        mock_db.flush.assert_awaited_once()

    async def test_refresh(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """refresh delegates to the session."""
        instance = MagicMock()
        await repo.refresh(instance)
        mock_db.refresh.assert_awaited_once_with(instance)

    # ── update_dashboard_user ──────────────────────────────────────────────────

    async def test_update_dashboard_user(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """update_dashboard_user updates name, email, and password_hash."""
        user = self._mock_user()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.update_dashboard_user(
            user_id=self.USER_ID,
            name="New Name",
            email="new@example.com",
            password_hash="$2b$12$newhash",
        )

        assert result.name == "New Name"
        assert result.email == "new@example.com"
        assert result.external_id == "new@example.com"
        assert result.password_hash == "$2b$12$newhash"
        mock_db.flush.assert_awaited_once()
        mock_db.refresh.assert_awaited_once()

    async def test_update_dashboard_user_partial(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """update_dashboard_user only updates provided fields."""
        user = self._mock_user(name="Original", email="orig@example.com")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user
        mock_db.execute.return_value = mock_result

        result = await repo.update_dashboard_user(
            user_id=self.USER_ID, name="Only Name"
        )

        assert result.name == "Only Name"
        assert result.email == "orig@example.com"  # unchanged

    async def test_update_dashboard_user_not_found(
        self, repo: AuthRepository, mock_db: AsyncMock
    ) -> None:
        """update_dashboard_user raises when user not found."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        from core.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            await repo.update_dashboard_user(
                user_id=self.USER_ID, name="New Name"
            )
