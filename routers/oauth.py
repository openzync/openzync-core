"""OAuth authentication endpoints — HTTP adapter layer only.

Endpoints:
    GET    /v1/auth/oauth/{provider}/login      — Redirect browser to OAuth provider
    GET    /v1/auth/oauth/{provider}/callback   — Handle provider callback, return JWT
    POST   /v1/auth/oauth/{provider}/link       — Initiate account linking (JWT required)
    POST   /v1/auth/oauth/unlink                — Remove linked account (JWT required)
    GET    /v1/auth/oauth/accounts              — List linked accounts (JWT required)
"""

from __future__ import annotations

from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_307_TEMPORARY_REDIRECT

from core.config import get_settings
from core.exceptions import (
    AppError,
    AuthenticationError,
    NotFoundError,
    ValidationError,
)
from dependencies.auth import get_dashboard_user
from dependencies.services import get_oauth_service
from schemas.oauth import (
    OAuthAccountResponse,
    OAuthInitResponse,
    OAuthLinkRequest,
    OAuthUnlinkRequest,
)
from services.oauth_service import OAuthService

router = APIRouter(
    prefix="/v1/auth/oauth",
    tags=["OAuth Authentication"],
)


def _frontend_base_url() -> str:
    """Derive the frontend base URL from the CORS origins setting.

    The first CORS origin is assumed to be the frontend URL for redirects
    after OAuth callback.
    """
    settings = get_settings()
    origins = settings.CORS_ORIGINS.split(",")
    return (origins[0].rstrip("/")) if origins else "http://localhost:3000"


def _redirect_with_tokens(
    access_token: str,
    refresh_token: str,
    expires_in: int,
) -> RedirectResponse:
    """Redirect the browser to the frontend with JWT tokens in URL params.

    Args:
        access_token: The JWT access token.
        refresh_token: The opaque refresh token.
        expires_in: Access token TTL in seconds.

    Returns:
        A 307 redirect to ``{frontend}/auth/callback?access_token=...``.
    """
    frontend = _frontend_base_url()
    params = urlencode({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "token_type": "Bearer",
    })
    redirect = f"{frontend}/auth/callback?{params}"
    return RedirectResponse(url=redirect, status_code=HTTP_307_TEMPORARY_REDIRECT)


