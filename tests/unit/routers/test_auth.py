"""Unit tests for the authentication router.

Tests all ``/v1/auth/...`` endpoints — signup, login, MFA, tokens, profile.
"""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from dependencies.auth import get_dashboard_user
from dependencies.services import get_auth_service, get_auth_throttle
from routers.auth import router
from schemas.auth import (
    ChangePasswordRequest,
    DashboardUserResponse,
    LoginResponse,
    PendingOrgResponse,
    SignupResponse,
    TokenResponse,
)
from schemas.email import OtpResponse

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


def _create_app() -> tuple[FastAPI, dict[str, AsyncMock]]:
    """Build a minimal FastAPI app with the auth router and overridden deps."""
    app = FastAPI()
    mocks: dict[str, AsyncMock] = {}

    # Auth middleware that sets request.state
    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = str(USER_ID)
        request.state.auth_type = "jwt"
        request.state.api_key_scopes = ["admin", "admin:write"]
        response = await call_next(request)
        return response

    # Mock services
    mocks["auth_service"] = AsyncMock()
    mocks["auth_throttle"] = AsyncMock()

    app.dependency_overrides[get_auth_service] = lambda: mocks["auth_service"]
    app.dependency_overrides[get_auth_throttle] = lambda: mocks["auth_throttle"]
    app.dependency_overrides[get_dashboard_user] = lambda: str(USER_ID)

    app.include_router(router)
    return app, mocks


