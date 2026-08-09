"""Unit tests for ``AuthService.join_organization`` — org-code join flow.

All external IO (repository, org repository, OTP service, password hashing)
is mocked at the service boundary.

Observed contract (smoke-verified):
- Valid code + new email → member user created in the target org + signup
  OTP sent.  The created user's role is ALWAYS ``"member"`` — never admin.
- Unknown or inactive org code → ``ValidationError("Invalid organization code")``.
- Org with ``join_enabled=False`` → ``AuthorizationError`` (403), no user
  created, no OTP sent — distinct from the 422 for an unknown code.
- Email already registered anywhere → generic ``SignupResponse``, no user
  created, no OTP sent (anti-enumeration — mirrors signup).
- Codes are normalized (case-insensitive, surrounding whitespace stripped)
  before lookup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from core.exceptions import AuthorizationError, ValidationError
from repositories.auth_repository import AuthRepository
from repositories.organization_repository import OrganizationRepository
from schemas.auth import JoinRequest, SignupResponse
from services.auth_service import AuthService

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _join_request(
    email: str = "alice@acme.com",
    password: str = "SecurePass1",
    org_code: str = "K7M2Q9X4",
) -> JoinRequest:
    """Build a valid join request (password passes strength checks)."""
    return JoinRequest(email=email, password=password, org_code=org_code)


class TestJoinOrganization:
    """AuthService.join_organization unit tests."""

    @pytest.fixture
    def mock_repo(self) -> AsyncMock:
        return AsyncMock(spec=AuthRepository)

    @pytest.fixture
    def mock_org_repo(self) -> AsyncMock:
        return AsyncMock(spec=OrganizationRepository)

    @pytest.fixture
    def mock_otp(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def service(
        self,
        mock_repo: AsyncMock,
        mock_org_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> AuthService:
        return AuthService(
            repo=mock_repo,
            otp_service=mock_otp,
            redis=AsyncMock(),
            org_repo=mock_org_repo,
            email_service=None,
            bao_client=None,
        )

    def _make_org(
        self, code: str = "K7M2Q9X4", join_enabled: bool = True,
    ) -> AsyncMock:
        org = AsyncMock()
        org.id = ORG_ID
        org.org_code = code
        org.join_enabled = join_enabled
        return org

    @pytest.mark.asyncio
    async def test_happy_path_creates_member_user_and_sends_otp(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_org_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Valid code + new email → member user in target org + signup OTP."""
        mock_org_repo.get_by_code.return_value = self._make_org()
        mock_repo.find_user_by_email.return_value = None
        mock_repo.create_dashboard_user.return_value = AsyncMock()

        with patch("services.auth_service.hash_password", return_value="hashed"):
            result = await service.join_organization(_join_request())

        assert isinstance(result, SignupResponse)
        assert result.email == "alice@acme.com"

        mock_org_repo.get_by_code.assert_awaited_once_with("K7M2Q9X4")
        mock_repo.create_dashboard_user.assert_awaited_once_with(
            organization_id=ORG_ID,
            email="alice@acme.com",
            password_hash="hashed",
            name="alice",
            role="member",  # join NEVER creates an admin
        )
        mock_otp.generate_and_send.assert_awaited_once_with(
            email="alice@acme.com", purpose="signup"
        )

    @pytest.mark.asyncio
    async def test_unknown_code_raises_validation_error(
        self,
        service: AuthService,
        mock_org_repo: AsyncMock,
        mock_repo: AsyncMock,
    ) -> None:
        """Unknown org code → ValidationError, nothing else happens."""
        mock_org_repo.get_by_code.return_value = None

        with pytest.raises(ValidationError) as exc:
            await service.join_organization(_join_request())

        assert str(exc.value) == "Invalid organization code"
        mock_repo.find_user_by_email.assert_not_awaited()
        mock_repo.create_dashboard_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inactive_org_rejected_like_unknown_code(
        self,
        service: AuthService,
        mock_org_repo: AsyncMock,
        mock_repo: AsyncMock,
    ) -> None:
        """Inactive org → repo lookup returns None → same ValidationError.

        ``get_by_code`` filters on ``is_active``; a deactivated org's code
        must not accept new members.  The service sees the same "no active
        org" signal as an unknown code.
        """
        mock_org_repo.get_by_code.return_value = None

        with pytest.raises(ValidationError) as exc:
            await service.join_organization(_join_request())

        assert str(exc.value) == "Invalid organization code"
        mock_repo.create_dashboard_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_org_raises_authorization_error_no_create_no_otp(
        self,
        service: AuthService,
        mock_org_repo: AsyncMock,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Org with ``join_enabled=False`` → AuthorizationError (403).

        The code resolves to a real (active) org, so this must be distinct
        from the 422 for an unknown code — and no user is created and no
        OTP is sent.
        """
        mock_org_repo.get_by_code.return_value = self._make_org(
            join_enabled=False,
        )

        with pytest.raises(AuthorizationError) as exc:
            await service.join_organization(_join_request())

        assert str(exc.value) == "This organization is not accepting new members"
        mock_repo.find_user_by_email.assert_not_awaited()
        mock_repo.create_dashboard_user.assert_not_awaited()
        mock_otp.generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_org_existing_email_still_403(
        self,
        service: AuthService,
        mock_org_repo: AsyncMock,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Disabled org + already-registered email → still 403, NOT generic.

        Contract #1 ordering: the ``join_enabled`` check sits after
        ``get_by_code`` but BEFORE the existing-email anti-enumeration
        short-circuit.  A registered email must NOT leak the generic
        signup response for a paused org — the 403 wins, and the email
        lookup is never even reached.
        """
        mock_org_repo.get_by_code.return_value = self._make_org(
            join_enabled=False,
        )
        mock_repo.find_user_by_email.return_value = AsyncMock()  # registered

        with pytest.raises(AuthorizationError) as exc:
            await service.join_organization(_join_request())

        assert str(exc.value) == "This organization is not accepting new members"
        mock_repo.find_user_by_email.assert_not_awaited()
        mock_repo.create_dashboard_user.assert_not_awaited()
        mock_otp.generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_email_returns_generic_response_no_create_no_otp(
        self,
        service: AuthService,
        mock_org_repo: AsyncMock,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Email already registered → generic response; NO user, NO OTP.

        Anti-enumeration: the response is identical to a fresh join.
        """
        mock_org_repo.get_by_code.return_value = self._make_org()
        mock_repo.find_user_by_email.return_value = AsyncMock()  # exists

        result = await service.join_organization(_join_request())

        assert isinstance(result, SignupResponse)
        assert result.email == "alice@acme.com"
        mock_repo.create_dashboard_user.assert_not_awaited()
        mock_otp.generate_and_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_code_normalization_case_and_whitespace(
        self,
        service: AuthService,
        mock_org_repo: AsyncMock,
        mock_repo: AsyncMock,
    ) -> None:
        """Lowercase + surrounding whitespace → normalized before lookup.

        ``gce3gg9z`` must equal ``GCE3GG9Z`` — codes are case-insensitive.
        """
        mock_org_repo.get_by_code.return_value = self._make_org()
        mock_repo.find_user_by_email.return_value = None
        mock_repo.create_dashboard_user.return_value = AsyncMock()

        with patch("services.auth_service.hash_password", return_value="hashed"):
            await service.join_organization(
                _join_request(org_code="  gce3gg9z  ")
            )

        mock_org_repo.get_by_code.assert_awaited_once_with("GCE3GG9Z")

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_email_integrity_error_generic_response(
        self,
        service: AuthService,
        mock_org_repo: AsyncMock,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """Concurrent duplicate join → IntegrityError → generic response.

        The unique email index wins the race; the aborted transaction is
        rolled back and the generic anti-enumeration response is returned.
        """
        from sqlalchemy.exc import IntegrityError

        mock_org_repo.get_by_code.return_value = self._make_org()
        mock_repo.find_user_by_email.return_value = None
        mock_repo.create_dashboard_user.side_effect = IntegrityError(
            "stmt", {}, Exception("unique constraint")
        )

        with patch("services.auth_service.hash_password", return_value="hashed"):
            result = await service.join_organization(_join_request())

        assert isinstance(result, SignupResponse)
        assert result.email == "alice@acme.com"
        mock_repo.rollback.assert_awaited_once()
        mock_otp.generate_and_send.assert_not_awaited()
