"""Unit tests for AuthService MFA flows — login gate, verify, enable, disable.

All external dependencies (repository, OTP service, Redis, password hashing)
are mocked at the service boundary.  No real I/O occurs.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.exceptions import AuthenticationError
from schemas.auth import (
    LoginRequest,
    LoginResponse,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaVerifyRequest,
    TokenResponse,
)
from schemas.email import OtpResponse
from services.auth_service import AuthService


@pytest.mark.unit
class TestMfaService:
    """AuthService MFA flow unit tests."""

    USER_ID = UUID("00000000-0000-0000-0000-000000000001")
    ORG_ID = UUID("00000000-0000-0000-0000-000000000002")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[AuthService, AsyncMock, MagicMock, AsyncMock]:
        """Create an AuthService with mocked repo, OTP service, and Redis.

        Returns:
            Tuple of (service, mock_repo, mock_otp, mock_redis).
            ``mock_redis`` is an ``AsyncMock`` so its methods
            (``get``, ``setex``, ``delete``) are all awaitable by default.
        """
        mock_repo = AsyncMock()
        mock_otp = MagicMock()
        mock_redis = AsyncMock()
        service = AuthService(
            repo=mock_repo, otp_service=mock_otp, redis=mock_redis,
        )
        return service, mock_repo, mock_otp, mock_redis

    def _mock_user(self, **kwargs) -> MagicMock:
        """Build a MagicMock that mimics the User ORM attributes the service touches."""
        user = MagicMock()
        user.id = kwargs.get("id", self.USER_ID)
        user.organization_id = kwargs.get("org_id", self.ORG_ID)
        user.email = kwargs.get("email", "a@b.com")
        user.password_hash = kwargs.get(
            "password_hash", "$2b$12$abcdefghijklmnopqrstuvwxyz1234567890abcd"
        )
        user.name = kwargs.get("name", "Test User")
        user.is_active = kwargs.get("is_active", True)
        user.is_deleted = kwargs.get("is_deleted", False)
        user.is_email_verified = kwargs.get("is_email_verified", True)
        user.mfa_enabled = kwargs.get("mfa_enabled", False)
        user.role = kwargs.get("role", "admin")
        return user

    # ── Login — MFA disabled ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_login_without_mfa_returns_tokens(self) -> None:
        """Login without MFA returns LoginResponse with access_token."""
        service, mock_repo, _mock_otp, _mock_redis = self._make_service()

        mock_repo.find_user_by_email.return_value = self._mock_user(
            mfa_enabled=False,
        )

        fake_tokens = TokenResponse(
            access_token="at1",
            refresh_token="rt1",
            expires_in=1800,
        )
        service._issue_tokens = AsyncMock(return_value=fake_tokens)

        with patch("services.auth_service.verify_password", return_value=True):
            result = await service.login(
                LoginRequest(email="a@b.com", password="pass"),
            )

        assert isinstance(result, LoginResponse)
        assert result.requires_mfa is False
        assert result.access_token == "at1"
        assert result.refresh_token == "rt1"
        assert result.expires_in == 1800
        assert result.mfa_session_token is None

    # ── Login — MFA enabled ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_login_with_mfa_returns_challenge(self) -> None:
        """Login with MFA enabled returns requires_mfa=True + session token."""
        service, mock_repo, mock_otp, mock_redis = self._make_service()

        mock_repo.find_user_by_email.return_value = self._mock_user(
            mfa_enabled=True,
        )

        # Make generate_and_send awaitable
        mock_otp.generate_and_send = AsyncMock()

        with (
            patch("services.auth_service.verify_password", return_value=True),
            patch("services.auth_service.secrets.token_hex", return_value="mfa-session-abc"),
        ):
            result = await service.login(
                LoginRequest(email="a@b.com", password="pass"),
            )

        assert isinstance(result, LoginResponse)
        assert result.requires_mfa is True
        assert result.mfa_session_token == "mfa-session-abc"
        assert result.access_token is None
        assert result.refresh_token is None

        # Verify the MFA OTP was sent
        mock_otp.generate_and_send.assert_awaited_once_with(
            email="a@b.com",
            purpose="mfa",
        )

        # Verify the session was stored in Redis with correct key, TTL, and payload
        mock_redis.setex.assert_awaited_once()
        call_args = mock_redis.setex.await_args
        assert call_args is not None
        assert call_args[0][0] == "mfa:session:mfa-session-abc"
        assert call_args[0][1] == 600  # _MFA_SESSION_TTL_SEC
        payload = json.loads(call_args[0][2])
        assert payload["user_id"] == str(self.USER_ID)
        assert payload["org_id"] == str(self.ORG_ID)
        assert payload["role"] == "admin"

    # ── MFA verify — valid ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_mfa_verify_valid_session_and_otp(self) -> None:
        """mfa_verify returns tokens when session + OTP are valid."""
        service, mock_repo, mock_otp, mock_redis = self._make_service()

        # Redis returns valid session data
        session_data = {
            "user_id": str(self.USER_ID),
            "org_id": str(self.ORG_ID),
            "role": "admin",
        }
        mock_redis.get.return_value = json.dumps(session_data)
        mock_otp.verify = AsyncMock(return_value=True)

        fake_tokens = TokenResponse(
            access_token="at-mfa",
            refresh_token="rt-mfa",
            expires_in=1800,
        )
        service._issue_tokens = AsyncMock(return_value=fake_tokens)

        result = await service.mfa_verify(
            MfaVerifyRequest(
                email="a@b.com",
                otp="123456",
                mfa_session_token="session-tok",
            ),
        )

        assert isinstance(result, TokenResponse)
        assert result.access_token == "at-mfa"
        assert result.refresh_token == "rt-mfa"

        # Session was consumed (deleted)
        mock_redis.delete.assert_awaited_once_with(
            "mfa:session:session-tok",
        )

        # Tokens were issued with the correct identity
        service._issue_tokens.assert_awaited_once_with(
            user_id=self.USER_ID,
            organization_id=self.ORG_ID,
            role="admin",
        )

    # ── MFA verify — expired / invalid session ─────────────────────────────

    @pytest.mark.asyncio
    async def test_mfa_verify_expired_session_raises_error(self) -> None:
        """mfa_verify raises AuthenticationError when the session has expired."""
        service, mock_repo, mock_otp, mock_redis = self._make_service()

        # Redis returns nothing — session expired or never existed
        mock_redis.get.return_value = None

        with pytest.raises(AuthenticationError) as exc_info:
            await service.mfa_verify(
                MfaVerifyRequest(
                    email="a@b.com",
                    otp="123456",
                    mfa_session_token="stale-token",
                ),
            )

        assert "expired" in str(exc_info.value.message).lower() or "invalid" in str(
            exc_info.value.message,
        ).lower()

    # ── MFA verify — invalid OTP ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_mfa_verify_invalid_otp_raises_error(self) -> None:
        """mfa_verify raises AuthenticationError when the OTP is wrong."""
        service, mock_repo, mock_otp, mock_redis = self._make_service()

        session_data = {
            "user_id": str(self.USER_ID),
            "org_id": str(self.ORG_ID),
            "role": "admin",
        }
        mock_redis.get.return_value = json.dumps(session_data)
        mock_otp.verify = AsyncMock(return_value=False)  # OTP doesn't match

        with pytest.raises(AuthenticationError) as exc_info:
            await service.mfa_verify(
                MfaVerifyRequest(
                    email="a@b.com",
                    otp="000000",
                    mfa_session_token="tok",
                ),
            )

        assert "invalid" in str(exc_info.value.message).lower() or "expired" in str(
            exc_info.value.message,
        ).lower()

    # ── Enable MFA — correct password ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_enable_mfa_with_correct_password(self) -> None:
        """enable_mfa toggles MFA on and returns OtpResponse."""
        service, mock_repo, mock_otp, mock_redis = self._make_service()

        mock_repo.get_user_by_id.return_value = self._mock_user(
            password_hash="$2b$12$validhash",
        )
        mock_repo.set_mfa_enabled = AsyncMock()
        mock_otp.generate_and_send = AsyncMock()

        with patch("services.auth_service.verify_password", return_value=True):
            result = await service.enable_mfa(
                user_id=self.USER_ID,
                payload=MfaEnableRequest(password="correct-pass"),
            )

        assert isinstance(result, OtpResponse)
        assert "enabled" in result.message.lower()

        mock_repo.set_mfa_enabled.assert_awaited_once_with(
            self.USER_ID,
            enabled=True,
        )

    # ── Enable MFA — wrong password ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_enable_mfa_with_wrong_password_raises_error(self) -> None:
        """enable_mfa raises AuthenticationError when the password is wrong."""
        service, mock_repo, mock_otp, mock_redis = self._make_service()

        mock_repo.get_user_by_id.return_value = self._mock_user(
            password_hash="$2b$12$validhash",
        )

        with patch("services.auth_service.verify_password", return_value=False):
            with pytest.raises(AuthenticationError) as exc_info:
                await service.enable_mfa(
                    user_id=self.USER_ID,
                    payload=MfaEnableRequest(password="wrong-pass"),
                )

        assert "password" in str(exc_info.value.message).lower()
        mock_repo.set_mfa_enabled.assert_not_called()

    # ── Disable MFA — correct password + valid OTP ─────────────────────────

    @pytest.mark.asyncio
    async def test_disable_mfa_with_correct_password_and_valid_otp(self) -> None:
        """disable_mfa toggles MFA off when password and OTP are correct."""
        service, mock_repo, mock_otp, mock_redis = self._make_service()

        mock_repo.get_user_by_id.return_value = self._mock_user(
            password_hash="$2b$12$validhash",
        )
        mock_repo.set_mfa_enabled = AsyncMock()
        mock_otp.verify = AsyncMock(return_value=True)
        mock_otp.invalidate = AsyncMock()

        with patch("services.auth_service.verify_password", return_value=True):
            result = await service.disable_mfa(
                user_id=self.USER_ID,
                payload=MfaDisableRequest(password="correct-pass", otp="123456"),
            )

        assert isinstance(result, OtpResponse)
        assert "disabled" in result.message.lower()

        mock_repo.set_mfa_enabled.assert_awaited_once_with(
            self.USER_ID,
            enabled=False,
        )

    # ── Disable MFA — invalid OTP ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_disable_mfa_with_invalid_otp_raises_error(self) -> None:
        """disable_mfa raises AuthenticationError when the OTP is invalid."""
        service, mock_repo, mock_otp, mock_redis = self._make_service()

        mock_repo.get_user_by_id.return_value = self._mock_user(
            password_hash="$2b$12$validhash",
        )
        mock_otp.verify = AsyncMock(return_value=False)

        with patch("services.auth_service.verify_password", return_value=True):
            with pytest.raises(AuthenticationError) as exc_info:
                await service.disable_mfa(
                    user_id=self.USER_ID,
                    payload=MfaDisableRequest(
                        password="correct-pass",
                        otp="000000",
                    ),
                )

        assert "invalid" in str(exc_info.value.message).lower() or "code" in str(
            exc_info.value.message,
        ).lower()
        mock_repo.set_mfa_enabled.assert_not_called()
