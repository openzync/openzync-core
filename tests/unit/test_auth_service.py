"""Unit tests for AuthService — authentication business logic with mocked dependencies.

Covers: signup, email verification, password reset, passwordless login,
password login, MFA flows, profile CRUD, refresh token rotation, and internal
helpers (password validation, hash refresh token, issue tokens).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, PropertyMock, call, patch
from uuid import UUID, uuid4

import pytest

from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from repositories.auth_repository import AuthRepository
from schemas.auth import (
    DashboardUserResponse,
    LoginRequest,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaVerifyRequest,
    SignupRequest,
    TokenResponse,
    UpdateProfileRequest,
    VerifyEmailRequest,
)
from schemas.email import OtpResponse, ResetPasswordRequest, VerifyOtpRequest
from services.auth_service import AuthService


@pytest.mark.unit
class TestAuthService:
    """AuthService unit tests — all external IO mocked at the boundary."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    USER_ID = UUID("00000000-0000-0000-0000-000000000002")
    OTHER_USER_ID = UUID("00000000-0000-0000-0000-000000000003")

    # ── Fixtures ─────────────────────────────────────────────────────────────

    @pytest.fixture
    def mock_repo(self) -> AsyncMock:
        return AsyncMock(spec=AuthRepository)

    @pytest.fixture
    def mock_otp(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def mock_redis(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def mock_email_service(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def mock_bao_client(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def mock_org_repo(self) -> AsyncMock:
        """Org repository mock — required by the AuthService constructor
        (org-code join lookups).  Unused by the legacy flows under test."""
        return AsyncMock()

    @pytest.fixture
    def service(
        self,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
        mock_redis: AsyncMock,
        mock_email_service: AsyncMock,
        mock_bao_client: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> AuthService:
        return AuthService(
            repo=mock_repo,
            otp_service=mock_otp,
            redis=mock_redis,
            org_repo=mock_org_repo,
            email_service=mock_email_service,
            bao_client=mock_bao_client,
        )

    @pytest.fixture
    def service_no_bao(
        self,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
        mock_redis: AsyncMock,
        mock_email_service: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> AuthService:
        return AuthService(
            repo=mock_repo,
            otp_service=mock_otp,
            redis=mock_redis,
            org_repo=mock_org_repo,
            email_service=mock_email_service,
            bao_client=None,
        )

    @pytest.fixture
    def service_no_email(
        self,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
        mock_redis: AsyncMock,
        mock_bao_client: AsyncMock,
        mock_org_repo: AsyncMock,
    ) -> AuthService:
        return AuthService(
            repo=mock_repo,
            otp_service=mock_otp,
            redis=mock_redis,
            org_repo=mock_org_repo,
            email_service=None,
            bao_client=mock_bao_client,
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_mock_user(self, **kwargs: object) -> MagicMock:
        """Build a mock User-like object with configurable attributes."""
        user = MagicMock()
        user.id = kwargs.get("id", self.USER_ID)
        user.organization_id = kwargs.get("organization_id", self.ORG_ID)
        user.email = kwargs.get("email", "admin@acme.com")
        user.external_id = kwargs.get("email", "admin@acme.com")
        user.name = kwargs.get("name", "admin")
        user.password_hash = kwargs.get(
            "password_hash", "$2b$12$HashedPasswordStringForTesting"
        )
        user.role = kwargs.get("role", "admin")
        user.is_active = kwargs.get("is_active", True)
        user.is_deleted = kwargs.get("is_deleted", False)
        user.is_email_verified = kwargs.get("is_email_verified", True)
        user.mfa_enabled = kwargs.get("mfa_enabled", False)
        return user

    # ═══════════════════════════════════════════════════════════════════════
    # signup()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_signup_happy_path(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Signup creates org, seeds prompts, creates user, and sends OTP."""
        mock_repo.find_user_by_email.return_value = None
        mock_repo.create_organization.return_value = MagicMock(
            id=self.ORG_ID, name="Acme Corp", plan="free"
        )
        mock_repo.seed_prompts_for_org.return_value = 3
        mock_repo.create_dashboard_user.return_value = self._make_mock_user()

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",
            organization_name="Acme Corp",
        )

        result = await service.signup(payload)

        assert "Verification code sent" in result.message
        assert result.email == "admin@acme.com"

        mock_repo.find_user_by_email.assert_awaited_once_with("admin@acme.com")
        mock_repo.create_organization.assert_awaited_once_with(
            name="Acme Corp", plan="free"
        )
        mock_repo.seed_prompts_for_org.assert_awaited_once_with(self.ORG_ID)
        mock_repo.create_dashboard_user.assert_awaited_once()
        mock_otp.generate_and_send.assert_awaited_once_with(
            email="admin@acme.com", purpose="signup"
        )

    @pytest.mark.asyncio
    async def test_signup_existing_user_returns_generic_success(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Signup for an existing email returns the SAME generic success
        (anti-enumeration) instead of a 409, and creates nothing."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user()

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",
            organization_name="Acme Corp",
        )

        result = await service.signup(payload)

        assert "Verification code sent" in result.message
        assert result.email == "admin@acme.com"
        mock_repo.create_organization.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_signup_integrity_error_returns_generic_success(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Concurrent duplicate signup (IntegrityError) also returns the
        generic success, rolls back the aborted transaction."""
        mock_repo.find_user_by_email.return_value = None
        mock_repo.create_organization.side_effect = IntegrityError(
            "mock", "mock", "mock"
        )

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",
            organization_name="Acme Corp",
        )

        result = await service.signup(payload)

        assert "Verification code sent" in result.message
        mock_repo.rollback.assert_awaited_once()

    # ═══════════════════════════════════════════════════════════════════════
    # H5 — anti-enumeration: identical responses for existing vs missing
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_signup_identical_for_existing_and_new(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Signup returns an identical response for existing and new emails."""
        mock_repo.find_user_by_email.return_value = None
        mock_repo.create_organization.return_value = MagicMock(
            id=self.ORG_ID, name="Acme Corp", plan="free"
        )
        mock_repo.seed_prompts_for_org.return_value = 3
        mock_repo.create_dashboard_user.return_value = self._make_mock_user()

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",
            organization_name="Acme Corp",
        )
        fresh = await service.signup(payload)

        mock_repo.find_user_by_email.return_value = self._make_mock_user()
        existing = await service.signup(payload)

        assert existing.message == fresh.message
        assert existing.email == fresh.email
        assert existing.model_dump() == fresh.model_dump()

    @pytest.mark.parametrize(
        ("flow", "expected_fragment"),
        [
            ("forgot_password", "a password reset code has been sent"),
            ("generate_login_otp", "a login code has been sent"),
        ],
    )
    @pytest.mark.asyncio
    async def test_otp_send_flows_identical_for_missing_and_existing(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        flow: str,
        expected_fragment: str,
    ) -> None:
        """forgot_password and login/otp/send return identical messages for
        missing vs existing emails."""
        mock_repo.find_user_by_email.return_value = None
        missing = await getattr(service, flow)("missing@acme.com")

        mock_repo.find_user_by_email.return_value = self._make_mock_user()
        existing = await getattr(service, flow)("admin@acme.com")

        assert expected_fragment in missing.message
        assert missing.message == existing.message
        assert missing.model_dump() == existing.model_dump()

    @pytest.mark.asyncio
    async def test_resend_verification_identical_for_missing_and_existing(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """resend-otp returns an identical response for missing vs existing
        emails (both verified and unverified) for the same submitted email."""
        mock_repo.find_user_by_email.return_value = None
        missing = await service.resend_verification("admin@acme.com")

        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            is_email_verified=True
        )
        verified = await service.resend_verification("admin@acme.com")

        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            is_email_verified=False
        )
        unverified = await service.resend_verification("admin@acme.com")

        assert missing.model_dump() == verified.model_dump()
        assert missing.model_dump() == unverified.model_dump()

    # ═══════════════════════════════════════════════════════════════════════
    # verify_email()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_verify_email_happy_path_with_bao(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
        mock_bao_client: AsyncMock,
    ) -> None:
        """Verify email with Bao client — creates org namespace."""
        user = self._make_mock_user(is_email_verified=False)
        mock_repo.find_user_by_email.return_value = user
        mock_otp.verify.return_value = True
        mock_repo.mark_email_verified.return_value = self._make_mock_user(
            is_email_verified=True
        )

        payload = VerifyEmailRequest(email="admin@acme.com", otp="123456")

        with patch.object(
            service, "_issue_tokens"
        ) as mock_issue:
            mock_issue.return_value = TokenResponse(
                access_token="at", refresh_token="rt", expires_in=1800
            )
            result = await service.verify_email(payload)

        assert result.access_token == "at"
        mock_repo.mark_email_verified.assert_awaited_once_with(self.USER_ID)
        mock_bao_client.create_org_namespace.assert_awaited_once_with(
            self.ORG_ID
        )

    @pytest.mark.asyncio
    async def test_verify_email_happy_path_without_bao(
        self,
        service_no_bao: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Verify email without Bao client — skips namespace creation."""
        user = self._make_mock_user(is_email_verified=False)
        mock_repo.find_user_by_email.return_value = user
        mock_otp.verify.return_value = True

        payload = VerifyEmailRequest(email="admin@acme.com", otp="123456")

        with patch.object(
            service_no_bao, "_issue_tokens"
        ) as mock_issue:
            mock_issue.return_value = TokenResponse(
                access_token="at", refresh_token="rt", expires_in=1800
            )
            result = await service_no_bao.verify_email(payload)

        assert result.access_token == "at"
        mock_repo.mark_email_verified.assert_awaited_once_with(self.USER_ID)

    @pytest.mark.asyncio
    async def test_verify_email_already_verified_skips_db_update(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Already-verified user still validates OTP but skips mark_email_verified."""
        user = self._make_mock_user(is_email_verified=True)
        mock_repo.find_user_by_email.return_value = user
        mock_otp.verify.return_value = True

        payload = VerifyEmailRequest(email="admin@acme.com", otp="123456")

        with patch.object(
            service, "_issue_tokens"
        ) as mock_issue:
            mock_issue.return_value = TokenResponse(
                access_token="at", refresh_token="rt", expires_in=1800
            )
            result = await service.verify_email(payload)

        assert result.access_token == "at"
        mock_repo.mark_email_verified.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_verify_email_otp_failure_raises_auth_error(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Wrong OTP raises AuthenticationError."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            is_email_verified=False
        )
        mock_otp.verify.return_value = False

        payload = VerifyEmailRequest(email="admin@acme.com", otp="000000")

        with pytest.raises(AuthenticationError, match="Invalid or expired"):
            await service.verify_email(payload)

    @pytest.mark.asyncio
    async def test_verify_email_user_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Non-existent user raises AuthenticationError (anti-enumeration)."""
        mock_repo.find_user_by_email.return_value = None

        payload = VerifyEmailRequest(email="nobody@acme.com", otp="123456")

        with pytest.raises(
            AuthenticationError, match="Invalid or expired verification code"
        ):
            await service.verify_email(payload)

    # ═══════════════════════════════════════════════════════════════════════
    # resend_verification()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_resend_verification_already_verified(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Verified email returns the generic message without sending OTP."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            is_email_verified=True
        )
        mock_repo.find_user_by_email.return_value.is_email_verified = True

        result = await service.resend_verification("admin@acme.com")

        assert "verification code has been sent" in result.message
        service._otp_service.generate_and_send.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_resend_verification_sends_otp(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Unverified email resends OTP but returns the generic message."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            is_email_verified=False
        )

        result = await service.resend_verification("admin@acme.com")

        assert "verification code has been sent" in result.message
        mock_otp.generate_and_send.assert_awaited_once_with(
            email="admin@acme.com", purpose="signup"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # forgot_password()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_forgot_password_user_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Missing user returns the generic confirmation (anti-enumeration)."""
        mock_repo.find_user_by_email.return_value = None

        result = await service.forgot_password("nobody@acme.com")

        assert "a password reset code has been sent" in result.message
        service._otp_service.generate_and_send.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_forgot_password_no_password_hash(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Passwordless-only user returns the generic confirmation."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            password_hash=None
        )

        result = await service.forgot_password("otp@acme.com")

        assert "a password reset code has been sent" in result.message
        service._otp_service.generate_and_send.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_forgot_password_happy_path(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Forgot password sends OTP and returns confirmation."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user()

        result = await service.forgot_password("admin@acme.com")

        assert "code has been sent" in result.message
        mock_otp.generate_and_send.assert_awaited_once_with(
            email="admin@acme.com", purpose="password_reset"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # reset_password()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_reset_password_user_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Non-existent user raises the generic AuthenticationError."""
        mock_repo.find_user_by_email.return_value = None

        payload = ResetPasswordRequest(
            email="nobody@acme.com", otp="123456", new_password="NewStrong1"
        )

        with pytest.raises(AuthenticationError, match="Invalid or expired reset code"):
            await service.reset_password(payload)

    @pytest.mark.asyncio
    async def test_reset_password_otp_failure(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Invalid OTP raises AuthenticationError."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user()
        mock_otp.verify.return_value = False

        payload = ResetPasswordRequest(
            email="admin@acme.com", otp="000000", new_password="NewStrong1"
        )

        with pytest.raises(AuthenticationError, match="Invalid or expired"):
            await service.reset_password(payload)

    @pytest.mark.asyncio
    async def test_reset_password_happy_path(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Reset password validates new password, updates hash, revokes tokens."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user()
        mock_otp.verify.return_value = True

        payload = ResetPasswordRequest(
            email="admin@acme.com", otp="654321", new_password="NewStrong1"
        )

        with patch("services.auth_service.hash_password", return_value="new_hash"):
            result = await service.reset_password(payload)

        assert "reset successfully" in result.message
        mock_repo.update_dashboard_user.assert_awaited_once_with(
            user_id=self.USER_ID,
            password_hash="new_hash",
        )
        mock_repo.revoke_all_refresh_tokens.assert_awaited_once_with(
            self.USER_ID
        )
        mock_otp.invalidate.assert_awaited_once_with(
            email="admin@acme.com", purpose="password_reset"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # generate_login_otp()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_generate_login_otp_user_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Missing user returns the generic confirmation (anti-enumeration)."""
        mock_repo.find_user_by_email.return_value = None

        result = await service.generate_login_otp("nobody@acme.com")

        assert "a login code has been sent" in result.message
        service._otp_service.generate_and_send.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_generate_login_otp_happy_path(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Sends login OTP and returns the generic confirmation."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user()

        result = await service.generate_login_otp("admin@acme.com")

        assert "a login code has been sent" in result.message
        mock_otp.generate_and_send.assert_awaited_once_with(
            email="admin@acme.com", purpose="passwordless_login"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # passwordless_login()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_passwordless_login_user_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Missing user raises AuthenticationError (anti-enumeration)."""
        mock_repo.find_user_by_email.return_value = None

        payload = VerifyOtpRequest(email="nobody@acme.com", otp="123456")

        with pytest.raises(
            AuthenticationError, match="Invalid or expired login code"
        ):
            await service.passwordless_login(payload)

    @pytest.mark.asyncio
    async def test_passwordless_login_otp_failure(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Invalid OTP raises AuthenticationError."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user()
        mock_otp.verify.return_value = False

        payload = VerifyOtpRequest(email="admin@acme.com", otp="000000")

        with pytest.raises(AuthenticationError, match="Invalid or expired"):
            await service.passwordless_login(payload)

    @pytest.mark.asyncio
    async def test_passwordless_login_already_verified_skips_mark(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Already-verified user does not call mark_email_verified."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            is_email_verified=True
        )
        mock_otp.verify.return_value = True

        payload = VerifyOtpRequest(email="admin@acme.com", otp="123456")

        with patch.object(service, "_issue_tokens") as mock_issue:
            mock_issue.return_value = TokenResponse(
                access_token="at", refresh_token="rt", expires_in=1800
            )
            result = await service.passwordless_login(payload)

        assert result.access_token == "at"
        mock_repo.mark_email_verified.assert_not_awaited()
        mock_otp.invalidate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_passwordless_login_not_verified_calls_mark(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Unverified user has email auto-verified after OTP login."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            is_email_verified=False
        )
        mock_otp.verify.return_value = True

        payload = VerifyOtpRequest(email="admin@acme.com", otp="123456")

        with patch.object(service, "_issue_tokens") as mock_issue:
            mock_issue.return_value = TokenResponse(
                access_token="at", refresh_token="rt", expires_in=1800
            )
            result = await service.passwordless_login(payload)

        assert result.access_token == "at"
        mock_repo.mark_email_verified.assert_awaited_once_with(self.USER_ID)
        mock_otp.invalidate.assert_awaited_once()

    # ═══════════════════════════════════════════════════════════════════════
    # login()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_login_user_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Non-existent user raises AuthenticationError."""
        mock_repo.find_user_by_email.return_value = None

        payload = LoginRequest(email="nobody@acme.com", password="pass")

        with pytest.raises(AuthenticationError, match="Invalid email or password"):
            await service.login(payload)

    @pytest.mark.asyncio
    async def test_login_no_password_hash(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """User without password_hash raises AuthenticationError."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            password_hash=None
        )

        payload = LoginRequest(email="admin@acme.com", password="pass")

        with pytest.raises(
            AuthenticationError, match="does not have password authentication"
        ):
            await service.login(payload)

    @pytest.mark.asyncio
    async def test_login_wrong_password(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Wrong password raises AuthenticationError."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user()

        payload = LoginRequest(email="admin@acme.com", password="WrongPass1")

        with patch(
            "services.auth_service.verify_password", return_value=False
        ):
            with pytest.raises(AuthenticationError, match="Invalid email or password"):
                await service.login(payload)

    @pytest.mark.asyncio
    async def test_login_inactive_user(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Inactive or deleted user raises AuthenticationError."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            is_active=False, is_deleted=True
        )

        payload = LoginRequest(email="admin@acme.com", password="StrongPass1")

        with patch(
            "services.auth_service.verify_password", return_value=True
        ):
            with pytest.raises(AuthenticationError, match="deactivated"):
                await service.login(payload)

    @pytest.mark.asyncio
    async def test_login_email_not_verified(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Unverified email raises AuthenticationError."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            is_email_verified=False
        )

        payload = LoginRequest(email="admin@acme.com", password="StrongPass1")

        with patch(
            "services.auth_service.verify_password", return_value=True
        ):
            with pytest.raises(AuthenticationError, match="Email not verified"):
                await service.login(payload)

    @pytest.mark.asyncio
    async def test_login_mfa_enabled(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """MFA-enabled user receives mfa_session_token and OTP is sent."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            mfa_enabled=True
        )

        payload = LoginRequest(email="admin@acme.com", password="StrongPass1")

        with patch(
            "services.auth_service.verify_password", return_value=True
        ):
            result = await service.login(payload)

        assert result.requires_mfa is True
        assert result.mfa_session_token is not None
        assert result.access_token is None

        mock_otp.generate_and_send.assert_awaited_once_with(
            email="admin@acme.com", purpose="mfa"
        )
        mock_redis.setex.assert_awaited_once()
        key, ttl, data = mock_redis.setex.call_args[0]
        assert key.startswith("mfa:session:")

    @pytest.mark.asyncio
    async def test_login_mfa_disabled_issues_tokens(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """MFA-disabled user receives JWT tokens directly."""
        mock_repo.find_user_by_email.return_value = self._make_mock_user(
            mfa_enabled=False
        )

        payload = LoginRequest(email="admin@acme.com", password="StrongPass1")

        with patch(
            "services.auth_service.verify_password", return_value=True
        ):
            with patch.object(service, "_issue_tokens") as mock_issue:
                mock_issue.return_value = TokenResponse(
                    access_token="at",
                    refresh_token="rt",
                    expires_in=1800,
                )
                result = await service.login(payload)

        assert result.requires_mfa is False
        assert result.access_token == "at"
        assert result.refresh_token == "rt"

    # ═══════════════════════════════════════════════════════════════════════
    # mfa_verify()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_mfa_verify_invalid_session(
        self,
        service: AuthService,
        mock_redis: AsyncMock,
    ) -> None:
        """Missing or expired MFA session raises AuthenticationError."""
        mock_redis.get.return_value = None

        payload = MfaVerifyRequest(
            email="admin@acme.com",
            otp="123456",
            mfa_session_token="bad-token",
        )

        with pytest.raises(AuthenticationError, match="MFA session has expired or is invalid"):
            await service.mfa_verify(payload)

    @pytest.mark.asyncio
    async def test_mfa_verify_invalid_otp(
        self,
        service: AuthService,
        mock_redis: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Invalid MFA OTP raises AuthenticationError."""
        session_data = (
            '{"user_id":"00000000-0000-0000-0000-000000000002",'
            '"org_id":"00000000-0000-0000-0000-000000000001",'
            '"role":"admin"}'
        )
        mock_redis.get.return_value = session_data
        mock_otp.verify.return_value = False

        payload = MfaVerifyRequest(
            email="admin@acme.com",
            otp="000000",
            mfa_session_token="valid-token",
        )

        with pytest.raises(AuthenticationError, match="Invalid or expired"):
            await service.mfa_verify(payload)

    @pytest.mark.asyncio
    async def test_mfa_verify_happy_path(
        self,
        service: AuthService,
        mock_redis: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Valid MFA session and OTP issue JWT tokens."""
        session_data = (
            '{"user_id":"00000000-0000-0000-0000-000000000002",'
            '"org_id":"00000000-0000-0000-0000-000000000001",'
            '"role":"admin"}'
        )
        mock_redis.get.return_value = session_data
        mock_otp.verify.return_value = True

        payload = MfaVerifyRequest(
            email="admin@acme.com",
            otp="654321",
            mfa_session_token="valid-token",
        )

        with patch.object(service, "_issue_tokens") as mock_issue:
            mock_issue.return_value = TokenResponse(
                access_token="at", refresh_token="rt", expires_in=1800
            )
            result = await service.mfa_verify(payload)

        assert result.access_token == "at"
        assert result.refresh_token == "rt"
        mock_redis.delete.assert_awaited_once_with("mfa:session:valid-token")

    # ═══════════════════════════════════════════════════════════════════════
    # enable_mfa()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_enable_mfa_user_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Missing user raises NotFoundError."""
        mock_repo.get_user_by_id.return_value = None

        payload = MfaEnableRequest(password="StrongPass1")

        with pytest.raises(NotFoundError, match="not found"):
            await service.enable_mfa(self.USER_ID, payload)

    @pytest.mark.asyncio
    async def test_enable_mfa_wrong_password(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Wrong current password raises AuthenticationError."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()

        payload = MfaEnableRequest(password="WrongPass1")

        with patch(
            "services.auth_service.verify_password", return_value=False
        ):
            with pytest.raises(AuthenticationError, match="incorrect"):
                await service.enable_mfa(self.USER_ID, payload)

    @pytest.mark.asyncio
    async def test_enable_mfa_happy_path(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Enabling MFA sets the flag and sends confirmation OTP."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()

        payload = MfaEnableRequest(password="StrongPass1")

        with patch(
            "services.auth_service.verify_password", return_value=True
        ):
            result = await service.enable_mfa(self.USER_ID, payload)

        assert "MFA has been enabled" in result.message
        mock_repo.set_mfa_enabled.assert_awaited_once_with(
            self.USER_ID, enabled=True
        )
        mock_otp.generate_and_send.assert_awaited_once_with(
            email="admin@acme.com", purpose="mfa"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # disable_mfa()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_disable_mfa_user_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Missing user raises NotFoundError."""
        mock_repo.get_user_by_id.return_value = None

        payload = MfaDisableRequest(password="StrongPass1", otp="123456")

        with pytest.raises(NotFoundError, match="not found"):
            await service.disable_mfa(self.USER_ID, payload)

    @pytest.mark.asyncio
    async def test_disable_mfa_wrong_password(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Wrong password raises AuthenticationError."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()

        payload = MfaDisableRequest(password="WrongPass1", otp="123456")

        with patch(
            "services.auth_service.verify_password", return_value=False
        ):
            with pytest.raises(AuthenticationError, match="incorrect"):
                await service.disable_mfa(self.USER_ID, payload)

    @pytest.mark.asyncio
    async def test_disable_mfa_invalid_otp(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Invalid MFA OTP raises AuthenticationError."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()
        mock_otp.verify.return_value = False

        payload = MfaDisableRequest(password="StrongPass1", otp="000000")

        with patch(
            "services.auth_service.verify_password", return_value=True
        ):
            with pytest.raises(AuthenticationError, match="Invalid MFA code"):
                await service.disable_mfa(self.USER_ID, payload)

    @pytest.mark.asyncio
    async def test_disable_mfa_happy_path(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Disabling MFA clears the flag and invalidates the OTP."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()
        mock_otp.verify.return_value = True

        payload = MfaDisableRequest(password="StrongPass1", otp="654321")

        with patch(
            "services.auth_service.verify_password", return_value=True
        ):
            result = await service.disable_mfa(self.USER_ID, payload)

        assert "MFA has been disabled" in result.message
        mock_repo.set_mfa_enabled.assert_awaited_once_with(
            self.USER_ID, enabled=False
        )
        mock_otp.invalidate.assert_awaited_once_with(
            email="admin@acme.com", purpose="mfa"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # refresh()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_refresh_invalid_token(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Unknown token fails the atomic claim and raises AuthenticationError."""
        mock_repo.revoke_refresh_token_if_current.return_value = False
        mock_repo.get_refresh_token_by_hash.return_value = None

        with pytest.raises(AuthenticationError, match="invalid or has expired"):
            await service.refresh("bad-token")

    @pytest.mark.asyncio
    async def test_refresh_user_no_longer_exists(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Refresh raises AuthenticationError when user is gone."""
        mock_stored = AsyncMock()
        mock_stored.id = uuid4()
        mock_stored.user_id = str(self.USER_ID)
        mock_stored.organization_id = self.ORG_ID
        mock_repo.revoke_refresh_token_if_current.return_value = True
        mock_repo.get_refresh_token_by_hash.return_value = mock_stored
        mock_repo.get_user_by_id.return_value = None

        with pytest.raises(AuthenticationError, match="no longer exists"):
            await service.refresh("some-valid-token")

    @pytest.mark.asyncio
    async def test_refresh_happy_path(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Refresh atomically claims the old token, issues a new pair, and
        chains the rotation link."""
        old_token_id = uuid4()
        new_token_id = uuid4()
        stored = AsyncMock()
        stored.id = old_token_id
        stored.user_id = str(self.USER_ID)
        stored.organization_id = self.ORG_ID
        new_stored = AsyncMock()
        new_stored.id = new_token_id

        mock_repo.revoke_refresh_token_if_current.return_value = True
        mock_repo.get_refresh_token_by_hash.side_effect = [stored, new_stored]
        mock_repo.get_user_by_id.return_value = self._make_mock_user()

        with patch.object(service, "_issue_tokens") as mock_issue:
            mock_issue.return_value = TokenResponse(
                access_token="new-at",
                refresh_token="new-rt",
                expires_in=1800,
            )
            result = await service.refresh("old-raw-token")

        assert result.access_token == "new-at"
        assert result.refresh_token == "new-rt"
        mock_repo.revoke_refresh_token_if_current.assert_awaited_once_with(
            service._hash_refresh_token("old-raw-token")
        )
        mock_repo.set_refresh_token_rotated_by.assert_awaited_once_with(
            old_token_id, new_token_id
        )

    @pytest.mark.asyncio
    async def test_refresh_reuse_revokes_family_and_rejects(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Replaying a rotated token revokes the whole family and raises."""
        presented_id = uuid4()
        successor_id = uuid4()
        leaf_id = uuid4()

        presented = AsyncMock()
        presented.id = presented_id
        presented.user_id = str(self.USER_ID)
        presented.organization_id = self.ORG_ID
        presented.rotated_by = successor_id
        successor = AsyncMock()
        successor.id = successor_id
        successor.rotated_by = leaf_id
        leaf = AsyncMock()
        leaf.id = leaf_id
        leaf.rotated_by = None

        mock_repo.revoke_refresh_token_if_current.return_value = False
        mock_repo.get_refresh_token_by_hash.return_value = presented
        mock_repo.get_refresh_token_by_id.side_effect = [successor, leaf]

        with pytest.raises(AuthenticationError, match="invalid or has expired"):
            await service.refresh("replayed-token")

        # Whole chain walked and revoked: presented → successor → leaf.
        mock_repo.revoke_refresh_token_ids.assert_awaited_once_with(
            [presented_id, successor_id, leaf_id]
        )
        # The revocation is persisted before the rejection is raised, so
        # the request dependency cannot roll it back.
        mock_repo.commit.assert_awaited_once()
        service._otp_service.generate_and_send.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_refresh_concurrent_replay_yields_single_successor(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Two concurrent refreshes with the same token: exactly one wins,
        the loser triggers family revocation and a generic rejection."""
        token_id = uuid4()
        new_token_id = uuid4()
        stored = AsyncMock()
        stored.id = token_id
        stored.user_id = str(self.USER_ID)
        stored.organization_id = self.ORG_ID
        stored.rotated_by = None
        new_stored = AsyncMock()
        new_stored.id = new_token_id

        claims = {"n": 0}

        async def _claim(token_hash: str) -> bool:
            claims["n"] += 1
            return claims["n"] == 1  # first caller wins the claim

        async def _by_hash(token_hash: str) -> AsyncMock | None:
            return stored

        mock_repo.revoke_refresh_token_if_current.side_effect = _claim
        mock_repo.get_refresh_token_by_hash.side_effect = _by_hash
        mock_repo.get_user_by_id.return_value = self._make_mock_user()

        with patch.object(service, "_issue_tokens") as mock_issue:
            mock_issue.return_value = TokenResponse(
                access_token="at", refresh_token="new-rt", expires_in=1800
            )
            first = await service.refresh("same-token")
            with pytest.raises(AuthenticationError):
                await service.refresh("same-token")

        assert mock_issue.await_count == 1  # exactly ONE successor issued
        assert first.access_token == "at"
        # Loser walked the family (presented token only — no chain here).
        mock_repo.revoke_refresh_token_ids.assert_awaited_once_with(
            [token_id]
        )

    @pytest.mark.asyncio
    async def test_refresh_deactivated_user_rejected(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Deactivated users cannot refresh."""
        stored = AsyncMock()
        stored.id = uuid4()
        stored.user_id = str(self.USER_ID)
        stored.organization_id = self.ORG_ID
        mock_repo.revoke_refresh_token_if_current.return_value = True
        mock_repo.get_refresh_token_by_hash.return_value = stored
        mock_repo.get_user_by_id.return_value = self._make_mock_user(
            is_active=False
        )

        with pytest.raises(AuthenticationError, match="deactivated"):
            await service.refresh("some-token")

    # ═══════════════════════════════════════════════════════════════════════
    # get_profile()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_get_profile_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Non-existent user raises NotFoundError."""
        mock_repo.get_user_by_id.return_value = None

        with pytest.raises(NotFoundError, match="not found"):
            await service.get_profile(self.USER_ID)

    @pytest.mark.asyncio
    async def test_get_profile_happy_path(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Returns the user's public profile."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user(
            name="Alice",
            email="alice@acme.com",
            role="admin",
            is_email_verified=True,
            mfa_enabled=False,
        )

        result = await service.get_profile(self.USER_ID)

        assert isinstance(result, DashboardUserResponse)
        assert result.email == "alice@acme.com"
        assert result.name == "Alice"
        assert result.role == "admin"
        assert result.is_email_verified is True
        assert result.mfa_enabled is False

    # ═══════════════════════════════════════════════════════════════════════
    # update_profile()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_update_profile_not_found(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Non-existent user raises NotFoundError."""
        mock_repo.get_user_by_id.return_value = None

        payload = UpdateProfileRequest()

        with pytest.raises(NotFoundError, match="not found"):
            await service.update_profile(self.USER_ID, payload)

    @pytest.mark.asyncio
    async def test_update_profile_name(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Name update calls repo with new name."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()
        updated_user = self._make_mock_user(name="New Name")
        mock_repo.update_dashboard_user.return_value = updated_user

        payload = UpdateProfileRequest(name="New Name")

        result = await service.update_profile(self.USER_ID, payload)

        assert result.name == "New Name"
        mock_repo.update_dashboard_user.assert_awaited_once_with(
            user_id=self.USER_ID, name="New Name"
        )

    @pytest.mark.asyncio
    async def test_update_profile_email(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Email update triggers uniqueness check and re-verification."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user(
            email="old@acme.com"
        )
        mock_repo.find_user_by_email.return_value = None
        mock_repo.update_dashboard_user.return_value = self._make_mock_user(
            email="new@acme.com"
        )

        payload = UpdateProfileRequest(email="new@acme.com")

        result = await service.update_profile(self.USER_ID, payload)

        assert result.email == "new@acme.com"
        mock_repo.find_user_by_email.assert_awaited_once_with("new@acme.com")
        mock_repo.reset_email_verification.assert_awaited_once_with(self.USER_ID)
        mock_otp.generate_and_send.assert_awaited_once_with(
            email="new@acme.com", purpose="signup"
        )

    @pytest.mark.asyncio
    async def test_update_profile_email_conflict(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Changing to an existing email raises ConflictError."""
        existing_user = self._make_mock_user(
            id=self.OTHER_USER_ID, email="taken@acme.com"
        )
        mock_repo.get_user_by_id.return_value = self._make_mock_user(
            email="old@acme.com"
        )
        mock_repo.find_user_by_email.return_value = existing_user

        payload = UpdateProfileRequest(email="taken@acme.com")

        with pytest.raises(ConflictError, match="already in use"):
            await service.update_profile(self.USER_ID, payload)

    @pytest.mark.asyncio
    async def test_update_profile_password_change(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_email_service: AsyncMock,
    ) -> None:
        """Password change validates current password and sends notification."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()
        updated_user = self._make_mock_user()
        mock_repo.update_dashboard_user.return_value = updated_user

        payload = UpdateProfileRequest(
            current_password="StrongPass1",
            new_password="NewStrong2",
        )

        with patch(
            "services.auth_service.verify_password", return_value=True
        ):
            with patch(
                "services.auth_service.hash_password", return_value="new_hash"
            ):
                with patch(
                    "services.email_service.render_email_template",
                    return_value="<html>",
                ):
                    with patch(
                        "services.email_service.render_text_template",
                        return_value="text",
                    ):
                        result = await service.update_profile(
                            self.USER_ID, payload
                        )

        assert result.email == "admin@acme.com"
        mock_repo.update_dashboard_user.assert_awaited_once_with(
            user_id=self.USER_ID, password_hash="new_hash"
        )
        mock_email_service.send_email.assert_awaited_once_with(
            to="admin@acme.com",
            subject=ANY,
            html_body="<html>",
            text_body="text",
        )

    @pytest.mark.asyncio
    async def test_update_profile_password_no_current_password(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Password change without current_password raises ValidationError."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()

        payload = UpdateProfileRequest(
            current_password=None,
            new_password="NewStrong2",
        )

        with pytest.raises(ValidationError, match="Current password is required"):
            await service.update_profile(self.USER_ID, payload)

    @pytest.mark.asyncio
    async def test_update_profile_password_wrong_current(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Password change with wrong current password raises AuthenticationError."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()

        payload = UpdateProfileRequest(
            current_password="WrongPass1",
            new_password="NewStrong2",
        )

        with patch(
            "services.auth_service.verify_password", return_value=False
        ):
            with pytest.raises(AuthenticationError, match="incorrect"):
                await service.update_profile(self.USER_ID, payload)

    @pytest.mark.asyncio
    async def test_update_profile_password_skips_email_without_service(
        self,
        service_no_email: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Password change without email_service skips notification."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()
        mock_repo.update_dashboard_user.return_value = self._make_mock_user()

        payload = UpdateProfileRequest(
            current_password="StrongPass1",
            new_password="NewStrong2",
        )

        with patch(
            "services.auth_service.verify_password", return_value=True
        ):
            with patch(
                "services.auth_service.hash_password", return_value="new_hash"
            ):
                result = await service_no_email.update_profile(
                    self.USER_ID, payload
                )

        assert result.email == "admin@acme.com"
        mock_repo.update_dashboard_user.assert_awaited_once()

    # ═══════════════════════════════════════════════════════════════════════
    # _validate_password()
    # ═══════════════════════════════════════════════════════════════════════

    def test_validate_password_too_short(self, service: AuthService) -> None:
        """Password shorter than 8 characters raises ValidationError."""
        with pytest.raises(ValidationError, match="at least 8 characters"):
            service._validate_password("Ab1")

    def test_validate_password_no_uppercase(
        self, service: AuthService
    ) -> None:
        """Password without uppercase letter raises ValidationError."""
        with pytest.raises(ValidationError, match="uppercase"):
            service._validate_password("abcdefg1")

    def test_validate_password_no_lowercase(
        self, service: AuthService
    ) -> None:
        """Password without lowercase letter raises ValidationError."""
        with pytest.raises(ValidationError, match="lowercase"):
            service._validate_password("ABCDEFG1")

    def test_validate_password_no_digit(self, service: AuthService) -> None:
        """Password without any digit raises ValidationError."""
        with pytest.raises(ValidationError, match="digit"):
            service._validate_password("Abcdefgh")

    def test_validate_password_valid(self, service: AuthService) -> None:
        """A sufficiently complex password passes without error."""
        # Should not raise
        service._validate_password("ValidPass1")

    # ═══════════════════════════════════════════════════════════════════════
    # _hash_refresh_token()
    # ═══════════════════════════════════════════════════════════════════════

    def test_hash_refresh_token_deterministic(
        self, service: AuthService
    ) -> None:
        """Same input always produces the same hash."""
        h1 = service._hash_refresh_token("my-token")
        h2 = service._hash_refresh_token("my-token")
        assert h1 == h2

    def test_hash_refresh_token_different_inputs(
        self, service: AuthService
    ) -> None:
        """Different inputs produce different hashes."""
        h1 = service._hash_refresh_token("token-a")
        h2 = service._hash_refresh_token("token-b")
        assert h1 != h2

    # ═══════════════════════════════════════════════════════════════════════
    # _issue_tokens()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_issue_tokens_creates_token_pair(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """_issue_tokens returns a populated TokenResponse and persists refresh."""
        mock_repo.create_refresh_token.return_value = AsyncMock()

        with patch(
            "services.auth_service.create_jwt_token", return_value="jwt-access"
        ):
            with patch(
                "services.auth_service.secrets.token_hex", return_value="raw-refresh"
            ):
                result = await service._issue_tokens(
                    user_id=self.USER_ID,
                    organization_id=self.ORG_ID,
                    role="admin",
                )

        assert isinstance(result, TokenResponse)
        assert result.access_token == "jwt-access"
        assert result.refresh_token == "raw-refresh"
        assert result.expires_in == 1800  # default JWT_ACCESS_TOKEN_TTL_MINUTES=30
        assert result.token_type == "Bearer"

        mock_repo.create_refresh_token.assert_awaited_once()
        call_kwargs = mock_repo.create_refresh_token.call_args.kwargs
        assert call_kwargs["user_id"] == self.USER_ID
        assert call_kwargs["organization_id"] == self.ORG_ID
        assert call_kwargs["token_hash"] == service._hash_refresh_token(
            "raw-refresh"
        )
