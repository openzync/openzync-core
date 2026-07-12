"""Dashboard authentication endpoints — HTTP adapter layer only.

Endpoints:
    POST   /v1/auth/signup             — Create org + admin user, send OTP
    POST   /v1/auth/verify-email       — Verify email with OTP, return JWT
    POST   /v1/auth/resend-otp         — Resend verification OTP
    POST   /v1/auth/login/otp/send     — Send passwordless login OTP
    POST   /v1/auth/login/otp/verify   — Verify login OTP, return JWT
    POST   /v1/auth/forgot-password    — Send password-reset OTP
    POST   /v1/auth/reset-password     — Reset password with OTP
    POST   /v1/auth/login              — Authenticate by email/password, return JWT
    POST   /v1/auth/refresh            — Rotate refresh token, return new JWT pair
    GET    /v1/auth/me                 — Get current dashboard user profile
    PATCH  /v1/auth/me                 — Update profile name, email, or password
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from dependencies.auth import get_dashboard_user
from dependencies.services import get_auth_service, get_auth_throttle
from middleware.auth_throttle import AuthThrottle
from schemas.auth import (
    DashboardUserResponse,
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    SignupResponse,
    TokenResponse,
    UpdateProfileRequest,
    VerifyEmailRequest,
)
from schemas.email import (
    OtpResponse,
    ResetPasswordRequest,
    SendOtpRequest,
    VerifyOtpRequest,
)
from services.auth_service import AuthService

router = APIRouter(
    prefix="/v1/auth",
    tags=["Authentication"],
)


def _client_ip(request: Request) -> str:
    """Extract the client IP address from the request.

    Respects the ``X-Forwarded-For`` header for reverse-proxy deployments.
    Falls back to ``request.client.host``.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=201,
    summary="Create organization and admin user",
    description=(
        "Registers a new organization with an admin dashboard user "
        "identified by email and password.  A verification code is sent "
        "to the user's email.  The user must call ``POST /v1/auth/verify-email`` "
        "with the code to complete signup and receive JWT tokens."
    ),
)
async def signup(
    payload: SignupRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> SignupResponse:
    """Sign up a new organization with an admin dashboard user.

    Args:
        payload: Email, password, and organization name.
        request: Incoming HTTP request (for IP extraction).
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Confirmation message (tokens obtained via verify-email).
    """
    await throttle.check_signup_attempt(_client_ip(request))
    return await service.signup(payload)


@router.post(
    "/verify-email",
    response_model=TokenResponse,
    summary="Verify email with OTP and receive JWT tokens",
    description=(
        "Accepts the email address and the 6-digit OTP sent during signup. "
        "On success, marks the email as verified and returns a JWT access "
        "token and refresh token pair.  Rate-limited to prevent brute-force."
    ),
)
async def verify_email(
    payload: VerifyEmailRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> TokenResponse:
    """Verify a user's email address with the OTP code.

    Args:
        payload: Email and OTP code.
        request: Incoming HTTP request (for IP extraction).
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Access and refresh tokens on successful verification.
    """
    await throttle.check_verify_attempt(payload.email, _client_ip(request))
    return await service.verify_email(payload)


@router.post(
    "/resend-otp",
    response_model=SignupResponse,
    summary="Resend the email verification OTP",
    description=(
        "Resends the 6-digit verification code to the user's email. "
        "Rate-limited: at most one request per 60s per email, and "
        "at most 5 sends per hour."
    ),
)
async def resend_otp(
    payload: SendOtpRequest,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> SignupResponse:
    """Resend the email verification code.

    Args:
        payload: Email address.
        service: Injected auth service.

    Returns:
        Confirmation message.
    """
    return await service.resend_verification(payload.email)


@router.post(
    "/login/otp/send",
    response_model=OtpResponse,
    summary="Send passwordless login OTP",
    description=(
        "Sends a 6-digit verification code to the user's email for "
        "passwordless login.  The user enters this code at "
        "``POST /v1/auth/login/otp/verify`` to receive JWT tokens.  "
        "Requires an existing user account."
    ),
)
async def send_login_otp(
    payload: SendOtpRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> OtpResponse:
    """Send a passwordless login OTP to the user's email.

    Args:
        payload: Email address to send the login code to.
        request: Incoming HTTP request (for IP extraction).
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Confirmation message.
    """
    await throttle.check_passwordless_send(payload.email, _client_ip(request))
    return await service.generate_login_otp(payload.email)


@router.post(
    "/login/otp/verify",
    response_model=TokenResponse,
    summary="Verify login OTP and receive JWT tokens",
    description=(
        "Accepts the email address and the 6-digit OTP sent for "
        "passwordless login.  On success, returns a JWT access token "
        "and refresh token pair.  The user's email is auto-verified "
        "if this is their first login.  Rate-limited to prevent "
        "brute-force OTP guessing."
    ),
)
async def verify_login_otp(
    payload: VerifyOtpRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> TokenResponse:
    """Verify a passwordless login OTP and receive JWT tokens.

    Args:
        payload: Email and OTP code.
        request: Incoming HTTP request (for IP extraction).
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Access and refresh tokens.
    """
    await throttle.check_passwordless_verify(payload.email, _client_ip(request))
    return await service.passwordless_login(payload)


@router.post(
    "/forgot-password",
    response_model=OtpResponse,
    summary="Send password-reset OTP",
    description=(
        "Sends a 6-digit verification code to the user's email for "
        "password reset.  Returns the same message whether or not the "
        "email exists (prevents email enumeration).  Rate-limited: "
        "at most 3 requests per email per hour."
    ),
)
async def forgot_password(
    payload: SendOtpRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> OtpResponse:
    """Send a password-reset OTP to the user's email.

    Args:
        payload: Email address to send the reset code to.
        request: Incoming HTTP request (for IP extraction).
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Confirmation message (same for existing and non-existing accounts).
    """
    await throttle.check_forgot_password_attempt(payload.email, _client_ip(request))
    return await service.forgot_password(payload.email)


@router.post(
    "/reset-password",
    response_model=OtpResponse,
    summary="Reset password with OTP",
    description=(
        "Accepts the email, the 6-digit OTP received via email, and a "
        "new password.  On success, the password is updated and all "
        "existing sessions are invalidated (the user must log in again).  "
        "Rate-limited to prevent brute-force OTP guessing."
    ),
)
async def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> OtpResponse:
    """Reset a user's password using an OTP-verified request.

    Args:
        payload: Email, OTP code, and new password.
        request: Incoming HTTP request (for IP extraction).
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Confirmation message and forces re-login.
    """
    await throttle.check_reset_attempt(payload.email, _client_ip(request))
    return await service.reset_password(payload)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate dashboard user",
    description=(
        "Authenticates a dashboard user by email and password.  "
        "Returns a JWT access token and a refresh token.  The user's "
        "email must be verified before login is allowed.  "
        "The access token is valid for 30 minutes (default); use the "
        "refresh token at ``POST /v1/auth/refresh`` to obtain a new pair."
    ),
)
async def login(
    payload: LoginRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> TokenResponse:
    """Log in a dashboard user.

    Args:
        payload: Email and password.
        request: Incoming HTTP request (for IP extraction).
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Access and refresh tokens.
    """
    await throttle.check_login_attempt(payload.email, _client_ip(request))
    return await service.login(payload)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate refresh token",
    description=(
        "Accepts a valid refresh token and returns a new access + refresh "
        "token pair.  The previous refresh token is revoked (rotation).  "
        "Refresh tokens are valid for 7 days by default."
    ),
)
async def refresh(
    payload: RefreshRequest,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> TokenResponse:
    """Refresh an expired access token.

    Args:
        payload: The current refresh token.
        service: Injected auth service.

    Returns:
        New access and refresh tokens.
    """
    return await service.refresh(payload.refresh_token)


@router.get(
    "/me",
    response_model=DashboardUserResponse,
    summary="Get current dashboard user",
    description=(
        "Returns the profile of the currently authenticated dashboard user. "
        "Requires a JWT access token (dashboard session)."
    ),
)
async def get_profile(
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    user_id: str = Depends(get_dashboard_user),
) -> DashboardUserResponse:
    """Get the current dashboard user's profile.

    Args:
        service: Injected auth service.
        user_id: Authenticated user UUID from JWT claims.

    Returns:
        The user's public profile (email, name, role, org, verification).
    """
    return await service.get_profile(user_id=UUID(user_id))


@router.patch(
    "/me",
    response_model=DashboardUserResponse,
    summary="Update dashboard user profile",
    description=(
        "Update the current user's name, email, or password. "
        "All fields are optional — only provided fields are updated. "
        "To change the password, provide both ``current_password`` and "
        "``new_password``."
    ),
)
async def update_profile(
    payload: UpdateProfileRequest,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    user_id: str = Depends(get_dashboard_user),
) -> DashboardUserResponse:
    """Update the current dashboard user's profile.

    Args:
        payload: Fields to update.
        service: Injected auth service.
        user_id: Authenticated user UUID from JWT claims.

    Returns:
        Updated user profile.
    """
    return await service.update_profile(
        user_id=UUID(user_id),
        payload=payload,
    )
