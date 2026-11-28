"""Dashboard authentication endpoints — HTTP adapter layer only.

Endpoints:
    POST   /v1/auth/setup   — One-time platform root bootstrap (public)
    POST   /v1/auth/signup             — Member signup into the root org, send OTP
    POST   /v1/auth/verify-email       — Verify email with OTP, return JWT
    POST   /v1/auth/resend-otp         — Resend verification OTP
    POST   /v1/auth/login/otp/send     — Send passwordless login OTP
    POST   /v1/auth/login/otp/verify   — Verify login OTP, return JWT
    POST   /v1/auth/forgot-password    — Send password-reset OTP
    POST   /v1/auth/reset-password     — Reset password with OTP
    POST   /v1/auth/login              — Authenticate by email/password, return JWT/MFA
    POST   /v1/auth/mfa/verify         — Complete MFA login with OTP
    POST   /v1/auth/mfa/enable         — Enable MFA (requires JWT)
    POST   /v1/auth/mfa/disable        — Disable MFA (requires JWT + OTP)
    POST   /v1/auth/refresh            — Rotate refresh token, return new JWT pair
    POST   /v1/auth/logout             — Revoke refresh-token family, clear cookie
    GET    /v1/auth/me                 — Get current dashboard user profile
    PATCH  /v1/auth/me                 — Update profile name, email, or password
    POST   /v1/auth/invites/info       — Resolve invite details (public, token in body)
    POST   /v1/auth/invites/accept     — Claim invite, set password, return JWT pair
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from core.audit import audit_action
from core.config import get_settings
from core.exceptions import AuthenticationError, AuthorizationError, ConflictError
from core.net import is_trusted_proxy
from dependencies.auth import get_dashboard_user
from dependencies.services import (
    get_auth_service,
    get_auth_throttle,
    get_invite_service,
    get_setup_service,
)
from middleware.auth_throttle import AuthThrottle
from schemas.auth import (
    AcceptInviteRequest,
    ChangePasswordRequest,
    DashboardUserResponse,
    InviteInfoResponse,
    InviteTokenRequest,
    LoginRequest,
    LoginResponse,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaVerifyRequest,
    LogoutResponse,
    PendingOrgResponse,
    RefreshRequest,
    RegistrationStatusResponse,
    SetupRequest,
    SetupResponse,
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
from services.invite_service import InviteService
from services.setup_service import SetupService

router = APIRouter(
    prefix="/v1/auth",
    tags=["Authentication"],
)


def _client_ip(request: Request) -> str:
    """Extract the client IP address from the request.

    The leftmost ``X-Forwarded-For`` entry is trusted only when the
    direct peer is in ``TRUSTED_PROXY_IPS`` — otherwise any client
    could forge its own throttle bucket. Falls back to ``"unknown"``.
    ``get_settings()`` returns the process-wide singleton (no I/O after
    first call), so this stays cheap per request.
    """
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and is_trusted_proxy(peer, get_settings().TRUSTED_PROXY_IPS):
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return peer


def _token_fingerprint(raw: str) -> str:
    """Hash a bearer token for throttle keys (never log/slice raw tokens).

    Args:
        raw: The raw refresh or magic-link token.

    Returns:
        First 16 hex chars of the SHA-256 digest — stable per token,
        useless to an attacker who reads the throttle store.
    """
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


_REFRESH_COOKIE_NAME = "refresh_token"
_REFRESH_COOKIE_PATH = "/v1/auth"


def _set_refresh_cookie(response: Response, token: str) -> None:
    """Set the ``refresh_token`` httpOnly cookie on a token-issuing response.

    # ⚠️ BREAKING: every endpoint returning a token pair now also emits
    # ``Set-Cookie`` (dual transport — the JSON body token is kept for
    # SDK/MCP/demo clients). Browsers must rely on the cookie plus
    # ``credentials: include`` and never touch the token in JS state.

    Args:
        response: Outgoing response carrying the ``Set-Cookie`` header.
        token: Opaque refresh token to store in the cookie.
    """
    settings = get_settings()
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=token,
        httponly=True,
        path=_REFRESH_COOKIE_PATH,
        max_age=settings.JWT_REFRESH_TOKEN_TTL_DAYS * 86400,
        samesite="lax",
        secure=settings.ENVIRONMENT == "production",
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Clear the ``refresh_token`` cookie (logout).

    Args:
        response: Outgoing response carrying the deleting ``Set-Cookie``.
    """
    settings = get_settings()
    response.delete_cookie(
        key=_REFRESH_COOKIE_NAME,
        path=_REFRESH_COOKIE_PATH,
        samesite="lax",
        secure=settings.ENVIRONMENT == "production",
    )


