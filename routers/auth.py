"""Dashboard authentication endpoints — HTTP adapter layer only.

Endpoints:
    POST /v1/auth/signup  — Create org + admin user, return JWT
    POST /v1/auth/login   — Authenticate by email/password, return JWT
    POST /v1/auth/refresh — Rotate refresh token, return new JWT pair
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from repositories.auth_repository import AuthRepository
from schemas.auth import LoginRequest, RefreshRequest, SignupRequest, TokenResponse
from services.auth_service import AuthService

router = APIRouter(
    prefix="/v1/auth",
    tags=["Authentication"],
)


def _get_auth_service(
    db: AsyncSession = Depends(get_db),
) -> AuthService:
    """Dependency factory for ``AuthService``."""
    return AuthService(repo=AuthRepository(db))


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=201,
    summary="Create organization and admin user",
    description=(
        "Registers a new organization with an admin dashboard user "
        "identified by email and password.  Returns a JWT access token "
        "and a refresh token for session management.  The access token "
        "expires in 30 minutes (default); the refresh token in 7 days."
    ),
)
async def signup(
    payload: SignupRequest,
    service: AuthService = Depends(_get_auth_service),
) -> TokenResponse:
    """Sign up a new organization with an admin dashboard user.

    Args:
        payload: Email, password, and organization name.
        service: Injected auth service.

    Returns:
        Access and refresh tokens.
    """
    return await service.signup(payload)


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
    service: AuthService = Depends(_get_auth_service),
) -> TokenResponse:
    """Log in a dashboard user.

    Args:
        payload: Email and password.
        service: Injected auth service.

    Returns:
        Access and refresh tokens.
    """
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
    service: AuthService = Depends(_get_auth_service),
) -> TokenResponse:
    """Refresh an expired access token.

    Args:
        payload: The current refresh token.
        service: Injected auth service.

    Returns:
        New access and refresh tokens.
    """
    return await service.refresh(payload.refresh_token)