def _redirect_with_error(error: str, description: str = "") -> RedirectResponse:
    """Redirect the browser to the frontend with an OAuth error.

    Args:
        error: A machine-readable error code.
        description: A human-readable error description.

    Returns:
        A 307 redirect to ``{frontend}/auth/error?error=...``.
    """
    frontend = _frontend_base_url()
    params: dict[str, str] = {"error": error}
    if description:
        params["description"] = description
    return RedirectResponse(
        url=f"{frontend}/auth/error?{urlencode(params)}",
        status_code=HTTP_307_TEMPORARY_REDIRECT,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# OAuth login endpoints (public — no auth required)
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/{provider}/login",
    response_model=OAuthInitResponse,
    summary="Initiate OAuth login with a third-party provider",
    description=(
        "Returns a redirect URL to the OAuth provider's authorization page. "
        "The browser should be redirected to this URL.  After the user "
        "authorizes, the provider redirects back to the callback endpoint.  "
        "Supported providers: ``google``, ``github``."
    ),
    responses={
        307: {"description": "Redirect to OAuth provider"},
        422: {"description": "Unsupported provider or OAuth disabled"},
    },
)
async def oauth_login(
    provider: str,
    service: OAuthService = Depends(get_oauth_service),
) -> OAuthInitResponse:
    """Initiate an OAuth login/signup flow.

    Args:
        provider: OAuth provider name (``"google"`` or ``"github"``).
        service: Injected OAuth service.

    Returns:
        An ``OAuthInitResponse`` with the ``redirect_url``.

    Raises:
        HTTPException: 422 if the provider is unsupported or OAuth is disabled.
    """
    try:
        return await service.initiate_login(provider)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get(
    "/{provider}/callback",
    summary="Handle OAuth provider callback",
    description=(
        "Callback URL that the OAuth provider redirects to after the user "
        "authorizes.  Validates the state parameter, exchanges the "
        "authorization code for an access token, fetches the user profile, "
        "and either logs the user in or creates a new account.  The browser "
        "is redirected to the frontend with JWT tokens as URL query parameters "
        "on success, or an error on failure."
    ),
    responses={
        307: {"description": "Redirect to frontend with tokens or error"},
    },
)
async def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Handle the OAuth provider's callback after user authorization.

    Args:
        provider: OAuth provider name (``"google"`` or ``"github"``).
        request: Incoming HTTP request.
        code: The authorization code from the provider.
        state: The CSRF state token for validation.
        error: OAuth error code if the user denied the request.

    Returns:
        A redirect to the frontend with tokens or an error message.
    """
    # Read from request.query_params as fallback for route-binding issues
    if code is None:
        code = request.query_params.get("code")
    if state is None:
        state = request.query_params.get("state")
    if error is None:
        error = request.query_params.get("error")

    # User denied the authorization request
    if error is not None:
        return _redirect_with_error(
            error=error,
            description="The OAuth authorization was denied or failed.",
        )

    # Missing required params
    if not code or not state:
        return _redirect_with_error(
            error="invalid_request",
            description="Missing authorization code or state parameter.",
        )

    # Handle callback within a properly managed DB session
    settings = get_settings()
    result = await _handle_oauth_callback(
        request=request,
        provider=provider,
        code=code,
        state=state,
    )

    if isinstance(result, RedirectResponse):
        return result

    access_token, refresh_token = result
    return _redirect_with_tokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.JWT_ACCESS_TOKEN_TTL_MINUTES * 60,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Account linking endpoints (JWT required)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post(
    "/{provider}/link",
    response_model=OAuthInitResponse,
    summary="Link an OAuth account to the current user",
    description=(
        "Initiates the OAuth account-linking flow for an already-authenticated "
        "dashboard user.  The frontend should redirect the browser to the "
        "returned ``redirect_url``.  After the user authorizes, the callback "
        "links the OAuth identity to the current user instead of creating a "
        "new account.  Requires a valid JWT token."
    ),
)
async def oauth_link(
    provider: str,
    service: OAuthService = Depends(get_oauth_service),
    user_id: str = Depends(get_dashboard_user),
) -> OAuthInitResponse:
    """Initiate OAuth account linking for the authenticated user.

    Args:
        provider: OAuth provider name (``"google"`` or ``"github"``).
        service: Injected OAuth service.
        user_id: Authenticated dashboard user UUID from JWT claims.

    Returns:
        An ``OAuthInitResponse`` with the redirect URL.
    """
    try:
        return await service.initiate_link(provider=provider, user_id=user_id)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post(
    "/unlink",
    status_code=204,
    summary="Unlink an OAuth account",
    description=(
        "Removes a linked OAuth provider account from the authenticated "
        "dashboard user.  The user will no longer be able to log in using "
        "that OAuth provider.  Requires a valid JWT token."
    ),
)
async def oauth_unlink(
    payload: OAuthUnlinkRequest,
    service: OAuthService = Depends(get_oauth_service),
    user_id: str = Depends(get_dashboard_user),
) -> None:
    """Unlink an OAuth account from the current user.

    Args:
        payload: Provider name to unlink.
        service: Injected OAuth service.
        user_id: Authenticated dashboard user UUID from JWT claims.

    Raises:
        HTTPException: 404 if no linked account exists for this provider.
    """
    try:
        await service.unlink_account(provider=payload.provider, user_id=user_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get(
    "/accounts",
    response_model=list[OAuthAccountResponse],
    summary="List linked OAuth accounts",
    description=(
        "Returns all OAuth provider accounts linked to the currently "
        "authenticated dashboard user.  Requires a valid JWT token."
    ),
)
async def oauth_accounts(
    service: OAuthService = Depends(get_oauth_service),
    user_id: str = Depends(get_dashboard_user),
) -> list[OAuthAccountResponse]:
    """List all OAuth accounts linked to the current user.

    Args:
        service: Injected OAuth service.
        user_id: Authenticated dashboard user UUID from JWT claims.

    Returns:
        A list of ``OAuthAccountResponse`` objects.
    """
    return await service.get_linked_accounts(user_id=user_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════


async def _handle_oauth_callback(
    request: Request,
    provider: str,
    code: str,
    state: str,
) -> tuple[str, str] | RedirectResponse:
    """Handle the OAuth callback within a properly managed DB session.

    Creates a DB session, wires up the OAuthService, calls
    ``handle_callback``, and returns the tokens.  The session is
    properly closed after the callback completes.

    Args:
        request: The incoming HTTP request.
        provider: OAuth provider name.
        code: The authorization code from the provider.
        state: The CSRF state token.

    Returns:
        A tuple of ``(access_token, refresh_token)`` on success,
        or a ``RedirectResponse`` with an error on failure.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from repositories.auth_repository import AuthRepository
    from repositories.oauth_repository import OAuthRepository

    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:
        raise RuntimeError("db_session_factory not configured on app.state")

    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise RuntimeError("Redis not configured on app.state")

    async with factory() as session:
        oauth_service = OAuthService(
            oauth_repo=OAuthRepository(session),
            auth_repo=AuthRepository(session),
            redis=redis,
        )
        try:
            return await oauth_service.handle_callback(
                provider=provider,
                code=code,
                state_token=state,
            )
        except (AuthenticationError, ValidationError) as exc:
            return _redirect_with_error(
                error="authentication_error",
                description=str(exc),
            )
    # Session is safely closed here — no further DB access possible.
