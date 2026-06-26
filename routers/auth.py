"""Dashboard authentication endpoints — HTTP adapter layer only.

Endpoints:
    POST   /v1/auth/signup             — Create org + admin user, return JWT
    POST   /v1/auth/login              — Authenticate by email/password, return JWT
    POST   /v1/auth/refresh            — Rotate refresh token, return new JWT pair
    GET    /v1/auth/verify             — Verify email via token
    POST   /v1/auth/resend-verification — Resend verification email
    GET    /v1/auth/me                 — Get current dashboard user profile
    PATCH  /v1/auth/me                 — Update profile name, email, or password
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from dependencies.auth import get_dashboard_user
from dependencies.services import get_auth_service, get_auth_throttle
from middleware.auth_throttle import AuthThrottle
from schemas.auth import (
    DashboardUserResponse,
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UpdateProfileRequest,
)
from services.auth_service import AuthService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/auth",
    tags=["Authentication"],
)


def _arq_queue_name(queue_type: str) -> str:
    """Build the full ARQ queue name matching the worker's config.

    Worker uses ``get_queue_name(settings.ENV, queue_type)`` which produces
    ``OpenZep:{env}:queue:{queue_type}``.  We replicate the same logic here
    so that jobs arrive in the correct queue.

    Args:
        queue_type: Queue type suffix (e.g. ``"high"``, ``"low"``).

    Returns:
        Fully qualified queue name for the current environment.
    """
    from core.config import settings as core_settings

    env = getattr(core_settings, "ENVIRONMENT", "development")
    return f"OpenZep:{env}:queue:{queue_type}"


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


# ═══════════════════════════════════════════════════════════════════════════════
# Signup
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=201,
    summary="Create organization and admin user",
    description=(
        "Registers a new organization with an admin dashboard user "
        "identified by email and password.  Returns a JWT access token "
        "and a refresh token for session management.  A verification "
        "email is sent to the user asynchronously."
    ),
)
async def signup(
    payload: SignupRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
    throttle: AuthThrottle = Depends(get_auth_throttle),
) -> TokenResponse:
    """Sign up a new organization with an admin dashboard user.

    Args:
        payload: Email, password, and organization name.
        request: Incoming HTTP request (for IP extraction + ARQ pool).
        service: Injected auth service.
        throttle: Injected auth throttle.

    Returns:
        Access and refresh tokens.
    """
    await throttle.check_signup_attempt(_client_ip(request))

    result = await service.signup(payload)

    # Enqueue verification email (best-effort — never block signup)
    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is not None:
        try:
            await arq_pool.enqueue(
                "send_verification_email",
                queue_name=_arq_queue_name("low"),
                email=result.email,
                token=result.verification_token,
                org_name=payload.organization_name,
            )
        except Exception:
            logger.exception(
                "signup.enqueue_verification_email_failed",
                extra={"email": result.email},
            )
    else:
        logger.warning("signup.arq_pool_not_available")

    return result.tokens


# ═══════════════════════════════════════════════════════════════════════════════
# Login
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate dashboard user",
    description=(
        "Authenticates a dashboard user by email and password.  Returns "
        "a JWT access token and a refresh token.  The access token is "
        "valid for 30 minutes (default); use the refresh token at "
        "``POST /v1/auth/refresh`` to obtain a new pair."
    ),
)
async def login(
    payload: LoginRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
    throttle: AuthThrottle = Depends(get_auth_throttle),
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
    throttle_result = await throttle.check_login_attempt(
        payload.email, _client_ip(request)
    )
    # TODO(me): use throttle_result["captcha_required"] for CAPTCHA in P2.3
    return await service.login(payload)


# ═══════════════════════════════════════════════════════════════════════════════
# Refresh
# ═══════════════════════════════════════════════════════════════════════════════


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
    service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """Refresh an expired access token.

    Args:
        payload: The current refresh token.
        service: Injected auth service.

    Returns:
        New access and refresh tokens.
    """
    return await service.refresh(payload.refresh_token)


# ═══════════════════════════════════════════════════════════════════════════════
# Email verification
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/verify",
    summary="Verify email address",
    description=(
        "Verifies the user's email address using a token sent via email. "
        "The token is valid for 24 hours."
    ),
)
async def verify_email(
    token: str = Query(..., description="Verification token from email link."),
    service: AuthService = Depends(get_auth_service),
) -> dict:
    """Verify the user's email address.

    Args:
        token: The raw verification token from the email link.
        service: Injected auth service.

    Returns:
        A success message.
    """
    await service.verify_email(token)
    return {"detail": "Email verified successfully."}


@router.post(
    "/resend-verification",
    summary="Resend verification email",
    description=(
        "Resends the verification email to the currently authenticated "
        "user's email address.  Generates a new token, invalidating the "
        "previous one.  Rate-limited to once per 60 seconds."
    ),
)
async def resend_verification(
    request: Request,
    service: AuthService = Depends(get_auth_service),
    user_id: str = Depends(get_dashboard_user),
) -> dict:
    """Resend the verification email.

    Args:
        request: Incoming HTTP request (for ARQ pool access).
        service: Injected auth service.
        user_id: Authenticated user UUID from JWT claims.

    Returns:
        A success message.
    """
    user_uuid = UUID(user_id)

    # Get user profile for email + org name
    profile = await service.get_profile(user_uuid)

    if profile.email_verified:
        raise HTTPException(status_code=400, detail="Email is already verified.")

    # Generate a fresh token via the public service method
    new_token = await service.resend_verification(user_uuid)

    # Enqueue the email
    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        logger.warning("resend_verification.arq_pool_not_available")
        raise HTTPException(status_code=503, detail="Email service not available.")

    try:
        await arq_pool.enqueue(
            "send_verification_email",
            queue_name=_arq_queue_name("low"),
            email=profile.email,
            token=new_token,
            org_name=profile.email.split("@")[0],  # fallback org name
        )
    except Exception:
        logger.exception(
            "resend_verification.enqueue_failed",
            extra={"user_id": user_id},
        )
        raise HTTPException(status_code=503, detail="Failed to send verification email.")

    return {"detail": "Verification email sent."}


# ═══════════════════════════════════════════════════════════════════════════════
# Profile
# ═══════════════════════════════════════════════════════════════════════════════


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
    service: AuthService = Depends(get_auth_service),
    user_id: str = Depends(get_dashboard_user),
) -> DashboardUserResponse:
    """Get the current dashboard user's profile.

    Args:
        service: Injected auth service.
        user_id: Authenticated user UUID from JWT claims.

    Returns:
        The user's public profile (email, name, role, org).
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
    service: AuthService = Depends(get_auth_service),
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