@router.get(
    "/registration-status",
    response_model=RegistrationStatusResponse,
    summary="Public registration status",
    description=(
        "Returns the platform registration mode so the frontend can decide "
        "between signup, invite-only, and approval states. "
        "PUBLIC ENDPOINT — no auth required."
    ),
)
async def registration_status(
    service: SetupService = Depends(get_setup_service),  # noqa: B008
) -> RegistrationStatusResponse:
    """Return the platform registration mode for the signup UI.

    Args:
        service: Injected setup service (owns the settings-row read).

    Returns:
        The current ``registration_mode`` (defaults to ``invite_only``).
    """
    return RegistrationStatusResponse(
        registration_mode=await service.get_registration_mode()
    )


@router.post(
    "/setup",
    response_model=SetupResponse,
    status_code=201,
    summary="One-time platform setup",
    description=(
        "Creates the platform root admin with the operator-chosen "
        "password and names the singleton platform org.  Usable exactly "
        "once — after the platform is set up every call returns 403.  "
        "PUBLIC ENDPOINT — no auth required."
    ),
)
@audit_action("auth.setup", "user", "Platform root created")
async def setup_platform_root(
    payload: SetupRequest,
    request: Request,
    service: SetupService = Depends(get_setup_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> SetupResponse:
    """Create the platform root admin (one-time bootstrap).

    Args:
        payload: Operator-chosen root password + singleton org name.
        request: Incoming HTTP request (for IP extraction).
        service: Injected setup service.
        throttle: Injected auth throttle.

    Returns:
        Confirmation message — the operator logs in with username
        ``'root'`` afterwards.

    Raises:
        AuthorizationError: Generic 403 when the platform is already set
            up (deliberately indistinguishable from any other setup
            failure — the endpoint must not leak bootstrap state).
    """
    await throttle.check_signup_attempt(_client_ip(request))
    try:
        await service.create_root_once(payload.password, payload.org_name)
    except ConflictError as exc:
        raise AuthorizationError("Platform setup is not available.") from exc
    return SetupResponse(
        message="Platform root created. Log in with username 'root'."
    )


@router.post(
    "/signup",
    response_model=SignupResponse | PendingOrgResponse | TokenResponse,
    status_code=201,
    summary="Create dashboard user in the platform org",
    description=(
        "Registers a dashboard user in the singleton platform org, "
        "identified by email and password.  A verification code is sent "
        "to the user's email.  The user must call "
        "``POST /v1/auth/verify-email`` with the code to complete signup "
        "and receive JWT tokens.  Under the ``approval`` registration "
        "mode the response carries ``status='pending'`` instead, and under "
        "``invite_only`` signup returns 403."
    ),
)
@audit_action("auth.signup", "user", "User signed up")
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
    invites: InviteService = Depends(get_invite_service),  # noqa: B008
) -> SignupResponse | PendingOrgResponse | TokenResponse:
    """Sign up a dashboard user in the platform org.

    Args:
        payload: Email + password, plus an optional ``invite_token``.
        request: Incoming HTTP request (for IP extraction).
        response: Outgoing response carrying the ``Set-Cookie`` header.
        service: Injected auth service.
        throttle: Injected auth throttle.
        invites: Injected invite service (invite-token path only).

    Returns:
        Confirmation message (tokens via verify-email), a
        pending-approval response under the ``approval`` mode — or a JWT
        pair immediately when ``invite_token`` claims an admin invite.
    """
    await throttle.check_signup_attempt(_client_ip(request))
    if payload.invite_token:
        # Invite path: claim the magic-link invite (same semantics as
        # POST /v1/auth/invites/accept) — the token is the credential, so
        # no OTP round-trip is needed.
        await throttle.check_sensitive_attempt(
            "invite_accept",
            _token_fingerprint(payload.invite_token),
            _client_ip(request),
        )
        result = await invites.accept_invite(
            token=payload.invite_token,
            password=payload.password,
        )
        _set_refresh_cookie(response, result.refresh_token)
        return result
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
@audit_action("auth.verify_email", "user", "Email verified")
async def verify_email(
    payload: VerifyEmailRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> TokenResponse:
    """Verify a user's email address with the OTP code.

    Args:
        payload: Email and OTP code.
        request: Incoming HTTP request (for IP extraction).
        response: Outgoing response carrying the ``Set-Cookie`` header.
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Access and refresh tokens on successful verification.
    """
    await throttle.check_verify_attempt(payload.email, _client_ip(request))
    result = await service.verify_email(payload)
    _set_refresh_cookie(response, result.refresh_token)
    return result


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
@audit_action("auth.resend_otp", "user", "OTP resent")
async def resend_otp(
    payload: SendOtpRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> SignupResponse:
    """Resend the email verification code.

    Args:
        payload: Email address.
        request: Incoming HTTP request (for IP extraction).
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Confirmation message.
    """
    await throttle.check_sensitive_attempt("resend_otp", payload.email, _client_ip(request))
    return await service.resend_verification(payload.email)


@router.post(
    "/login/otp/send",
    response_model=OtpResponse,
    summary="Send passwordless login OTP",
    description=(
        "Sends a 6-digit verification code to the user's email for "
        "passwordless login.  The user enters this code at "
        "``POST /v1/auth/login/otp/verify`` to receive JWT tokens.  "
        "Returns the same response whether or not the account exists "
        "(prevents email enumeration)."
    ),
)
@audit_action("auth.otp_send", "session", "OTP sent")
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
@audit_action("auth.otp_verify", "session", "OTP verified")
async def verify_login_otp(
    payload: VerifyOtpRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> TokenResponse:
    """Verify a passwordless login OTP and receive JWT tokens.

    Args:
        payload: Email and OTP code.
        request: Incoming HTTP request (for IP extraction).
        response: Outgoing response carrying the ``Set-Cookie`` header.
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Access and refresh tokens.
    """
    await throttle.check_passwordless_verify(payload.email, _client_ip(request))
    result = await service.passwordless_login(payload)
    _set_refresh_cookie(response, result.refresh_token)
    return result


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
@audit_action("auth.forgot_password", "user", "Password reset requested")
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
@audit_action("auth.reset_password", "user", "Password reset")
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
    result = await service.reset_password(payload)
    # A successful reset clears the attempt counters so failed tries from
    # an attacker never permanently lock out the account owner.  Failed
    # attempts raise before this line and do not decrement.
    await throttle.record_reset_success(payload.email, _client_ip(request))
    return result


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Authenticate dashboard user",
    description=(
        "Authenticates a dashboard user by email and password.  "
        "If MFA is disabled, returns JWT tokens directly.  "
        "If MFA is enabled, returns ``requires_mfa=true`` with a "
        "``mfa_session_token`` — the client must then call "
        "``POST /v1/auth/mfa/verify`` with the OTP sent via email.  "
        "The user's email must be verified before login is allowed."
    ),
)
@audit_action("auth.login", "session", "User logged in")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> LoginResponse:
    """Log in a dashboard user.

    Args:
        payload: Email and password.
        request: Incoming HTTP request (for IP extraction).
        response: Outgoing response carrying the ``Set-Cookie`` header.
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Tokens (MFA off) or MFA challenge (MFA on).
    """
    await throttle.check_login_attempt(payload.email, _client_ip(request))
    result = await service.login(payload)
    # A password-only success clears the attempt counters so failed tries
    # from an attacker never permanently lock out the account owner.  The
    # MFA path is not "successful" yet — counters are cleared at MFA verify.
    if not result.requires_mfa:
        await throttle.record_login_success(payload.email, _client_ip(request))
        if result.refresh_token is not None:
            _set_refresh_cookie(response, result.refresh_token)
    return result


@router.post(
    "/mfa/verify",
    response_model=TokenResponse,
    summary="Complete MFA login with OTP",
    description=(
        "Second step of MFA-authenticated login.  Accepts the email, "
        "the 6-digit MFA code from email, and the ``mfa_session_token`` "
        "obtained from ``POST /v1/auth/login``.  On success, returns "
        "a JWT access token and refresh token pair."
    ),
)
@audit_action("auth.mfa_verify", "user", "MFA verified")
async def mfa_verify(
    payload: MfaVerifyRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> TokenResponse:
    """Complete MFA-authenticated login.

    Args:
        payload: Email, OTP code, and MFA session token.
        request: Incoming HTTP request (for IP extraction).
        response: Outgoing response carrying the ``Set-Cookie`` header.
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Access and refresh tokens.
    """
    await throttle.check_mfa_verify(payload.email, _client_ip(request))
    result = await service.mfa_verify(payload)
    # Full login is only successful once the MFA OTP verifies — clear the
    # password-attempt counters accumulated at the login step.
    await throttle.record_login_success(payload.email, _client_ip(request))
    _set_refresh_cookie(response, result.refresh_token)
    return result


@router.post(
    "/mfa/enable",
    response_model=OtpResponse,
    summary="Enable MFA for the current user",
    description=(
        "Enables email-based MFA for the authenticated user.  "
        "Requires the current password for re-authentication.  "
        "A confirmation email is sent to the user's registered email.  "
        "Requires JWT authentication."
    ),
)
@audit_action("auth.mfa_enable", "user", "MFA enabled")
async def mfa_enable(
    payload: MfaEnableRequest,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    user_id: str = Depends(get_dashboard_user),
) -> OtpResponse:
    """Enable MFA for the current dashboard user.

    Args:
        payload: Current password for re-authentication.
        service: Injected auth service.
        user_id: Authenticated user UUID from JWT claims.

    Returns:
        Confirmation message.
    """
    return await service.enable_mfa(user_id=UUID(user_id), payload=payload)


@router.post(
    "/mfa/disable",
    response_model=OtpResponse,
    summary="Disable MFA for the current user",
    description=(
        "Disables email-based MFA for the authenticated user.  "
        "Requires the current password AND an MFA OTP for verification.  "
        "To obtain an OTP, first call ``POST /v1/auth/login`` (which "
        "sends an MFA code if MFA is enabled).  "
        "Requires JWT authentication."
    ),
)
@audit_action("auth.mfa_disable", "user", "MFA disabled")
async def mfa_disable(
    payload: MfaDisableRequest,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    user_id: str = Depends(get_dashboard_user),
) -> OtpResponse:
    """Disable MFA for the current dashboard user.

    Args:
        payload: Current password and MFA OTP for verification.
        service: Injected auth service.
        user_id: Authenticated user UUID from JWT claims.

    Returns:
        Confirmation message.
    """
    return await service.disable_mfa(user_id=UUID(user_id), payload=payload)


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
@audit_action("auth.refresh", "token", "Token refreshed")
async def refresh(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> TokenResponse:
    """Refresh an expired access token.

    # ⚠️ BREAKING: ``payload.refresh_token`` is optional — when omitted
    # the ``refresh_token`` httpOnly cookie is used instead.

    Args:
        payload: Optional refresh token in the body.
        request: Incoming HTTP request (for IP extraction + cookie fallback).
        response: Outgoing response carrying the ``Set-Cookie`` header.
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        New access and refresh tokens.

    Raises:
        AuthenticationError: If neither body nor cookie carries a token.
    """
    raw_token = payload.refresh_token or request.cookies.get("refresh_token")
    if not raw_token:
        raise AuthenticationError("Refresh token is required.")
    await throttle.check_sensitive_attempt(
        "refresh", _token_fingerprint(raw_token), _client_ip(request)
    )
    result = await service.refresh(raw_token)
    _set_refresh_cookie(response, result.refresh_token)
    return result


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="Log out and revoke the refresh-token family",
    description=(
        "Revokes the refresh token from the request body or the "
        "``refresh_token`` httpOnly cookie (the whole rotation family), "
        "and clears the cookie via a deleting ``Set-Cookie``.  "
        "Idempotent — a missing or unknown token still returns success "
        "with the cookie cleared.  "
        "PUBLIC ENDPOINT — no auth required (the token IS the credential)."
    ),
)
@audit_action("auth.logout", "session", "User logged out")
async def logout(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> LogoutResponse:
    """Log out the current session.

    Args:
        payload: Optional refresh token in the body.
        request: Incoming HTTP request (for IP extraction + cookie fallback).
        response: Outgoing response carrying the clearing ``Set-Cookie``.
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Confirmation message.
    """
    raw_token = payload.refresh_token or request.cookies.get("refresh_token")
    await throttle.check_sensitive_attempt(
        "logout",
        _token_fingerprint(raw_token) if raw_token else "none",
        _client_ip(request),
    )
    if raw_token is not None:
        await service.logout(raw_token)
    _clear_refresh_cookie(response)
    return LogoutResponse(message="Logged out.")


@router.post(
    "/change-password",
    response_model=TokenResponse,
    summary="Change password and rotate the session",
    description=(
        "Verifies the current password, sets the new one, clears the "
        "must-change-password gate, revokes all existing refresh tokens, "
        "and returns a fresh token pair.  Exempt from the "
        "must-change-password gate — this is how the seeded root "
        "credential is replaced."
    ),
)
@audit_action("auth.password_changed", "user", "Password changed")
async def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
    user_id: str = Depends(get_dashboard_user),  # noqa: B008
) -> TokenResponse:
    """Change the current user's password.

    Args:
        payload: Current + new password.
        response: Outgoing response carrying the ``Set-Cookie`` header.
        service: Injected auth service.
        user_id: Authenticated user UUID from JWT claims.

    Returns:
        A fresh access + refresh token pair (all prior sessions revoked).
    """
    result = await service.change_password(
        user_id=UUID(user_id),
        payload=payload,
    )
    _set_refresh_cookie(response, result.refresh_token)
    return result


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
        The user's public profile (email, name, role, verification).
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
@audit_action("auth.profile.update", "user", "Profile updated")
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


@router.post(
    "/invites/info",
    response_model=InviteInfoResponse,
    summary="Resolve pending-invite details",
    description=(
        "Returns the invitee identity (email, name) for a valid "
        "magic-link token, shown on the invite landing page.  Unknown, "
        "expired, and used tokens all return the same generic 404.  "
        "PUBLIC ENDPOINT — no auth required (the token IS the credential)."
    ),
)
@audit_action("auth.invite_info", "user", "Invite info requested")
async def invite_info(
    payload: InviteTokenRequest,
    request: Request,
    service: InviteService = Depends(get_invite_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> InviteInfoResponse:
    """Resolve the invite details behind a magic-link token.

    The token arrives in the POST body — never the URL path — so the magic
    link (a live bearer credential) cannot leak into request logs.

    Args:
        payload: The raw magic-link token.
        request: Incoming HTTP request (for IP extraction).
        service: Invite service (injected).
        throttle: Injected auth throttle.

    Returns:
        The invitee's email and name.

    Raises:
        NotFoundError: Generic 404 for unknown/expired/used tokens.
    """
    await throttle.check_sensitive_attempt(
        "invite_info", _token_fingerprint(payload.token), _client_ip(request)
    )
    return await service.get_invite_info(payload.token)


@router.post(
    "/invites/accept",
    response_model=TokenResponse,
    summary="Accept an invite and log in",
    description=(
        "Claims the pending invite atomically, sets the invitee's password, "
        "and returns a JWT pair — the invitee is authenticated immediately.  "
        "PUBLIC ENDPOINT — no auth required (the token IS the credential)."
    ),
)
@audit_action("auth.invite_accept", "user", "Invite accepted")
async def accept_invite(
    payload: AcceptInviteRequest,
    request: Request,
    response: Response,
    service: InviteService = Depends(get_invite_service),  # noqa: B008
    throttle: AuthThrottle = Depends(get_auth_throttle),  # noqa: B008
) -> TokenResponse:
    """Accept a pending invite and return a fresh token pair.

    The token arrives in the POST body — never the URL path — for the same
    log-leak reason as ``/invites/info``.  The response is the standard
    ``TokenResponse`` (JWT pair) and is excluded from audit body capture.

    Args:
        payload: Raw magic-link token + chosen password.
        request: Incoming HTTP request (for IP extraction).
        response: Outgoing response carrying the ``Set-Cookie`` header.
        service: Invite service (injected).
        throttle: Injected auth throttle.

    Returns:
        A fresh access + refresh token pair.

    Raises:
        NotFoundError: Generic 404 for unknown/expired/used tokens.
        ValidationError: If the password is too weak (→ 422).
    """
    await throttle.check_sensitive_attempt(
        "invite_accept", _token_fingerprint(payload.token), _client_ip(request)
    )
    result = await service.accept_invite(
        token=payload.token,
        password=payload.password,
    )
    _set_refresh_cookie(response, result.refresh_token)
    return result