# ── Signup ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_success() -> None:
    """POST /v1/auth/signup returns 201 with confirmation message."""
    app, mocks = _create_app()
    mocks["auth_service"].signup.return_value = SignupResponse(
        message="Verification code sent to email",
        email="admin@acme.com",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/signup",
            json={
                "email": "admin@acme.com",
                "password": "secure-p@ssword-123",
                "organization_name": "Acme Corp",
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["message"] == "Verification code sent to email"
    assert body["email"] == "admin@acme.com"
    mocks["auth_throttle"].check_signup_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_signup_422() -> None:
    """POST /v1/auth/signup returns 422 when payload is invalid."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    # Missing required fields
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/auth/signup", json={})

    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body


@pytest.mark.asyncio
async def test_signup_invalid_email_422() -> None:
    """POST /v1/auth/signup returns 422 when email is malformed."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/signup",
            json={
                "email": "not-an-email",
                "password": "secure-p@ssword-123",
                "organization_name": "Acme Corp",
            },
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_signup_short_password_422() -> None:
    """POST /v1/auth/signup returns 422 when password is too short."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/signup",
            json={
                "email": "admin@acme.com",
                "password": "short",
                "organization_name": "Acme Corp",
            },
        )

    assert resp.status_code == 422


# ── Org-code join ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_join_success() -> None:
    """POST /v1/auth/join returns 201 with confirmation message."""
    from core.exceptions import register_exception_handlers

    app, mocks = _create_app()
    register_exception_handlers(app)
    mocks["auth_service"].join_organization.return_value = SignupResponse(
        message="Verification code sent to email. "
        "Use POST /v1/auth/verify-email to complete signup.",
        email="alice@acme.com",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/join",
            json={
                "email": "alice@acme.com",
                "password": "SecurePass1",
                "org_code": "K7M2Q9X4",
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "alice@acme.com"
    mocks["auth_service"].join_organization.assert_awaited_once()
    # Join is throttled like signup — per-IP signup attempt counter.
    mocks["auth_throttle"].check_signup_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_invalid_org_code_422_problem_json() -> None:
    """POST /v1/auth/join with an unknown org code → 422 problem+json.

    Observed contract: ``{"type":".../validation_error","title":"Validation
    Error","status":422,"detail":"Invalid organization code"}`` — NOT a 400.
    """
    from core.exceptions import ValidationError, register_exception_handlers

    app, mocks = _create_app()
    register_exception_handlers(app)
    mocks["auth_service"].join_organization.side_effect = ValidationError(
        "Invalid organization code"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/join",
            json={
                "email": "alice@acme.com",
                "password": "SecurePass1",
                "org_code": "K7M2Q9X4",
            },
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["type"] == "https://errors.openzync.tech/validation_error"
    assert body["title"] == "Validation Error"
    assert body["status"] == 422
    assert body["detail"] == "Invalid organization code"


@pytest.mark.asyncio
async def test_join_missing_field_422() -> None:
    """POST /v1/auth/join missing org_code → 422 Pydantic detail array."""
    app, mocks = _create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/join",
            json={"email": "alice@acme.com", "password": "SecurePass1"},
        )

    assert resp.status_code == 422
    body = resp.json()
    assert isinstance(body["detail"], list)  # Pydantic validation array
    mocks["auth_service"].join_organization.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_throttled_429() -> None:
    """POST /v1/auth/join over the signup throttle → 429 problem+json."""
    from core.exceptions import RateLimitError, register_exception_handlers

    app, mocks = _create_app()
    register_exception_handlers(app)
    mocks["auth_throttle"].check_signup_attempt.side_effect = RateLimitError(
        "Too many signup attempts from this IP address. Try again later."
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/join",
            json={
                "email": "alice@acme.com",
                "password": "SecurePass1",
                "org_code": "K7M2Q9X4",
            },
        )

    assert resp.status_code == 429
    body = resp.json()
    assert body["type"] == "https://errors.openzync.tech/rate_limit_exceeded"
    assert body["status"] == 429
    mocks["auth_service"].join_organization.assert_not_awaited()


# ── Email verification ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_email_success() -> None:
    """POST /v1/auth/verify-email returns 200 with token pair."""
    app, mocks = _create_app()
    mocks["auth_service"].verify_email.return_value = TokenResponse(
        access_token="at_abc123",
        refresh_token="rt_def456",
        expires_in=1800,
        token_type="Bearer",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/verify-email",
            json={"email": "admin@acme.com", "otp": "483926"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "at_abc123"
    assert body["refresh_token"] == "rt_def456"
    assert body["expires_in"] == 1800
    assert body["token_type"] == "Bearer"
    mocks["auth_throttle"].check_verify_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_email_422() -> None:
    """POST /v1/auth/verify-email returns 422 when OTP is missing."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/verify-email", json={"email": "admin@acme.com"}
        )

    assert resp.status_code == 422


# ── Resend OTP ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resend_otp_success() -> None:
    """POST /v1/auth/resend-otp returns 200 with confirmation."""
    app, mocks = _create_app()
    mocks["auth_service"].resend_verification.return_value = SignupResponse(
        message="Verification code resent",
        email="admin@acme.com",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/resend-otp",
            json={"email": "admin@acme.com"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Verification code resent"


# ── Login ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_success() -> None:
    """POST /v1/auth/login returns 200 with tokens (MFA disabled)."""
    app, mocks = _create_app()
    mocks["auth_service"].login.return_value = LoginResponse(
        access_token="at_abc123",
        refresh_token="rt_def456",
        expires_in=1800,
        token_type="Bearer",
        requires_mfa=False,
        mfa_session_token=None,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login",
            json={"email": "admin@acme.com", "password": "secure-p@ssword-123"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "at_abc123"
    assert body["requires_mfa"] is False
    mocks["auth_throttle"].check_login_attempt.assert_awaited_once()
    # Password-only success clears the attempt counters (H4d).
    mocks["auth_throttle"].record_login_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_root_user_success() -> None:
    """POST /v1/auth/login with email='root' returns 200 (seeded root user).

    The root credential is literally ``login using root`` — the schema
    admits the non-email identifier and the flow proceeds like any user.
    """
    app, mocks = _create_app()
    mocks["auth_service"].login.return_value = LoginResponse(
        access_token="at_root123",  # noqa: S106 — test fixture token
        refresh_token="rt_root456",  # noqa: S106 — test fixture token
        expires_in=1800,
        token_type="Bearer",  # noqa: S106 — test fixture token
        requires_mfa=False,
        mfa_session_token=None,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login",
            json={"email": "root", "password": "admin"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"] == "at_root123"  # noqa: S105 — test fixture
    # The schema normalized the identifier before the service saw it.
    mocks["auth_service"].login.assert_awaited_once()
    sent_payload = mocks["auth_service"].login.call_args.args[0]
    assert sent_payload.email == "root"


@pytest.mark.asyncio
async def test_login_root_at_422() -> None:
    """POST /v1/auth/login with email='root@' still fails schema validation.

    Only the literal ``root`` bypasses the email check — a malformed
    address is 422 exactly as before.
    """
    app, mocks = _create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login",
            json={"email": "root@", "password": "admin"},
        )

    assert resp.status_code == 422
    mocks["auth_service"].login.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_success_mfa_no_decrement() -> None:
    """POST /v1/auth/login with MFA enabled must NOT clear counters yet —
    success only counts once the MFA OTP verifies."""
    app, mocks = _create_app()
    mocks["auth_service"].login.return_value = LoginResponse(
        access_token=None,
        refresh_token=None,
        expires_in=None,
        token_type=None,
        requires_mfa=True,
        mfa_session_token="mfa_session_xyz",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login",
            json={"email": "admin@acme.com", "password": "secure-p@ssword-123"},
        )

    assert resp.status_code == 200
    mocks["auth_throttle"].record_login_success.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_mfa_requires_mfa() -> None:
    """POST /v1/auth/login returns 200 with requires_mfa=true when MFA is on."""
    app, mocks = _create_app()
    mocks["auth_service"].login.return_value = LoginResponse(
        access_token=None,
        refresh_token=None,
        expires_in=None,
        token_type=None,
        requires_mfa=True,
        mfa_session_token="mfa_session_xyz",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login",
            json={"email": "admin@acme.com", "password": "secure-p@ssword-123"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["requires_mfa"] is True
    assert body["mfa_session_token"] == "mfa_session_xyz"
    assert body["access_token"] is None


@pytest.mark.asyncio
async def test_login_422() -> None:
    """POST /v1/auth/login returns 422 when password is missing."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login", json={"email": "admin@acme.com"}
        )

    assert resp.status_code == 422


# ── Login OTP ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_otp_send_success() -> None:
    """POST /v1/auth/login/otp/send returns 200 with confirmation."""
    app, mocks = _create_app()
    mocks["auth_service"].generate_login_otp.return_value = OtpResponse(
        message="OTP sent to email"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login/otp/send",
            json={"email": "admin@acme.com"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "OTP sent to email"
    mocks["auth_throttle"].check_passwordless_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_otp_verify_success() -> None:
    """POST /v1/auth/login/otp/verify returns 200 with tokens."""
    app, mocks = _create_app()
    mocks["auth_service"].passwordless_login.return_value = TokenResponse(
        access_token="at_abc123",
        refresh_token="rt_def456",
        expires_in=1800,
        token_type="Bearer",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/login/otp/verify",
            json={"email": "admin@acme.com", "otp": "483926"},
        )

    assert resp.status_code == 200
    assert resp.json()["access_token"] == "at_abc123"
    mocks["auth_throttle"].check_passwordless_verify.assert_awaited_once()


# ── Forgot / Reset password ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forgot_password_success() -> None:
    """POST /v1/auth/forgot-password returns 200."""
    app, mocks = _create_app()
    mocks["auth_service"].forgot_password.return_value = OtpResponse(
        message="Password reset code sent to email"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/forgot-password",
            json={"email": "admin@acme.com"},
        )

    assert resp.status_code == 200
    assert resp.json()["message"] == "Password reset code sent to email"
    mocks["auth_throttle"].check_forgot_password_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_password_success() -> None:
    """POST /v1/auth/reset-password returns 200."""
    app, mocks = _create_app()
    mocks["auth_service"].reset_password.return_value = OtpResponse(
        message="Password has been reset"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/reset-password",
            json={
                "email": "admin@acme.com",
                "otp": "483926",
                "new_password": "new-secure-p@ssword-456",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["message"] == "Password has been reset"
    mocks["auth_throttle"].check_reset_attempt.assert_awaited_once()
    # Success clears the reset attempt counters (H4d).
    mocks["auth_throttle"].record_reset_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_password_failure_no_decrement() -> None:
    """POST /v1/auth/reset-password with a bad OTP must NOT clear counters —
    the service raises before the success decrement runs."""
    from core.exceptions import AuthenticationError, register_exception_handlers

    app, mocks = _create_app()
    register_exception_handlers(app)
    mocks["auth_service"].reset_password.side_effect = AuthenticationError(
        "Invalid or expired reset code. Please request a new code."
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/reset-password",
            json={
                "email": "admin@acme.com",
                "otp": "000000",
                "new_password": "new-secure-p@ssword-456",
            },
        )

    assert resp.status_code == 401
    mocks["auth_throttle"].check_reset_attempt.assert_awaited_once()
    mocks["auth_throttle"].record_reset_success.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_password_422() -> None:
    """POST /v1/auth/reset-password returns 422 when new_password is missing."""
    app, _ = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/reset-password",
            json={"email": "admin@acme.com", "otp": "483926"},
        )

    assert resp.status_code == 422


# ── MFA ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mfa_verify_success() -> None:
    """POST /v1/auth/mfa/verify returns 200 with tokens."""
    app, mocks = _create_app()
    mocks["auth_service"].mfa_verify.return_value = TokenResponse(
        access_token="at_abc123",
        refresh_token="rt_def456",
        expires_in=1800,
        token_type="Bearer",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/mfa/verify",
            json={
                "email": "admin@acme.com",
                "otp": "483926",
                "mfa_session_token": "mfa_session_xyz",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["access_token"] == "at_abc123"
    mocks["auth_throttle"].check_mfa_verify.assert_awaited_once()
    # Full login succeeds only once MFA verifies — clear login counters (H4d).
    mocks["auth_throttle"].record_login_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_mfa_enable_success() -> None:
    """POST /v1/auth/mfa/enable returns 200."""
    app, mocks = _create_app()
    mocks["auth_service"].enable_mfa.return_value = OtpResponse(
        message="MFA has been enabled"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/mfa/enable",
            json={"password": "my-current-password"},
        )

    assert resp.status_code == 200
    assert resp.json()["message"] == "MFA has been enabled"
    mocks["auth_service"].enable_mfa.assert_awaited_once_with(
        user_id=USER_ID,
        payload=mocks["auth_service"].enable_mfa.call_args[1]["payload"],
    )


@pytest.mark.asyncio
async def test_mfa_disable_success() -> None:
    """POST /v1/auth/mfa/disable returns 200."""
    app, mocks = _create_app()
    mocks["auth_service"].disable_mfa.return_value = OtpResponse(
        message="MFA has been disabled"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/mfa/disable",
            json={"password": "my-current-password", "otp": "483926"},
        )

    assert resp.status_code == 200
    assert resp.json()["message"] == "MFA has been disabled"


# ── Refresh ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_success() -> None:
    """POST /v1/auth/refresh returns 200 with new token pair."""
    app, mocks = _create_app()
    mocks["auth_service"].refresh.return_value = TokenResponse(
        access_token="at_new123",
        refresh_token="rt_new456",
        expires_in=1800,
        token_type="Bearer",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/refresh",
            json={"refresh_token": "rt_def456"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "at_new123"
    assert body["refresh_token"] == "rt_new456"

    mocks["auth_service"].refresh.assert_awaited_once_with("rt_def456")


# ── Profile ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_me_success() -> None:
    """GET /v1/auth/me returns 200 with the dashboard user profile."""
    app, mocks = _create_app()
    mocks["auth_service"].get_profile.return_value = DashboardUserResponse(
        id=USER_ID,
        email="admin@acme.com",
        name="Admin User",
        role="admin",
        organization_id=ORG_ID,
        is_email_verified=True,
        mfa_enabled=False,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/auth/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "admin@acme.com"
    assert body["name"] == "Admin User"
    assert body["role"] == "admin"
    assert body["is_email_verified"] is True
    assert body["mfa_enabled"] is False
    assert body["locale"] == "en"  # additive field — legacy fields intact
    mocks["auth_service"].get_profile.assert_awaited_once_with(user_id=USER_ID)


@pytest.mark.asyncio
async def test_update_me_success() -> None:
    """PATCH /v1/auth/me returns 200 with updated profile."""
    app, mocks = _create_app()
    mocks["auth_service"].update_profile.return_value = DashboardUserResponse(
        id=USER_ID,
        email="updated@acme.com",
        name="Updated User",
        role="admin",
        organization_id=ORG_ID,
        is_email_verified=True,
        mfa_enabled=False,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/v1/auth/me",
            json={"name": "Updated User"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Updated User"
    assert body["email"] == "updated@acme.com"
    mocks["auth_service"].update_profile.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_me_locale_success() -> None:
    """PATCH /v1/auth/me with a supported locale → 200, locale echoed."""
    app, mocks = _create_app()
    mocks["auth_service"].update_profile.return_value = DashboardUserResponse(
        id=USER_ID,
        email="admin@acme.com",
        name="Admin User",
        role="admin",
        organization_id=ORG_ID,
        is_email_verified=True,
        mfa_enabled=False,
        locale="en",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch("/v1/auth/me", json={"locale": "en"})

    assert resp.status_code == 200
    assert resp.json()["locale"] == "en"
    # Self-update only — user_id always comes from the JWT session, never the body.
    mocks["auth_service"].update_profile.assert_awaited_once_with(
        user_id=USER_ID, payload=ANY
    )
    sent_payload = mocks["auth_service"].update_profile.call_args.kwargs["payload"]
    assert sent_payload.locale == "en"


@pytest.mark.asyncio
async def test_update_me_invalid_locale_422() -> None:
    """PATCH /v1/auth/me with an unsupported locale → 422, exact message.

    Observed contract: the Pydantic field_validator rejects the tag before
    the service is reached — detail is the validation array whose ``msg``
    carries the canonical ``Unsupported locale 'xx'. Supported: en.`` text.
    """
    app, mocks = _create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch("/v1/auth/me", json={"locale": "xx"})

    assert resp.status_code == 422
    body = resp.json()
    assert isinstance(body["detail"], list)
    # The canonical message is the observed contract; Pydantic v2 prepends
    # its own "Value error, " prefix to the raw validator message.
    assert body["detail"][0]["msg"].endswith("Unsupported locale 'xx'. Supported: en.")
    mocks["auth_service"].update_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_me_unauthorized() -> None:
    """GET /v1/auth/me returns 401 when get_dashboard_user raises.

    Simulates a user that is not JWT-authenticated by overriding the dep
    to raise an HTTPException.
    """
    from fastapi import HTTPException

    app = FastAPI()
    mocks: dict[str, AsyncMock] = {}

    @app.middleware("http")
    async def _mock_auth(request, call_next):
        request.state.org_id = str(ORG_ID)
        request.state.user_id = None  # No user ID — not a JWT session
        request.state.auth_type = "api_key"
        response = await call_next(request)
        return response

    mocks["auth_service"] = AsyncMock()
    app.dependency_overrides[get_auth_service] = lambda: mocks["auth_service"]
    # Override get_dashboard_user to raise 401
    app.dependency_overrides[get_dashboard_user] = lambda: (
        _raise_401()
    )
    app.include_router(router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/auth/me")

    assert resp.status_code == 401


def _raise_401():
    from fastapi import HTTPException, status

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


# ── Signup — pending-approval path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_pending_approval_201() -> None:
    """Org-approval signup → 201 with flat {status, message}, no email key."""
    app, mocks = _create_app()
    mocks["auth_service"].signup.return_value = PendingOrgResponse(
        status="pending",
        message="Your organization is pending approval.",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/signup",
            json={
                "email": "admin@acme.com",
                "password": "secure-p@ssword-123",
                "organization_name": "Acme Corp",
            },
        )

    assert resp.status_code == 201
    assert resp.json() == {
        "status": "pending",
        "message": "Your organization is pending approval.",
    }


@pytest.mark.asyncio
async def test_signup_reserved_system_name_422() -> None:
    """Organization name 'SYSTEM' → 422 with the loud reserved-name detail."""
    app, mocks = _create_app()
    from core.exceptions import ValidationError, register_exception_handlers

    register_exception_handlers(app)
    mocks["auth_service"].signup.side_effect = ValidationError(
        "Organization name 'SYSTEM' is reserved and cannot be used."
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/signup",
            json={
                "email": "admin@acme.com",
                "password": "secure-p@ssword-123",
                "organization_name": "SYSTEM",
            },
        )

    assert resp.status_code == 422
    assert "reserved" in resp.json()["detail"]


# ── Change password ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_password_success_200() -> None:
    """POST /v1/auth/change-password → 200 with fresh tokens."""
    app, mocks = _create_app()
    mocks["auth_service"].change_password.return_value = TokenResponse(
        access_token="at.new",
        refresh_token="rt.new",
        expires_in=1800,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/auth/change-password",
            json={
                "old_password": "OldPass1",
                "new_password": "NewPass1",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "at.new"
    assert body["refresh_token"] == "rt.new"
    mocks["auth_service"].change_password.assert_awaited_once_with(
        user_id=USER_ID,
        payload=ChangePasswordRequest(
            old_password="OldPass1",
            new_password="NewPass1",
        ),
    )


# ── Registration status (public) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_registration_status_200_flat_defaults() -> None:
    """GET /v1/auth/registration-status → flat two-field body (defaults)."""
    app, _ = _create_app()
    from schemas.system_config import SystemConfigResponse

    with patch("routers.auth.get_system_config") as mock_get:
        mock_get.return_value = SystemConfigResponse()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/auth/registration-status")

    assert resp.status_code == 200
    assert resp.json() == {
        "org_creation_policy": "allow_all",
        "approval_scope": "both",
    }


@pytest.mark.asyncio
async def test_registration_status_200_flat_approvals() -> None:
    """Approvals policy → flat body mirrors the stored config."""
    app, _ = _create_app()
    from schemas.system_config import SystemConfigResponse

    with patch("routers.auth.get_system_config") as mock_get:
        mock_get.return_value = SystemConfigResponse(
            org_creation_policy="approvals",
            approval_scope="in_app",
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/auth/registration-status")

    assert resp.status_code == 200
    assert resp.json() == {
        "org_creation_policy": "approvals",
        "approval_scope": "in_app",
    }
