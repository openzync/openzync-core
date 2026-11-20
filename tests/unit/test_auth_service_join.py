"""Unit tests for ``AuthService.join_organization`` — singleton-org join flow.

All external IO (repository, OTP service, password hashing) is mocked at
the service boundary.

Observed contract:
- open mode + new email → member user created + signup OTP sent.  The
  created user's role is ALWAYS ``"member"`` — never admin.
- Email already registered → generic ``SignupResponse``, no user created,
  no OTP sent (anti-enumeration — mirrors signup).
- invite_only mode → ``AuthorizationError`` (403), no user created, no OTP.
- approval mode → member created inactive, no OTP, same generic response.
- Concurrent duplicate (IntegrityError) → generic response + rollback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from core.exceptions import AuthorizationError
from repositories.auth_repository import AuthRepository
from schemas.auth import JoinRequest, SignupResponse
from services.auth_service import AuthService

pytestmark = pytest.mark.unit

USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _join_request(
    email: str = "alice@acme.com",
    password: str = "SecurePass1",
) -> JoinRequest:
    """Build a valid join request (password passes strength checks)."""
    return JoinRequest(email=email, password=password)


class TestJoinOrganization:
    """AuthService.join_organization unit tests."""

    @pytest.fixture
    def mock_repo(self) -> AsyncMock:
        repo = AsyncMock(spec=AuthRepository)
        repo.session = AsyncMock()
        return repo

    @pytest.fixture
    def mock_otp(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def mock_settings_repo(self) -> AsyncMock:
        repo = AsyncMock()
        repo.get_registration_mode.return_value = "open"
        return repo

    @pytest.fixture
    def service(
        self,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
        mock_settings_repo: AsyncMock,
    ) -> AuthService:
        return AuthService(
            repo=mock_repo,
            otp_service=mock_otp,
            redis=AsyncMock(),
            email_service=None,
            settings_repo=mock_settings_repo,
        )

    @pytest.mark.asyncio
    async def test_happy_path_creates_member_user_and_sends_otp(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Open mode + new email → member user + signup OTP."""
        mock_repo.find_user_by_email.return_value = None
        created = AsyncMock()
        created.id = USER_ID
        mock_repo.create_dashboard_user.return_value = created

        with patch(
            "services.auth_service.hash_password", return_value="hashed"
        ):
            result = await service.join_organization(_join_request())

        assert isinstance(result, SignupResponse)
        assert result.email == "alice@acme.com"

        create_kwargs = mock_repo.create_dashboard_user.call_args.kwargs
        assert create_kwargs["email"] == "alice@acme.com"
        assert create_kwargs["password_hash"] == "hashed"
        assert create_kwargs["role"] == "member"
        assert create_kwargs["permissions"] == ["project:read", "project:write"]
        mock_otp.generate_and_send.assert_awaited_once_with(
            email="alice@acme.com", purpose="signup"
        )

    @pytest.mark.asyncio
    async def test_existing_email_returns_generic_response_no_create_no_otp(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Existing email → generic success, no user created, no OTP sent."""
        mock_repo.find_user_by_email.return_value = AsyncMock()

        result = await service.join_organization(_join_request())

        assert isinstance(result, SignupResponse)
        assert result.email == "alice@acme.com"
        mock_repo.create_dashboard_user.assert_not_awaited()
        mock_otp.generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invite_only_mode_blocks_join_with_403(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
        mock_settings_repo: AsyncMock,
    ) -> None:
        """invite_only mode → 403 before any creation (even for new emails)."""
        mock_settings_repo.get_registration_mode.return_value = "invite_only"

        with pytest.raises(AuthorizationError):
            await service.join_organization(_join_request())

        mock_repo.create_dashboard_user.assert_not_awaited()
        mock_otp.generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invite_only_existing_email_still_403(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_settings_repo: AsyncMock,
    ) -> None:
        """invite_only rejects before the email lookup — no enumeration."""
        mock_settings_repo.get_registration_mode.return_value = "invite_only"
        mock_repo.find_user_by_email.return_value = AsyncMock()

        with pytest.raises(AuthorizationError):
            await service.join_organization(_join_request())

        mock_repo.create_dashboard_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approval_mode_creates_inactive_member_no_otp(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
        mock_settings_repo: AsyncMock,
    ) -> None:
        """approval mode → member created inactive, no OTP, generic response."""
        mock_settings_repo.get_registration_mode.return_value = "approval"
        mock_repo.find_user_by_email.return_value = None
        created = AsyncMock()
        created.id = USER_ID
        mock_repo.create_dashboard_user.return_value = created

        with patch(
            "services.auth_service.hash_password", return_value="hashed"
        ):
            result = await service.join_organization(_join_request())

        assert isinstance(result, SignupResponse)
        assert created.is_active is False
        mock_repo.flush.assert_awaited_once()
        mock_otp.generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_email_integrity_error_generic_response(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """IntegrityError on create → generic success + rollback, no OTP."""
        mock_repo.find_user_by_email.return_value = None
        mock_repo.create_dashboard_user.side_effect = IntegrityError(
            "mock", "mock", "mock"
        )

        with patch(
            "services.auth_service.hash_password", return_value="hashed"
        ):
            result = await service.join_organization(_join_request())

        assert isinstance(result, SignupResponse)
        mock_repo.rollback.assert_awaited_once()
        mock_otp.generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_join_mints_member_never_admin(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """The joined user's role is always member — never self-promoted."""
        mock_repo.find_user_by_email.return_value = None
        created = AsyncMock()
        created.id = USER_ID
        mock_repo.create_dashboard_user.return_value = created

        with patch(
            "services.auth_service.hash_password", return_value="hashed"
        ):
            await service.join_organization(_join_request())

        assert mock_repo.create_dashboard_user.call_args.kwargs["role"] == (
            "member"
        )
