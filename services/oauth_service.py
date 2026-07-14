"""OAuth service — Google and GitHub OAuth 2.0 authentication flows.

Handles the Authorization Code flow for both providers:
1. Generate state token, store in Redis, return provider auth URL.
2. Handle callback: validate state, exchange code, fetch user profile.
3. Find existing OAuthAccount -> issue JWT, or create account + issue JWT.

Also handles account linking for already-authenticated dashboard users.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx

from core.config import get_settings
from core.exceptions import (
    AuthenticationError,
    NotFoundError,
    ValidationError,
)
from repositories.auth_repository import AuthRepository
from repositories.oauth_repository import OAuthRepository
from schemas.auth import TokenResponse
from schemas.oauth import OAuthAccountResponse, OAuthInitResponse
from utils.crypto import create_jwt_token

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

OAUTH_STATE_PREFIX: str = "oauth:state:"
"""Redis key prefix for OAuth state tokens."""

# ── Google OAuth endpoints ────────────────────────────────────────────────────

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_SCOPES = "openid email profile"

# ── GitHub OAuth endpoints ────────────────────────────────────────────────────

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"
GITHUB_SCOPES = "read:user user:email"

ALLOWED_PROVIDERS = frozenset({"google", "github"})
"""Set of OAuth providers this service supports."""


# ═══════════════════════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════════════════════


class OAuthService:
    """OAuth authentication and account linking for dashboard users.

    Implements the Authorization Code grant for Google and GitHub.
    State tokens are stored in Redis (single-use, TTL-bound) for CSRF
    protection.  New dashboard users are auto-provisioned with an
    organisation when they log in via OAuth for the first time.

    Args:
        oauth_repo: Repository for OAuthAccount CRUD.
        auth_repo: Repository for dashboard user lookup and creation.
        redis: Async Redis client for state token storage.
    """

    def __init__(
        self,
        oauth_repo: OAuthRepository,
        auth_repo: AuthRepository,
        redis: AsyncRedis,
    ) -> None:
        self._oauth_repo = oauth_repo
        self._auth_repo = auth_repo
        self._redis = redis

    # ── Public API ─────────────────────────────────────────────────────────

    async def initiate_login(self, provider: str) -> OAuthInitResponse:
        """Generate the OAuth provider authorization URL for login/signup.

        Creates a CSRF state token stored in Redis, constructs the
        provider's authorization URL, and returns both to the caller.
        The frontend redirects the browser to the returned URL.

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).

        Returns:
            An ``OAuthInitResponse`` with the redirect URL and state token.

        Raises:
            ValidationError: If the provider is not supported or OAuth
                is disabled via ``OAUTH_ENABLED``.
        """
        self._validate_provider(provider)
        self._check_enabled()

        state_token = secrets.token_urlsafe(32)
        redirect_uri = self._callback_url(provider)
        auth_url = self._build_auth_url(provider, state_token, redirect_uri)

        state_data: dict[str, Any] = {
            "provider": provider,
            "mode": "login_signup",
        }
        await self._store_state(state_token, state_data)

        logger.info(
            "oauth.login_initiated",
            extra={"provider": provider, "state_token_prefix": state_token[:8]},
        )

        return OAuthInitResponse(
            redirect_url=auth_url,
            state_token=state_token,
        )

    async def initiate_link(
        self,
        provider: str,
        user_id: str,
    ) -> OAuthInitResponse:
        """Generate the OAuth provider authorization URL for account linking.

        Like :meth:`initiate_login` but marks the state with
        ``mode="link"`` and the authenticated user's UUID so the callback
        knows to link the provider identity to the existing account rather
        than create a new user.

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).
            user_id: The authenticated dashboard user's UUID string.

        Returns:
            An ``OAuthInitResponse`` with the redirect URL and state token.

        Raises:
            ValidationError: If the provider is not supported or OAuth
                is disabled.
        """
        self._validate_provider(provider)
        self._check_enabled()

        state_token = secrets.token_urlsafe(32)
        redirect_uri = self._callback_url(provider)
        auth_url = self._build_auth_url(provider, state_token, redirect_uri)

        state_data: dict[str, Any] = {
            "provider": provider,
            "mode": "link",
            "user_id": user_id,
        }
        await self._store_state(state_token, state_data)

        logger.info(
            "oauth.link_initiated",
            extra={
                "provider": provider,
                "user_id": user_id,
                "state_token_prefix": state_token[:8],
            },
        )

        return OAuthInitResponse(
            redirect_url=auth_url,
            state_token=state_token,
        )

    async def handle_callback(
        self,
        provider: str,
        code: str,
        state_token: str,
    ) -> tuple[str, str]:
        """Handle the OAuth provider callback after user authorization.

        This is the core of the OAuth flow:

        1. Validate and consume the state token from Redis (single-use).
        2. Exchange the authorization code for an access token.
        3. Fetch the user's profile from the provider.
        4. Look up existing ``OAuthAccount`` or create a new user + account.

        When an existing OAuthAccount is found, the linked dashboard user
        is logged in directly.  When no account exists but a dashboard user
        with the same email is found, the OAuth account is auto-linked.  In
        all other cases a new organisation and dashboard user is created.

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).
            code: The authorization code from the provider callback.
            state_token: The CSRF state token (validated against Redis).

        Returns:
            A tuple of ``(access_token, refresh_token)`` strings.  The
            caller (typically a router) should redirect the browser to the
            frontend with these as URL query parameters.

        Raises:
            AuthenticationError: If the state token is invalid/expired,
                the provider mismatches, the linked account is deactivated,
                or the token exchange fails.
            ValidationError: If the provider is not supported.
        """
        self._validate_provider(provider)
        self._check_enabled()

        # ── 1. Validate and consume state token ─────────────────────────────
        state_data = await self._consume_state(state_token)
        if state_data is None:
            raise AuthenticationError(
                "OAuth state token is invalid or has expired. "
                "Please try logging in again."
            )

        stored_provider = state_data.get("provider")
        if stored_provider != provider:
            # ⚠️ SECURITY: Provider mismatch in stored state indicates
            # either a CSRF attack or a misconfigured client.
            logger.warning(
                "oauth.provider_mismatch",
                extra={
                    "expected": stored_provider,
                    "received": provider,
                    "state_token_prefix": state_token[:8],
                },
            )
            raise AuthenticationError(
                f"OAuth provider mismatch: expected '{stored_provider}', "
                f"got '{provider}'."
            )

        mode = state_data.get("mode", "login_signup")

        # ── 2. Exchange code for access token ──────────────────────────────
        redirect_uri = self._callback_url(provider)
        token_data = await self._exchange_code(provider, code, redirect_uri)
        access_token_str: str = token_data["access_token"]

        # ── 3. Fetch user profile from provider ────────────────────────────
        profile = await self._fetch_user_profile(provider, access_token_str)
        provider_user_id: str = str(profile["id"])
        email: str = profile.get("email") or ""
        name: str = (
            profile.get("name")
            or email.split("@")[0]
            if email
            else provider_user_id
        )

        # ── 4. Find or create the user ─────────────────────────────────────
        existing_link = await self._oauth_repo.find_by_provider(
            provider, provider_user_id
        )

        if existing_link is not None:
            # ── Existing OAuth account -> login existing user ──────────────
            user = await self._auth_repo.get_user_by_id(
                _parse_uuid(existing_link.user_id, "user_id")
            )
            if user is None or user.is_deleted:
                raise AuthenticationError(
                    "The linked dashboard account no longer exists "
                    "or has been deactivated."
                )
            if not user.is_active:
                raise AuthenticationError(
                    "This account has been deactivated."
                )

            logger.info(
                "oauth.existing_account_login",
                extra={
                    "provider": provider,
                    "user_id": str(user.id),
                    "provider_user_id": provider_user_id,
                },
            )

        elif mode == "link":
            # ── Linking mode — create the OAuth account link ─────────────
            user_id_from_state = state_data.get("user_id")
            if not user_id_from_state:
                raise AuthenticationError(
                    "Missing user identifier in OAuth state. "
                    "Please try linking your account again."
                )
            user = await self._auth_repo.get_user_by_id(
                _parse_uuid(user_id_from_state, "user_id")
            )
            if user is None or user.is_deleted:
                raise AuthenticationError(
                    "Your dashboard account no longer exists or has been "
                    "deactivated."
                )
            if not user.is_active:
                raise AuthenticationError(
                    "Your account has been deactivated."
                )

            await self._oauth_repo.create(
                provider, provider_user_id, user.id
            )
            logger.info(
                "oauth.account_linked",
                extra={
                    "provider": provider,
                    "user_id": user_id_from_state,
                    "provider_user_id": provider_user_id,
                },
            )

        else:
            # ── New user — try to find by email or create ──────────────────
            user = (
                await self._auth_repo.find_user_by_email(email)
                if email
                else None
            )

            if user is not None and not user.is_deleted and user.is_active:
                # Existing dashboard user with same email -> auto-link
                await self._oauth_repo.create(
                    provider, provider_user_id, user.id
                )
                logger.info(
                    "oauth.auto_linked_existing_user",
                    extra={
                        "provider": provider,
                        "user_id": str(user.id),
                        "email": email,
                    },
                )
            else:
                # No existing user -> create new org + dashboard user
                org = await self._auth_repo.create_organization(
                    name=f"{name}'s Organization",
                    plan="free",
                )
                # Seed default prompt templates for the new org
                await self._auth_repo.seed_prompts_for_org(org.id)

                # TechLead note: create_dashboard_user signature says
                # `password_hash: str` but the User model column is
                # `Mapped[str | None]`.  OAuth users have no password,
                # so we pass None.  The repository type annotation should
                # be updated to `str | None` for correctness.
                user = await self._auth_repo.create_dashboard_user(
                    organization_id=org.id,
                    email=email,
                    password_hash=None,  # type: ignore[arg-type]
                    name=name,
                    role="admin",
                )
                # Email is verified by the OAuth provider
                await self._auth_repo.mark_email_verified(user.id)
                # Create the OAuth link
                await self._oauth_repo.create(
                    provider, provider_user_id, user.id
                )

                logger.info(
                    "oauth.new_user_created",
                    extra={
                        "provider": provider,
                        "user_id": str(user.id),
                        "org_id": str(org.id),
                        "email": email,
                    },
                )

        # ── 5. Issue JWT tokens ────────────────────────────────────────────
        role = user.role if user.role is not None else "admin"
        tokens = await self._issue_tokens(
            user_id=user.id,
            organization_id=user.organization_id,
            role=role,
        )

        return tokens.access_token, tokens.refresh_token

    async def get_linked_accounts(
        self,
        user_id: str,
    ) -> list[OAuthAccountResponse]:
        """List all OAuth accounts linked to the authenticated user.

        Args:
            user_id: The authenticated dashboard user's UUID string.

        Returns:
            A list of ``OAuthAccountResponse`` objects, one per linked
            provider account.  May be empty.
        """
        parsed_user_id = _parse_uuid(user_id, "user_id")
        accounts: Sequence[Any] = await self._oauth_repo.find_by_user_id(
            parsed_user_id
        )
        return [
            OAuthAccountResponse(
                id=acct.id,
                provider=acct.provider,
                provider_user_id=acct.provider_user_id,
                created_at=acct.created_at,
            )
            for acct in accounts
        ]

    async def unlink_account(
        self,
        provider: str,
        user_id: str,
    ) -> None:
        """Unlink an OAuth account from the authenticated user.

        Removes the ``OAuthAccount`` record so the provider identity is
        no longer associated with the dashboard user.  The dashboard
        account itself is not affected.

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).
            user_id: The authenticated dashboard user's UUID string.

        Raises:
            NotFoundError: If no linked OAuth account exists for this
                user and provider combination.
            ValidationError: If the provider is not supported.
        """
        self._validate_provider(provider)

        parsed_user_id = _parse_uuid(user_id, "user_id")
        accounts = await self._oauth_repo.find_by_user_id(parsed_user_id)

        target = next(
            (acct for acct in accounts if acct.provider == provider),
            None,
        )
        if target is None:
            raise NotFoundError(
                f"No linked {provider} account found for this user."
            )

        await self._oauth_repo.delete(target.id)

        logger.info(
            "oauth.account_unlinked",
            extra={
                "provider": provider,
                "user_id": user_id,
                "oauth_account_id": str(target.id),
            },
        )

    # ── State management ───────────────────────────────────────────────────

    async def _store_state(
        self,
        state_token: str,
        data: dict[str, Any],
    ) -> None:
        """Store OAuth state data in Redis with a configurable TTL.

        The state token is consumed exactly once (see :meth:`_consume_state`).
        Redis TTL provides automatic expiry as a safety net.

        Args:
            state_token: The random CSRF state token string.
            data: JSON-serialisable data to associate with this state
                (provider, mode, optional user_id).
        """
        ttl = get_settings().OAUTH_STATE_TTL_SECONDS
        await self._redis.setex(
            f"{OAUTH_STATE_PREFIX}{state_token}",
            ttl,
            json.dumps(data),
        )

    async def _consume_state(
        self,
        state_token: str,
    ) -> dict[str, Any] | None:
        """Retrieve and delete an OAuth state from Redis (single-use).

        Deleting the key on read prevents replay attacks where the same
        state token is used twice.  If the key has already expired (TTL),
        this returns ``None``.

        Args:
            state_token: The CSRF state token to look up.

        Returns:
            The stored data dict, or ``None`` if the token does not exist
            or has already expired / been consumed.
        """
        key = f"{OAUTH_STATE_PREFIX}{state_token}"
        raw = await self._redis.get(key)
        if raw is None:
            return None
        await self._redis.delete(key)  # single-use — prevent replay
        return json.loads(raw)

    # ── Provider helpers ───────────────────────────────────────────────────

    def _build_auth_url(
        self,
        provider: str,
        state_token: str,
        redirect_uri: str,
    ) -> str:
        """Build the provider's OAuth authorization URL.

        Constructs the URL with the correct parameters for each provider's
        OAuth 2.0 implementation.

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).
            state_token: CSRF state token (embedded in the URL).
            redirect_uri: The callback URL registered with the provider.

        Returns:
            The full authorization URL to redirect the browser to.
        """
        settings = get_settings()

        if provider == "google":
            params = {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": GOOGLE_SCOPES,
                "state": state_token,
                "access_type": "online",
                "prompt": "select_account",
            }
            return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

        # GitHub
        params = {
            "client_id": settings.GITHUB_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": GITHUB_SCOPES,
            "state": state_token,
        }
        return f"{GITHUB_AUTH_URL}?{urlencode(params)}"

    def _callback_url(self, provider: str) -> str:
        """Build the full callback URL for the given provider.

        Derives the base URL from the first configured CORS origin.
        In production this must match the redirect URI registered in
        the OAuth provider's application settings.

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).

        Returns:
            The full callback URL, e.g.
            ``https://api.openzync.tech/v1/auth/oauth/google/callback``.
        """
        settings = get_settings()
        # Use the dedicated OAUTH_CALLBACK_BASE_URL when set (production).
        # Fall back to the first CORS origin for backward compatibility
        # (most common in development where API and frontend share a port).
        base = settings.OAUTH_CALLBACK_BASE_URL
        if not base:
            cors_origins = settings.CORS_ORIGINS.split(",")
            base = (
                cors_origins[0].rstrip("/")
                if cors_origins and cors_origins[0]
                else "http://localhost:8000"
            )
        return f"{base}/v1/auth/oauth/{provider}/callback"

    async def _exchange_code(
        self,
        provider: str,
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """Exchange an authorization code for an access token.

        Makes a server-to-server POST request to the provider's token
        endpoint.  The client secret is never exposed to the browser.

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).
            code: The authorization code from the provider callback.
            redirect_uri: The callback URL (must match the registered URI).

        Returns:
            The token response dict containing at minimum ``access_token``.

        Raises:
            AuthenticationError: If the token exchange fails (non-200
                status, provider error, or missing access_token).
        """
        settings = get_settings()

        if provider == "google":
            data: dict[str, str] = {
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
            url = GOOGLE_TOKEN_URL
        else:
            data = {
                "code": code,
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
            }
            url = GITHUB_TOKEN_URL

        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"Accept": "application/json"}
            response = await client.post(url, data=data, headers=headers)

            if response.status_code != 200:
                logger.error(
                    "oauth.token_exchange_failed",
                    extra={
                        "provider": provider,
                        "status": response.status_code,
                        "error": response.text[:500],
                    },
                )
                raise AuthenticationError(
                    f"Failed to exchange authorization code with "
                    f"{provider.title()}.  Please try again."
                )

            result = response.json()
            if "error" in result:
                logger.error(
                    "oauth.provider_error",
                    extra={
                        "provider": provider,
                        "error": result["error"],
                        "error_description": result.get(
                            "error_description", ""
                        ),
                    },
                )
                raise AuthenticationError(
                    f"{provider.title()} returned an error: "
                    f"{result['error']}.  "
                    f"{result.get('error_description', 'No details provided.')}"
                )

            access_token: str | None = result.get("access_token")
            if not access_token:
                raise AuthenticationError(
                    f"No access_token in {provider.title()} response."
                )

            return result

    async def _fetch_user_profile(
        self,
        provider: str,
        access_token: str,
    ) -> dict[str, Any]:
        """Fetch the user's profile from the OAuth provider.

        For GitHub, the public profile may not include the primary email
        address, so we make a secondary request to the user/emails endpoint
        to retrieve the verified primary email.

        Args:
            provider: OAuth provider name (``"google"`` or ``"github"``).
            access_token: The access token from token exchange.

        Returns:
            A dict with at minimum ``id``, ``email``, and ``name`` keys.
            ``id`` is guaranteed; ``email`` and ``name`` may be empty
            strings if the provider did not return them.
        """
        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            if provider == "google":
                response = await client.get(
                    GOOGLE_USERINFO_URL, headers=headers
                )
                response.raise_for_status()
                profile: dict[str, Any] = response.json()
                # Google returns 'id' as a string — use as-is.
                return profile

            # GitHub — fetch public user profile first
            headers["Accept"] = "application/vnd.github.v3+json"
            response = await client.get(GITHUB_USER_URL, headers=headers)
            response.raise_for_status()
            profile = response.json()

            # GitHub may not expose the email in the public profile if
            # the user has set it to private.  Fall back to the
            # /user/emails endpoint.
            if not profile.get("email"):
                try:
                    emails_resp = await client.get(
                        GITHUB_EMAILS_URL, headers=headers
                    )
                    emails_resp.raise_for_status()
                    emails: list[dict[str, Any]] = emails_resp.json()
                    primary = next(
                        (
                            e
                            for e in emails
                            if e.get("primary") and e.get("verified")
                        ),
                        None,
                    )
                    if primary:
                        profile["email"] = primary["email"]
                    elif emails:
                        # Fall back to the first email if none is marked
                        # as primary + verified.
                        profile["email"] = emails[0].get("email", "")
                except Exception:
                    logger.warning(
                        "oauth.github_email_fetch_failed",
                        extra={"error": "Failed to fetch GitHub emails"},
                    )

            return profile

    # ── Token issuance ─────────────────────────────────────────────────────

    async def _issue_tokens(
        self,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        role: str,
    ) -> TokenResponse:
        """Generate and persist an access + refresh token pair.

        Mirrors ``AuthService._issue_tokens`` so that OAuth login produces
        the exact same token format as email/password login.  The access
        token is a signed JWT; the refresh token is an opaque string stored
        as a SHA-256 hash in the database.

        Args:
            user_id: The dashboard user's UUID.
            organization_id: The user's organisation UUID.
            role: User role for JWT claims (``"admin"`` or ``"member"``).

        Returns:
            A ``TokenResponse`` with fresh access and refresh tokens.
        """
        settings = get_settings()
        # Use naive UTC datetime for DB storage (refresh_token.expires_at
        # is TIMESTAMP WITHOUT TIME ZONE).
        now = datetime.now(UTC).replace(tzinfo=None)

        access_ttl = timedelta(minutes=settings.JWT_ACCESS_TOKEN_TTL_MINUTES)
        refresh_ttl = timedelta(days=settings.JWT_REFRESH_TOKEN_TTL_DAYS)

        # Access token
        access_token = create_jwt_token(
            data={
                "sub": str(user_id),
                "org_id": str(organization_id),
                "role": role,
                "type": "access",
            },
            secret=settings.SECRET_KEY,
            expires_delta=access_ttl,
        )

        # Refresh token (opaque — stored as SHA-256 hash)
        raw_refresh = secrets.token_hex(32)
        refresh_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
        refresh_expires = now + refresh_ttl

        await self._auth_repo.create_refresh_token(
            user_id=user_id,
            organization_id=organization_id,
            token_hash=refresh_hash,
            expires_at=refresh_expires,
        )

        return TokenResponse(
            access_token=access_token,
            refresh_token=raw_refresh,
            expires_in=int(access_ttl.total_seconds()),
        )

    # ── Validation helpers ────────────────────────────────────────────────

    @staticmethod
    def _validate_provider(provider: str) -> None:
        """Validate that the provider is in the allowed set.

        Args:
            provider: OAuth provider name to validate.

        Raises:
            ValidationError: If the provider is not in
                :data:`ALLOWED_PROVIDERS`.
        """
        if provider not in ALLOWED_PROVIDERS:
            raise ValidationError(
                f"Unsupported OAuth provider '{provider}'.  "
                f"Supported providers: "
                f"{', '.join(sorted(ALLOWED_PROVIDERS))}."
            )

    @staticmethod
    def _check_enabled() -> None:
        """Check that OAuth is enabled in settings (kill switch).

        When ``OAUTH_ENABLED`` is ``False``, all OAuth endpoints are
        effectively disabled at the service layer.  The corresponding
        router can also return 404 for a cleaner UX.

        Raises:
            ValidationError: If ``OAUTH_ENABLED`` is ``False``.
        """
        if not get_settings().OAUTH_ENABLED:
            raise ValidationError(
                "OAuth login is currently disabled.  "
                "Contact your administrator for more information."
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Module-level helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _parse_uuid(value: str | uuid.UUID, field_name: str) -> uuid.UUID:
    """Validate and return a UUID object from a string or UUID.

    Accepts both ``str`` and ``uuid.UUID`` objects.  In Python 3.13+,
    ``uuid.UUID(uuid_obj)`` crashes because the constructor treats the
    argument as a hex string and calls ``.replace()`` on it.  This
    helper handles both types safely.

    Args:
        value: The UUID string or UUID object to validate.
        field_name: Human-readable field name for error messages
            (e.g. ``"user_id"``).

    Returns:
        The validated :class:`uuid.UUID` object.

    Raises:
        ValidationError: If ``value`` is not a valid UUID.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValidationError(
            f"Invalid UUID for '{field_name}': {value!r}"
        )
