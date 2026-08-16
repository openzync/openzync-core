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

    @pytest.fixture(autouse=True)
    def _default_platform_policy(self) -> None:
        """Default the platform policy to allow_all/both for legacy flows.

        ``signup``/``join_organization`` now consult the platform policy
        via ``get_system_config`` before acting.  The legacy tests assert
        pre-feature behaviour, so this fixture pins the backward-compatible
        default (``allow_all``/``both``) and the policy-gate tests override
        it explicitly.
        """
        from schemas.system_config import SystemConfigResponse

        with patch(
            "services.auth_service.get_system_config",
            new=AsyncMock(return_value=SystemConfigResponse()),
        ):
            yield

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
        user.must_change_password = kwargs.get("must_change_password", False)
        user.locale = kwargs.get("locale", "en")
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
            password="StrongPass1",  # noqa: S106 — test fixture
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
            password="StrongPass1",  # noqa: S106 — test fixture
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
            password="StrongPass1",  # noqa: S106 — test fixture
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
            password="StrongPass1",  # noqa: S106 — test fixture
            organization_name="Acme Corp",
        )
        fresh = await service.signup(payload)

        mock_repo.find_user_by_email.return_value = self._make_mock_user()
        existing = await service.signup(payload)

        assert existing.message == fresh.message
        assert existing.email == fresh.email
        assert existing.model_dump() == fresh.model_dump()

    # ═══════════════════════════════════════════════════════════════════════
    # Platform policy gates (superadmin layer)
    # ═══════════════════════════════════════════════════════════════════════

    def _policy_config(
        self,
        policy: str = "allow_all",
        scope: str = "both",
    ) -> object:
        """Build a SystemConfigResponse for a given policy/scope."""
        from schemas.system_config import SystemConfigResponse

        return SystemConfigResponse(
            org_creation_policy=policy,
            approval_scope=scope,
        )

    @pytest.mark.asyncio
    async def test_signup_reject_all_raises_403(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """reject_all blocks signup with 403 before any org creation."""
        from core.exceptions import AuthorizationError

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",  # noqa: S106 — test fixture
            organization_name="Acme Corp",
        )
        with (
            patch(
                "services.auth_service.get_system_config",
                new=AsyncMock(
                    return_value=self._policy_config(policy="reject_all"),
                ),
            ),
            pytest.raises(AuthorizationError),
        ):
            await service.signup(payload)

        mock_repo.create_organization.assert_not_awaited()
        mock_repo.create_dashboard_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_signup_approvals_public_signup_creates_pending_org(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
        mock_bao_client: AsyncMock,
    ) -> None:
        """approvals + public_signup scope → pending org + pending admin.

        Asserts the pending-path contract: org created with
        status='pending', admin user with password_hash=None, and NO
        namespace call, NO OTP.
        """
        from schemas.auth import PendingOrgResponse

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",  # noqa: S106 — test fixture
            organization_name="Acme Corp",
        )
        with patch(
            "services.auth_service.get_system_config",
            new=AsyncMock(
                return_value=self._policy_config(
                    policy="approvals", scope="public_signup"
                ),
            ),
        ):
            result = await service.signup(payload)

        assert isinstance(result, PendingOrgResponse)
        assert result.status == "pending"

        mock_repo.create_organization.assert_awaited_once_with(
            name="Acme Corp", plan="free", status="pending"
        )
        mock_repo.create_dashboard_user.assert_awaited_once()
        # Pending admin — no password hash yet (set at invite-accept time).
        assert (
            mock_repo.create_dashboard_user.call_args.kwargs["password_hash"]
            is None
        )
        # No OpenBao namespace, no OTP, no prompt seeding for pending orgs.
        mock_bao_client.create_org_namespace.assert_not_awaited()
        mock_otp.generate_and_send.assert_not_awaited()
        mock_repo.seed_prompts_for_org.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_signup_approvals_duplicate_email_returns_generic_success(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Concurrent duplicate in the approvals path (IntegrityError) →
        generic success + rollback — no pending org leaks, no 409, no
        account enumeration."""
        mock_repo.create_organization.side_effect = IntegrityError(
            "mock", "mock", "mock"
        )

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",  # noqa: S106 — test fixture
            organization_name="Acme Corp",
        )
        with patch(
            "services.auth_service.get_system_config",
            new=AsyncMock(
                return_value=self._policy_config(
                    policy="approvals", scope="public_signup"
                ),
            ),
        ):
            result = await service.signup(payload)

        assert "Verification code sent" in result.message
        mock_repo.rollback.assert_awaited_once()
        mock_repo.create_organization.assert_awaited_once_with(
            name="Acme Corp", plan="free", status="pending"
        )

    @pytest.mark.asyncio
    async def test_signup_allow_all_unchanged(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_otp: AsyncMock,
    ) -> None:
        """allow_all keeps the live path — verified admin + OTP sent."""
        mock_repo.find_user_by_email.return_value = None
        mock_repo.create_organization.return_value = MagicMock(
            id=self.ORG_ID, name="Acme Corp", plan="free"
        )
        mock_repo.seed_prompts_for_org.return_value = 3
        mock_repo.create_dashboard_user.return_value = self._make_mock_user()

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",  # noqa: S106 — test fixture
            organization_name="Acme Corp",
        )
        # Policy fixture defaults to allow_all already; assert explicitly.
        with patch(
            "services.auth_service.get_system_config",
            new=AsyncMock(return_value=self._policy_config()),
        ):
            result = await service.signup(payload)

        assert "Verification code sent" in result.message
        mock_repo.create_organization.assert_awaited_once_with(
            name="Acme Corp", plan="free"
        )
        mock_otp.generate_and_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_signup_system_name_rejected(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """The reserved SYSTEM org name is rejected even under allow_all."""
        from core.exceptions import ValidationError

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",  # noqa: S106 — test fixture
            organization_name="system",
        )
        with (
            patch(
                "services.auth_service.get_system_config",
                new=AsyncMock(return_value=self._policy_config()),
            ),
            pytest.raises(ValidationError),
        ):
            await service.signup(payload)

        mock_repo.create_organization.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_signup_approvals_without_public_signup_raises_403(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """approvals WITHOUT public_signup scope → 403, never live creation.

        A non-selected channel is rejected — it must not fall through to
        instant org creation nor create a pending row.
        """
        from core.exceptions import AuthorizationError

        payload = SignupRequest(
            email="admin@acme.com",
            password="StrongPass1",  # noqa: S106 — test fixture
            organization_name="Acme Corp",
        )
        with (
            patch(
                "services.auth_service.get_system_config",
                new=AsyncMock(
                    return_value=self._policy_config(
                        policy="approvals", scope="in_app"
                    ),
                ),
            ),
            pytest.raises(AuthorizationError),
        ):
            await service.signup(payload)

        mock_repo.create_organization.assert_not_awaited()
        mock_repo.create_dashboard_user.assert_not_awaited()
        mock_repo.seed_prompts_for_org.assert_not_awaited()

    # ═══════════════════════════════════════════════════════════════════════
    # change_password()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_change_password_rotates_session_and_clears_flag(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
        mock_redis: AsyncMock,
    ) -> None:
        """change_password verifies old pw, sets new hash, clears the flag,
        revokes refresh-token family, and returns fresh tokens."""
        from schemas.auth import ChangePasswordRequest

        user = self._make_mock_user()
        user.password_hash = "hashed-old"  # noqa: S105 — test fixture hash
        mock_repo.get_user_by_id.return_value = user
        mock_repo.update_dashboard_user.return_value = user
        mock_repo.create_refresh_token.return_value = MagicMock()

        payload = ChangePasswordRequest(  # noqa: S106 — test fixture credential
            old_password="OldPass1",  # noqa: S106 — test fixture
            new_password="NewPass1",  # noqa: S106 — test fixture
        )
        with (
            patch("services.auth_service.verify_password", return_value=True),
            patch("services.auth_service.hash_password", return_value="hashed-new"),
            patch(
                "services.auth_service.invalidate_must_change_password",
                new=AsyncMock(),
            ) as mock_invalidate,
        ):
            tokens = await service.change_password(
                user_id=self.USER_ID,
                payload=payload,
            )

        assert tokens.access_token  # fresh pair returned
        mock_repo.update_dashboard_user.assert_awaited_once_with(
            user_id=self.USER_ID,
            password_hash="hashed-new",  # noqa: S106 — test fixture
            must_change_password=False,
        )
        mock_repo.revoke_all_refresh_tokens.assert_awaited_once_with(
            self.USER_ID
        )
        mock_invalidate.assert_awaited_once_with(mock_redis, self.USER_ID)

    @pytest.mark.asyncio
    async def test_change_password_wrong_old_password_raises(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Wrong old password → AuthenticationError, nothing changes."""
        from core.exceptions import AuthenticationError
        from schemas.auth import ChangePasswordRequest

        user = self._make_mock_user()
        user.password_hash = "hashed-old"  # noqa: S105 — test fixture hash
        mock_repo.get_user_by_id.return_value = user

        payload = ChangePasswordRequest(  # noqa: S106 — test fixture credential
            old_password="WrongPass1",  # noqa: S106 — test fixture
            new_password="NewPass1",  # noqa: S106 — test fixture
        )
        with (
            patch("services.auth_service.verify_password", return_value=False),
            pytest.raises(AuthenticationError),
        ):
            await service.change_password(
                user_id=self.USER_ID,
                payload=payload,
            )

        mock_repo.update_dashboard_user.assert_not_awaited()
        mock_repo.revoke_all_refresh_tokens.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_change_password_weak_new_password_rejected(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """A weak new password → ValidationError before any mutation."""
        from core.exceptions import ValidationError
        from schemas.auth import ChangePasswordRequest

        user = self._make_mock_user()
        user.password_hash = "hashed-old"  # noqa: S105 — test fixture hash
        mock_repo.get_user_by_id.return_value = user

        # 8 chars but no uppercase letter — passes the schema min_length
        # but fails the service-level strength rule.
        payload = ChangePasswordRequest(  # noqa: S106 — test fixture credential
            old_password="OldPass1",  # noqa: S106 — test fixture
            new_password="short123",  # noqa: S106 — test fixture
        )
        with (
            patch("services.auth_service.verify_password", return_value=True),
            pytest.raises(ValidationError),
        ):
            await service.change_password(
                user_id=self.USER_ID,
                payload=payload,
            )

        mock_repo.update_dashboard_user.assert_not_awaited()

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
            service, "issue_tokens"
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
            service_no_bao, "issue_tokens"
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
            service, "issue_tokens"
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
            email="admin@acme.com", purpose="signup",
            locale="en",
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
            email="admin@acme.com", purpose="password_reset",
            locale="en",
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
            email="admin@acme.com", purpose="passwordless_login",
            locale="en",
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

        with patch.object(service, "issue_tokens") as mock_issue:
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

        with patch.object(service, "issue_tokens") as mock_issue:
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
            email="admin@acme.com", purpose="mfa",
            locale="en",
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

        with (
            patch("services.auth_service.verify_password", return_value=True),
            patch.object(service, "issue_tokens") as mock_issue,
        ):
            mock_issue.return_value = TokenResponse(
                access_token="at",  # noqa: S106 — test fixture token
                refresh_token="rt",  # noqa: S106 — test fixture token
                expires_in=1800,
            )
            result = await service.login(payload)

        assert result.requires_mfa is False
        assert result.access_token == "at"  # noqa: S105 — test fixture
        assert result.refresh_token == "rt"

    @pytest.mark.asyncio
    async def test_login_root_user_looks_up_by_root_and_issues_tokens(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """The seeded root user logs in via email='root' like any user.

        The root user's row (email='root', must_change_password=True) is
        looked up by the raw string — login itself is NOT gated by
        must_change_password; only dashboard APIs are.
        """
        root = self._make_mock_user(email="root")
        root.must_change_password = True
        root.role = "superadmin"
        mock_repo.find_user_by_email.return_value = root

        payload = LoginRequest(  # noqa: S106 — test fixture credential
            email="root", password="admin"
        )

        with (
            patch("services.auth_service.verify_password", return_value=True),
            patch.object(service, "issue_tokens") as mock_issue,
        ):
            mock_issue.return_value = TokenResponse(
                access_token="at",  # noqa: S106 — test fixture token
                refresh_token="rt",  # noqa: S106 — test fixture token
                expires_in=1800,
            )
            result = await service.login(payload)

        mock_repo.find_user_by_email.assert_awaited_once_with("root")
        assert result.requires_mfa is False
        assert result.access_token == "at"  # noqa: S105 — test fixture
        mock_issue.assert_awaited_once_with(
            user_id=root.id,
            organization_id=root.organization_id,
            role="superadmin",  # root's role claim comes from the DB row
        )

    def test_login_schema_accepts_root_and_rejects_malformed(self) -> None:
        """LoginRequest admits 'root' (case-insensitive) and keeps the
        EmailStr format check for everything else."""
        from pydantic import ValidationError

        # root forms normalize to 'root'
        for raw in ("root", "ROOT", "  Root  "):
            assert LoginRequest(  # noqa: S106 — test fixture credential
                email=raw, password="admin"
            ).email == "root"

        # malformed addresses (including 'root@') still fail with 422
        for bad in ("root@", "not-an-email", "@x.com", ""):
            with pytest.raises(ValidationError):
                LoginRequest(  # noqa: S106 — test fixture credential
                    email=bad, password="admin"
                )

        # regular email semantics unchanged — domain lowercased, local
        # part preserved, whitespace stripped
        assert (
            LoginRequest(  # noqa: S106 — test fixture credential
                email="John.Doe@Example.COM", password="x"
            ).email
            == "John.Doe@example.com"
        )

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

        with patch.object(service, "issue_tokens") as mock_issue:
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
            email="admin@acme.com", purpose="mfa",
            locale="en",
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

        with patch.object(service, "issue_tokens") as mock_issue:
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

        with patch.object(service, "issue_tokens") as mock_issue:
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
    async def test_get_profile_includes_locale(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """get_profile echoes the stored user locale."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user(locale="en")

        result = await service.get_profile(self.USER_ID)

        assert result.locale == "en"

    @pytest.mark.asyncio
    async def test_update_profile_locale(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """A supported locale is persisted via the repo."""
        mock_repo.get_user_by_id.return_value = self._make_mock_user()
        mock_repo.update_dashboard_user.return_value = self._make_mock_user(
            locale="en"
        )

        payload = UpdateProfileRequest(locale="en")

        result = await service.update_profile(self.USER_ID, payload)

        assert result.locale == "en"
        mock_repo.update_dashboard_user.assert_awaited_once_with(
            user_id=self.USER_ID, locale="en"
        )

    @pytest.mark.asyncio
    async def test_update_profile_locale_unsupported_raises(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """A direct service call with an unsupported locale raises ValidationError.

        The HTTP layer rejects unknown tags in the schema; this guard covers
        in-process callers (workers, tests) that bypass schema validation.
        """
        mock_repo.get_user_by_id.return_value = self._make_mock_user()
        payload = UpdateProfileRequest(locale="en")
        payload.locale = "xx"  # bypass the schema validator — direct call path

        with pytest.raises(ValidationError, match="Unsupported locale 'xx'"):
            await service.update_profile(self.USER_ID, payload)
        mock_repo.update_dashboard_user.assert_not_awaited()

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
            email="new@acme.com", purpose="signup",
            locale="en",
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
    # issue_tokens()
    # ═══════════════════════════════════════════════════════════════════════

    @pytest.mark.asyncio
    async def test_issue_tokens_creates_token_pair(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """issue_tokens returns a populated TokenResponse and persists refresh."""
        mock_repo.create_refresh_token.return_value = AsyncMock()

        with patch(
            "services.auth_service.create_jwt_token", return_value="jwt-access"
        ):
            with patch(
                "services.auth_service.secrets.token_hex", return_value="raw-refresh"
            ):
                result = await service.issue_tokens(
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

    @pytest.mark.asyncio
    async def test_issue_tokens_includes_mcp_claim_from_db_row(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """The access token carries an ``mcp`` claim read from the DB row.

        With must_change_password=True on the row, the minted JWT data has
        ``mcp: True``; after the flag is cleared, the next issuance has
        ``mcp: False`` — the claim always reflects issue-time state.
        """
        mock_repo.create_refresh_token.return_value = AsyncMock()
        user = self._make_mock_user(must_change_password=True)
        mock_repo.get_user_by_id.return_value = user

        captured: dict = {}

        def _capture_jwt(data: dict, **kwargs: object) -> str:
            captured.update(data)
            return "jwt-access"

        with (
            patch(
                "services.auth_service.create_jwt_token",
                side_effect=_capture_jwt,
            ),
            patch(
                "services.auth_service.secrets.token_hex",
                return_value="raw-refresh",
            ),
        ):
            await service.issue_tokens(
                user_id=self.USER_ID,
                organization_id=self.ORG_ID,
                role="admin",
            )
        assert captured["mcp"] is True
        mock_repo.get_user_by_id.assert_awaited_once_with(self.USER_ID)

        # Flag cleared on the row → next issuance carries False.
        user.must_change_password = False
        mock_repo.get_user_by_id.reset_mock()
        with (
            patch(
                "services.auth_service.create_jwt_token",
                side_effect=_capture_jwt,
            ),
            patch(
                "services.auth_service.secrets.token_hex",
                return_value="raw-refresh",
            ),
        ):
            await service.issue_tokens(
                user_id=self.USER_ID,
                organization_id=self.ORG_ID,
                role="admin",
            )
        assert captured["mcp"] is False

    @pytest.mark.asyncio
    async def test_refresh_reissues_with_fresh_mcp_claim(
        self,
        service: AuthService,
        mock_repo: AsyncMock,
    ) -> None:
        """Refresh re-reads the user row and mints a fresh mcp claim.

        The refreshed token must reflect the CURRENT flag value — it never
        copies the old token's claim.  After the user changes their
        password (flag cleared), the next refresh issues mcp: False.
        """
        old_token_id = uuid4()
        stored = AsyncMock()
        stored.id = old_token_id
        stored.user_id = str(self.USER_ID)
        stored.organization_id = self.ORG_ID
        new_stored = AsyncMock()
        new_stored.id = uuid4()

        user = self._make_mock_user(must_change_password=True)
        mock_repo.revoke_refresh_token_if_current.return_value = True
        mock_repo.get_user_by_id.return_value = user
        mock_repo.create_refresh_token.return_value = AsyncMock()

        captured: dict = {}

        def _capture_jwt(data: dict, **kwargs: object) -> str:
            captured.update(data)
            return "jwt-access"

        # First refresh while the flag is still set → mcp: True
        mock_repo.get_refresh_token_by_hash.side_effect = [stored, new_stored]
        with patch("services.auth_service.create_jwt_token", side_effect=_capture_jwt):
            await service.refresh("old-raw-token")
        assert captured["mcp"] is True

        # change_password clears the flag → next refresh issues mcp: False
        user.must_change_password = False
        stored2 = AsyncMock()
        stored2.id = uuid4()
        stored2.user_id = str(self.USER_ID)
        stored2.organization_id = self.ORG_ID
        new_stored2 = AsyncMock()
        new_stored2.id = uuid4()
        mock_repo.get_refresh_token_by_hash.side_effect = [stored2, new_stored2]
        with patch("services.auth_service.create_jwt_token", side_effect=_capture_jwt):
            await service.refresh("another-raw-token")
        assert captured["mcp"] is False
