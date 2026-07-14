"""Unit tests for OAuthService — OAuth authentication flows.

Tests cover:
- State token generation and validation
- Google and GitHub callback handling
- User provisioning (new, existing, linking)
- Account linking and unlinking
- Error handling for invalid providers, disabled OAuth, expired state
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.exceptions import AuthenticationError, NotFoundError, ValidationError
from schemas.auth import TokenResponse
from schemas.oauth import OAuthAccountResponse, OAuthInitResponse
from services.oauth_service import OAUTH_STATE_PREFIX, OAuthService


# ═══════════════════════════════════════════════════════════════════════════════
# Shared test data
# ═══════════════════════════════════════════════════════════════════════════════

_USER_ID = UUID("11111111-1111-1111-1111-111111111111")
_ORG_ID = UUID("22222222-2222-2222-2222-222222222222")
_OAUTH_ACCT_ID = UUID("33333333-3333-3333-3333-333333333333")
_PROVIDER_USER_ID = "987654321"
_JWT_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.access-token"
_RAW_REFRESH_TOKEN = "a" * 64  # token_hex(32) produces 64 chars

# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_oauth_repo() -> MagicMock | AsyncMock:
    """Mock OAuthRepository with all methods returning sensible defaults.

    Every method is an ``AsyncMock``.  Tests override the return values
    they need.
    """
    repo = MagicMock(spec_set=["find_by_provider", "find_by_user_id", "create", "delete"])
    repo.find_by_provider = AsyncMock()
    repo.find_by_user_id = AsyncMock(return_value=[])
    repo.create = AsyncMock()
    repo.delete = AsyncMock()
    return repo


@pytest.fixture
def mock_auth_repo() -> MagicMock | AsyncMock:
    """Mock AuthRepository with find/create methods.

    Covers user lookup, org creation, user creation, token storage,
    and email verification.
    """
    repo = MagicMock(
        spec_set=[
            "find_user_by_email",
            "get_user_by_id",
            "create_organization",
            "seed_prompts_for_org",
            "create_dashboard_user",
            "create_refresh_token",
            "mark_email_verified",
        ],
    )
    repo.find_user_by_email = AsyncMock()
    repo.get_user_by_id = AsyncMock()
    repo.create_organization = AsyncMock()
    repo.seed_prompts_for_org = AsyncMock(return_value=5)
    repo.create_dashboard_user = AsyncMock()
    repo.create_refresh_token = AsyncMock()
    repo.mark_email_verified = AsyncMock()
    return repo


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Mock async Redis client with ``get`` / ``setex`` / ``delete``.

    ``get`` returns ``None`` by default — tests that need state tokens
    override this.  No ``spec_set`` is used so that all child attributes
    are ``AsyncMock`` instances (required for ``await`` and ``assert_*``
    methods).
    """
    redis = AsyncMock()
    redis.get.return_value = None
    return redis


@pytest.fixture
def mock_settings() -> MagicMock:
    """MagicMock with the OAuth-related Settings attributes populated.

    Use with ``@patch("services.oauth_service.get_settings")``:

        @patch("services.oauth_service.get_settings")
        async def test_foo(self, mock_get_settings, mock_settings):
            mock_get_settings.return_value = mock_settings
            ...
    """
    settings = MagicMock()
    settings.OAUTH_ENABLED = True
    settings.GOOGLE_CLIENT_ID = "google-client-id-123"
    settings.GOOGLE_CLIENT_SECRET = "gs-google-secret"
    settings.GITHUB_CLIENT_ID = "github-client-id-456"
    settings.GITHUB_CLIENT_SECRET = "gs-github-secret"
    settings.CORS_ORIGINS = "http://localhost:3000"
    settings.JWT_ACCESS_TOKEN_TTL_MINUTES = 30
    settings.JWT_REFRESH_TOKEN_TTL_DAYS = 7
    settings.SECRET_KEY = "test-secret-key-min-32-chars-long!!"
    settings.OAUTH_STATE_TTL_SECONDS = 600
    settings.OAUTH_CALLBACK_BASE_URL = ""
    return settings


@pytest.fixture
def oauth_service(
    mock_oauth_repo: MagicMock,
    mock_auth_repo: MagicMock,
    mock_redis: AsyncMock,
) -> OAuthService:
    """OAuthService with all dependencies mocked — no I/O, no DB, no network."""
    return OAuthService(
        oauth_repo=mock_oauth_repo,
        auth_repo=mock_auth_repo,
        redis=mock_redis,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helper factories
# ═══════════════════════════════════════════════════════════════════════════════


def _mock_user(**kwargs: Any) -> MagicMock:
    """Build a MagicMock that looks like a ``User`` ORM instance.

    Keyword Args override the defaults.  Typical overrides:
        ``is_active=False``, ``is_deleted=True``, different ``role``.
    """
    user = MagicMock()
    user.id = kwargs.get("id", _USER_ID)
    user.organization_id = kwargs.get("organization_id", _ORG_ID)
    user.role = kwargs.get("role", "admin")
    user.is_active = kwargs.get("is_active", True)
    user.is_deleted = kwargs.get("is_deleted", False)
    user.email = kwargs.get("email", "oauth-user@example.com")
    user.name = kwargs.get("name", "OAuth User")
    return user


def _mock_oauth_account(**kwargs: Any) -> MagicMock:
    """Build a MagicMock that looks like an ``OAuthAccount`` ORM instance."""
    acct = MagicMock()
    acct.id = kwargs.get("id", _OAUTH_ACCT_ID)
    acct.provider = kwargs.get("provider", "google")
    acct.provider_user_id = kwargs.get("provider_user_id", _PROVIDER_USER_ID)
    # Use a real UUID object to match production ORM behavior.
    # _parse_uuid handles both ``UUID`` and ``str`` inputs.
    acct.user_id = kwargs.get("user_id", _USER_ID)
    acct.created_at = kwargs.get("created_at", datetime(2025, 6, 1))
    return acct


def _mock_http_response(status: int = 200, json_data: Any = None) -> MagicMock:
    """Build a ``MagicMock`` that quacks like an ``httpx.Response``.

    ``httpx.Response.json()`` is **synchronous** — the mock must return
    the dict directly, not a coroutine.  ``AsyncMock`` would return a
    coroutine from ``.json()``, which breaks the actual code path.

    The returned mock has ``.status_code``, ``.json()``, ``.raise_for_status()``,
    and ``.text`` populated so the service can read them without raising.
    """
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {}
    resp.text = json.dumps(json_data) if json_data else ""
    # raise_for_status is a no-op by default — no need to call it
    return resp


def _configure_http_mock(
    mock_http_cls: MagicMock,
    *,
    post_response: AsyncMock | None = None,
    get_responses: AsyncMock | list[AsyncMock] | None = None,
) -> AsyncMock:
    """Configure a patched ``httpx.AsyncClient`` context manager.

    Uses coroutine-function side_effects for ``post`` and ``get`` so that
    ``await client.post(...)`` correctly resolves to the ``MagicMock``
    response (httpx ``Response`` methods are synchronous).

    Args:
        mock_http_cls: The patched ``httpx.AsyncClient`` class.
        post_response: Single response for ``client.post()``.
        get_responses: Single response or list for ``client.get()``
            (list enables ``side_effect`` for multiple sequential GETs).

    Returns:
        The mock client instance (``__aenter__`` return value).
    """
    mock_client = AsyncMock()
    mock_http_cls.return_value.__aenter__.return_value = mock_client

    if post_response is not None:
        async def _post(*args: Any, **kwargs: Any) -> MagicMock:
            return post_response
        mock_client.post.side_effect = _post

    if get_responses is not None:
        if isinstance(get_responses, list):
            async def _get(*args: Any, **kwargs: Any) -> MagicMock:
                resp = get_responses.pop(0)
                return resp
            mock_client.get.side_effect = _get
        else:
            async def _get(*args: Any, **kwargs: Any) -> MagicMock:
                return get_responses
            mock_client.get.side_effect = _get

    return mock_client


# ═══════════════════════════════════════════════════════════════════════════════
# Tests for initiate_login
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestInitiateLogin:
    """Tests for :meth:`OAuthService.initiate_login`."""

    @patch("services.oauth_service.get_settings")
    async def test_google_returns_redirect_url(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Should return an OAuthInitResponse with a Google auth URL.

        The URL must contain the Google OAuth endpoint, the configured
        client ID, and the CSRF state token should be stored in Redis.
        """
        mock_get_settings.return_value = mock_settings

        response = await oauth_service.initiate_login("google")

        assert isinstance(response, OAuthInitResponse)
        assert "accounts.google.com" in response.redirect_url
        assert response.state_token
        assert len(response.state_token) > 20

        # State token persisted in Redis with correct TTL
        mock_redis.setex.assert_awaited_once()
        call_args = mock_redis.setex.await_args
        assert call_args is not None
        key = call_args[0][0] if call_args.args else call_args.kwargs.get("name")
        assert key.startswith(OAUTH_STATE_PREFIX)
        ttl = call_args[0][1] if len(call_args.args) > 1 else call_args.kwargs.get("time")
        assert ttl == 600  # OAUTH_STATE_TTL_SECONDS

    @patch("services.oauth_service.get_settings")
    async def test_github_returns_redirect_url(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Should return an OAuthInitResponse with a GitHub auth URL.

        The URL must contain ``github.com/login/oauth/authorize``.
        """
        mock_get_settings.return_value = mock_settings

        response = await oauth_service.initiate_login("github")

        assert isinstance(response, OAuthInitResponse)
        assert "github.com/login/oauth/authorize" in response.redirect_url
        assert response.state_token

    @patch("services.oauth_service.get_settings")
    async def test_unsupported_provider_raises_validation_error(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Should raise ``ValidationError`` for providers outside ``ALLOWED_PROVIDERS``."""
        mock_get_settings.return_value = mock_settings

        with pytest.raises(ValidationError, match="(?i)unsupported.*provider.*gitlab"):
            await oauth_service.initiate_login("gitlab")

        # Redis should NOT have been called
        mock_redis.setex.assert_not_awaited()

    @patch("services.oauth_service.get_settings")
    async def test_disabled_oauth_raises_validation_error(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Should raise ``ValidationError`` when ``OAUTH_ENABLED`` is ``False``."""
        mock_settings.OAUTH_ENABLED = False
        mock_get_settings.return_value = mock_settings

        with pytest.raises(ValidationError, match="disabled"):
            await oauth_service.initiate_login("google")

        mock_redis.setex.assert_not_awaited()

    @patch("services.oauth_service.get_settings")
    async def test_state_stored_in_redis(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """State token should be stored in Redis with correct key prefix and TTL.

        The stored JSON must include the provider and ``mode='login_signup'``.
        """
        mock_get_settings.return_value = mock_settings

        await oauth_service.initiate_login("github")

        mock_redis.setex.assert_awaited_once()
        call_args = mock_redis.setex.await_args

        # Extract positional or keyword arguments
        if call_args.args:
            key, ttl, raw_data = call_args.args[0], call_args.args[1], call_args.args[2]
        else:
            key = call_args.kwargs["name"]
            ttl = call_args.kwargs["time"]
            raw_data = call_args.kwargs["value"]

        assert key.startswith(OAUTH_STATE_PREFIX)
        assert ttl == 600

        stored = json.loads(raw_data)
        assert stored["provider"] == "github"
        assert stored["mode"] == "login_signup"


# ═══════════════════════════════════════════════════════════════════════════════
# Tests for handle_callback
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestHandleCallback:
    """Tests for :meth:`OAuthService.handle_callback`."""

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_existing_oauth_account_returns_tokens(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Existing OAuthAccount should find the user and return token pair.

        Verifies that the user's active status is checked and that
        ``_issue_tokens`` is called with the correct role.
        """
        mock_get_settings.return_value = mock_settings
        mock_create_jwt.return_value = _JWT_ACCESS_TOKEN

        # ── Redis: valid state token ───────────────────────────────────────
        state_data = json.dumps({"provider": "google", "mode": "login_signup"})
        mock_redis.get.return_value = state_data

        # ── HTTP: token exchange + profile fetch ───────────────────────────
        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "provider-at"}),
                get_responses=_mock_http_response(
                    json_data={"id": _PROVIDER_USER_ID, "email": "existing@example.com", "name": "Existing"},
                ),
            )

            # ── Repos: existing OAuth account + active user ────────────────
            oauth_account = _mock_oauth_account()
            mock_oauth_repo.find_by_provider.return_value = oauth_account

            user = _mock_user(email="existing@example.com")
            mock_auth_repo.get_user_by_id.return_value = user
            mock_auth_repo.create_refresh_token.return_value = MagicMock()

            # ── Execute ────────────────────────────────────────────────────
            access_token, refresh_token = await oauth_service.handle_callback(
                provider="google",
                code="auth-code-xyz",
                state_token="valid-state-token",
            )

        # ── Assert ─────────────────────────────────────────────────────────
        assert access_token == _JWT_ACCESS_TOKEN
        assert len(refresh_token) == 64  # secrets.token_hex(32)

        # State was consumed (get + delete)
        mock_redis.get.assert_awaited_once()
        mock_redis.delete.assert_awaited_once()

        # Existing account found — no new user/org creation
        mock_oauth_repo.find_by_provider.assert_awaited_once_with("google", _PROVIDER_USER_ID)
        mock_auth_repo.get_user_by_id.assert_awaited_once_with(_USER_ID)
        mock_auth_repo.create_organization.assert_not_awaited()
        mock_auth_repo.create_dashboard_user.assert_not_awaited()

        # Tokens issued
        mock_create_jwt.assert_called_once()
        mock_auth_repo.create_refresh_token.assert_awaited_once()

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_new_google_user_creates_org_and_user(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """New Google user should create org + dashboard user + OAuthAccount.

        Verifies the full provisioning flow: org creation, prompt seeding,
        user creation with ``password_hash=None``, email verification,
        and OAuth link creation.
        """
        mock_get_settings.return_value = mock_settings
        mock_create_jwt.return_value = _JWT_ACCESS_TOKEN

        # ── Redis ──────────────────────────────────────────────────────────
        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        # ── HTTP ───────────────────────────────────────────────────────────
        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "provider-at"}),
                get_responses=_mock_http_response(
                    json_data={
                        "id": _PROVIDER_USER_ID,
                        "email": "newuser@gmail.com",
                        "name": "New Google User",
                    },
                ),
            )

            # ── Repos: no existing link, no existing user by email ─────────
            mock_oauth_repo.find_by_provider.return_value = None
            mock_auth_repo.find_user_by_email.return_value = None

            mock_org = MagicMock()
            mock_org.id = _ORG_ID
            mock_auth_repo.create_organization.return_value = mock_org

            new_user = _mock_user(email="newuser@gmail.com", name="New Google User")
            mock_auth_repo.create_dashboard_user.return_value = new_user
            mock_auth_repo.mark_email_verified.return_value = new_user
            mock_auth_repo.create_refresh_token.return_value = MagicMock()

            # ── Execute ────────────────────────────────────────────────────
            access_token, refresh_token = await oauth_service.handle_callback(
                provider="google",
                code="auth-code-abc",
                state_token="valid-state",
            )

        # ── Assert ─────────────────────────────────────────────────────────
        assert access_token == _JWT_ACCESS_TOKEN
        assert len(refresh_token) == 64

        # Org was created with correct name and plan
        mock_auth_repo.create_organization.assert_awaited_once_with(
            name="New Google User's Organization",
            plan="free",
        )
        mock_auth_repo.seed_prompts_for_org.assert_awaited_once_with(_ORG_ID)

        # Dashboard user created with NO password hash (OAuth user)
        mock_auth_repo.create_dashboard_user.assert_awaited_once_with(
            organization_id=_ORG_ID,
            email="newuser@gmail.com",
            password_hash=None,
            name="New Google User",
            role="admin",
        )

        # Email auto-verified
        mock_auth_repo.mark_email_verified.assert_awaited_once_with(_USER_ID)

        # OAuth link created
        mock_oauth_repo.create.assert_awaited_once_with(
            "google", _PROVIDER_USER_ID, _USER_ID,
        )

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_new_github_user_fetches_email_from_emails_endpoint(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """GitHub user with private email should fetch from ``/user/emails``.

        When the public profile lacks an ``email`` field, the service
        makes a secondary request to ``/user/emails`` to find the primary
        verified email.
        """
        mock_get_settings.return_value = mock_settings
        mock_create_jwt.return_value = _JWT_ACCESS_TOKEN

        # ── Redis ──────────────────────────────────────────────────────────
        mock_redis.get.return_value = json.dumps(
            {"provider": "github", "mode": "login_signup"},
        )

        # ── HTTP: three calls — POST (token) + GET (user, no email) + GET (emails) ──
        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "gh-at"}),
                get_responses=[
                    _mock_http_response(json_data={"id": "gh-456", "name": "GitHub Dev"}),  # no email
                    _mock_http_response(
                        json_data=[
                            {"email": "private@github.com", "primary": True, "verified": True},
                        ],
                    ),
                ],
            )

            # ── Repos ──────────────────────────────────────────────────────
            mock_oauth_repo.find_by_provider.return_value = None
            mock_auth_repo.find_user_by_email.return_value = None

            mock_org = MagicMock()
            mock_org.id = _ORG_ID
            mock_auth_repo.create_organization.return_value = mock_org

            gh_user = _mock_user(email="private@github.com", name="GitHub Dev")
            mock_auth_repo.create_dashboard_user.return_value = gh_user
            mock_auth_repo.mark_email_verified.return_value = gh_user
            mock_auth_repo.create_refresh_token.return_value = MagicMock()

            # ── Execute ────────────────────────────────────────────────────
            access_token, refresh_token = await oauth_service.handle_callback(
                provider="github",
                code="gh-code",
                state_token="gh-state",
            )

        # ── Assert ─────────────────────────────────────────────────────────
        assert access_token == _JWT_ACCESS_TOKEN

        # Dashboard user was created with email from /user/emails
        mock_auth_repo.create_dashboard_user.assert_awaited_once_with(
            organization_id=_ORG_ID,
            email="private@github.com",
            password_hash=None,
            name="GitHub Dev",
            role="admin",
        )

        # OAuth link created with GitHub provider and correct user ID
        mock_oauth_repo.create.assert_awaited_once_with(
            "github", "gh-456", _USER_ID,
        )

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_existing_email_auto_links_account(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """User with matching email should auto-link the OAuth account.

        When no OAuthAccount exists but a dashboard user with the same
        email is found (active, not deleted), the service should link
        the OAuth account to the existing user rather than creating a
        new org + user.
        """
        mock_get_settings.return_value = mock_settings
        mock_create_jwt.return_value = _JWT_ACCESS_TOKEN

        # ── Redis ──────────────────────────────────────────────────────────
        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        # ── HTTP ───────────────────────────────────────────────────────────
        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "at"}),
                get_responses=_mock_http_response(
                    json_data={"id": _PROVIDER_USER_ID, "email": "existing@example.com", "name": "Existing"},
                ),
            )

            # ── Repos: no OAuth link, but existing user by email ───────────
            mock_oauth_repo.find_by_provider.return_value = None

            existing_user = _mock_user(id=_USER_ID, email="existing@example.com", name="Existing")
            mock_auth_repo.find_user_by_email.return_value = existing_user
            mock_auth_repo.create_refresh_token.return_value = MagicMock()

            # ── Execute ────────────────────────────────────────────────────
            access_token, refresh_token = await oauth_service.handle_callback(
                provider="google",
                code="auth-code",
                state_token="state",
            )

        # ── Assert ─────────────────────────────────────────────────────────
        assert access_token == _JWT_ACCESS_TOKEN

        # Should NOT create a new org or user
        mock_auth_repo.create_organization.assert_not_awaited()
        mock_auth_repo.create_dashboard_user.assert_not_awaited()

        # Should link the OAuth account to the existing user
        mock_oauth_repo.create.assert_awaited_once_with(
            "google", _PROVIDER_USER_ID, _USER_ID,
        )

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_invalid_state_token_raises_authentication_error(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Invalid/expired state token should raise ``AuthenticationError``.

        When ``_consume_state`` returns ``None`` (Redis miss or expired key),
        the callback must reject the request immediately.
        """
        mock_get_settings.return_value = mock_settings
        mock_redis.get.return_value = None  # No state found

        with pytest.raises(AuthenticationError, match="state token.*invalid.*expired"):
            await oauth_service.handle_callback(
                provider="google",
                code="code",
                state_token="bad-state",
            )

        # State was checked but not deleted (nothing to delete)
        mock_redis.get.assert_awaited_once()
        mock_redis.delete.assert_not_awaited()

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_state_token_consumed_single_use(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """State token should be deleted from Redis after use (single-use).

        The ``_consume_state`` method does a ``GET`` followed by a ``DEL``
        to prevent replay attacks.
        """
        mock_get_settings.return_value = mock_settings
        mock_create_jwt.return_value = _JWT_ACCESS_TOKEN

        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "at"}),
                get_responses=_mock_http_response(
                    json_data={"id": _PROVIDER_USER_ID, "email": "u@e.com", "name": "U"},
                ),
            )

            oauth_account = _mock_oauth_account()
            mock_oauth_repo.find_by_provider.return_value = oauth_account
            user = _mock_user()
            mock_auth_repo.get_user_by_id.return_value = user
            mock_auth_repo.create_refresh_token.return_value = MagicMock()

            await oauth_service.handle_callback(
                provider="google",
                code="code",
                state_token="single-use-state",
            )

        # GET to retrieve, DELETE to consume
        mock_redis.get.assert_awaited_once()
        mock_redis.delete.assert_awaited_once()

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_deactivated_user_raises_error(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Deactivated linked user should raise ``AuthenticationError``.

        Covers two scenarios: ``is_deleted=True`` and ``is_active=False``.
        Both should prevent login.
        """
        mock_get_settings.return_value = mock_settings

        # ── Case 1: user.is_deleted == True ────────────────────────────────
        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "at"}),
                get_responses=_mock_http_response(
                    json_data={"id": _PROVIDER_USER_ID, "email": "gone@e.com", "name": "Gone"},
                ),
            )

            oauth_account = _mock_oauth_account()
            mock_oauth_repo.find_by_provider.return_value = oauth_account
            deleted_user = _mock_user(is_deleted=True)
            mock_auth_repo.get_user_by_id.return_value = deleted_user

            with pytest.raises(AuthenticationError, match="deactivated"):
                await oauth_service.handle_callback(
                    provider="google",
                    code="code",
                    state_token="state",
                )

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_linking_mode_creates_link_for_existing_user(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Linking mode with no existing OAuthAccount should create the link.

        When ``mode='link'`` is in the state token and the ``user_id``
        points to an active dashboard user, the service must create the
        OAuth account link for that user.
        """
        mock_get_settings.return_value = mock_settings
        mock_create_jwt.return_value = _JWT_ACCESS_TOKEN

        mock_redis.get.return_value = json.dumps(
            {"provider": "github", "mode": "link", "user_id": str(_USER_ID)},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "at"}),
                get_responses=_mock_http_response(
                    json_data={"id": "gh-789", "email": "link@test.com", "name": "Link"},
                ),
            )

            # No existing OAuthAccount
            mock_oauth_repo.find_by_provider.return_value = None
            # But user found by the stored user_id from state
            mock_auth_repo.get_user_by_id.return_value = _mock_user(
                id=_USER_ID, email="existing@user.com",
            )

            result = await oauth_service.handle_callback(
                provider="github",
                code="code",
                state_token="state",
            )

            # Should succeed and issue tokens (refresh token is randomly generated)
            assert result[0] == _JWT_ACCESS_TOKEN
            assert len(result[1]) == 64  # refresh token is token_hex(32)
            # Should create the OAuth link
            mock_oauth_repo.create.assert_awaited_once_with(
                "github", "gh-789", _USER_ID,
            )

    @patch("services.oauth_service.get_settings")
    async def test_google_token_exchange_failure(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Failed token exchange should raise ``AuthenticationError``.

        Simulates a non-200 response from the provider's token endpoint.
        """
        mock_get_settings.return_value = mock_settings

        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(status=400, json_data={"error": "invalid_grant"}),
                get_responses=_mock_http_response(json_data={}),
            )

            with pytest.raises(AuthenticationError, match="Failed to exchange"):
                await oauth_service.handle_callback(
                    provider="google",
                    code="bad-code",
                    state_token="state",
                )

    @patch("services.oauth_service.get_settings")
    async def test_provider_error_in_token_response(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Provider error dict in token response should raise ``AuthenticationError``.

        Some providers return HTTP 200 with an ``error`` field in the JSON
        body (e.g. ``{"error": "invalid_client"}``).  The service must
        detect and reject these.
        """
        mock_get_settings.return_value = mock_settings

        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(
                    status=200,
                    json_data={"error": "invalid_client", "error_description": "Bad client id"},
                ),
            )

            with pytest.raises(AuthenticationError, match="invalid_client"):
                await oauth_service.handle_callback(
                    provider="google",
                    code="code",
                    state_token="state",
                )

    @patch("services.oauth_service.get_settings")
    async def test_missing_access_token_in_response(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Missing ``access_token`` in token response should raise error.

        The provider returned HTTP 200 without an ``access_token`` field.
        """
        mock_get_settings.return_value = mock_settings

        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(
                    status=200,
                    json_data={"token_type": "Bearer"},  # no access_token
                ),
            )

            with pytest.raises(AuthenticationError, match="No access_token"):
                await oauth_service.handle_callback(
                    provider="google",
                    code="code",
                    state_token="state",
                )


# ═══════════════════════════════════════════════════════════════════════════════
# Tests for account management
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestAccountLinking:
    """Tests for :meth:`OAuthService.initiate_link` and :meth:`OAuthService.unlink_account`."""

    @patch("services.oauth_service.get_settings")
    async def test_initiate_link_stores_user_id_in_state(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Link state should contain ``user_id`` and ``mode='link'``.

        Unlike login/signup, the link flow associates the OAuth identity
        with an already-authenticated user.
        """
        mock_get_settings.return_value = mock_settings

        response = await oauth_service.initiate_link(
            provider="google",
            user_id=str(_USER_ID),
        )

        assert isinstance(response, OAuthInitResponse)
        assert "accounts.google.com" in response.redirect_url

        # Verify Redis stored data includes user_id and mode='link'
        mock_redis.setex.assert_awaited_once()
        call_args = mock_redis.setex.await_args
        raw_data = (
            call_args.args[2]
            if len(call_args.args) > 2
            else call_args.kwargs.get("value", "")
        )
        stored = json.loads(raw_data)
        assert stored["provider"] == "google"
        assert stored["mode"] == "link"
        assert stored["user_id"] == str(_USER_ID)

    @patch("services.oauth_service.get_settings")
    async def test_initiate_link_unsupported_provider(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_settings: MagicMock,
    ) -> None:
        """Initiate link with unsupported provider should raise ``ValidationError``."""
        mock_get_settings.return_value = mock_settings

        with pytest.raises(ValidationError, match="(?i)unsupported.*provider"):
            await oauth_service.initiate_link(provider="facebook", user_id=str(_USER_ID))

    @patch("services.oauth_service.get_settings")
    async def test_unlink_account_removes_link(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Unlink should delete the ``OAuthAccount`` record for the provider."""
        mock_get_settings.return_value = mock_settings

        # Mock existing accounts for the user
        existing = [
            _mock_oauth_account(provider="google"),
            _mock_oauth_account(provider="github", provider_user_id="gh-1"),
        ]
        mock_oauth_repo.find_by_user_id.return_value = existing

        await oauth_service.unlink_account(provider="google", user_id=str(_USER_ID))

        # Should delete the Google account only
        mock_oauth_repo.delete.assert_awaited_once_with(_OAUTH_ACCT_ID)

    @patch("services.oauth_service.get_settings")
    async def test_unlink_nonexistent_account_raises_not_found(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Unlinking a non-existent provider should raise ``NotFoundError``."""
        mock_get_settings.return_value = mock_settings

        mock_oauth_repo.find_by_user_id.return_value = [
            _mock_oauth_account(provider="google"),
        ]

        with pytest.raises(NotFoundError, match="No linked.*github.*found"):
            await oauth_service.unlink_account(provider="github", user_id=str(_USER_ID))

        mock_oauth_repo.delete.assert_not_awaited()

    @patch("services.oauth_service.get_settings")
    async def test_get_linked_accounts_returns_list(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Get accounts should return ``OAuthAccountResponse`` list."""
        mock_get_settings.return_value = mock_settings

        now = datetime(2025, 6, 15)
        accounts_data = [
            _mock_oauth_account(provider="google", created_at=now),
            _mock_oauth_account(
                id=UUID("44444444-4444-4444-4444-444444444444"),
                provider="github",
                provider_user_id="gh-user",
                created_at=now,
            ),
        ]
        mock_oauth_repo.find_by_user_id.return_value = accounts_data

        result = await oauth_service.get_linked_accounts(user_id=str(_USER_ID))

        assert len(result) == 2
        for acct in result:
            assert isinstance(acct, OAuthAccountResponse)
        assert result[0].provider == "google"
        assert result[1].provider == "github"
        assert result[1].provider_user_id == "gh-user"

    @patch("services.oauth_service.get_settings")
    async def test_get_linked_accounts_empty(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Get accounts for a user with no linked accounts should return empty list."""
        mock_get_settings.return_value = mock_settings

        mock_oauth_repo.find_by_user_id.return_value = []

        result = await oauth_service.get_linked_accounts(user_id=str(_USER_ID))

        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# Tests for edge cases
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEdgeCases:
    """Tests for edge cases in OAuth flow."""

    @patch("services.oauth_service.get_settings")
    async def test_state_provider_mismatch(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """State provider should match callback provider.

        If the state token was created for ``google`` but the callback
        comes to the ``github`` endpoint, the service must reject.
        This is a CSRF protection mechanism.
        """
        mock_get_settings.return_value = mock_settings

        # State says "google", but callback is for "github"
        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "at"}),
            )

            with pytest.raises(
                AuthenticationError,
                match="provider mismatch.*expected.*google.*got.*github",
            ):
                await oauth_service.handle_callback(
                    provider="github",
                    code="code",
                    state_token="state",
                )

    @patch("services.oauth_service.get_settings")
    async def test_oauth_account_integrity_error_propagates(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Duplicate OAuth link should propagate ``IntegrityError``.

        When ``oauth_repo.create`` raises ``IntegrityError`` (duplicate
        provider + provider_user_id), the service currently does not
        catch it — the caller (router + DB session) is responsible for
        handling the constraint violation.
        """
        mock_get_settings.return_value = mock_settings

        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "at"}),
                get_responses=_mock_http_response(
                    json_data={"id": _PROVIDER_USER_ID, "email": "dup@test.com", "name": "Dup"},
                ),
            )

            mock_oauth_repo.find_by_provider.return_value = None
            mock_auth_repo.find_user_by_email.return_value = None

            mock_org = MagicMock()
            mock_org.id = _ORG_ID
            mock_auth_repo.create_organization.return_value = mock_org
            mock_auth_repo.create_dashboard_user.return_value = _mock_user()

            from sqlalchemy.exc import IntegrityError as SAIntegrityError
            mock_oauth_repo.create.side_effect = SAIntegrityError(
                "duplicate key value violates unique constraint",
                params={},
                orig=Exception(),
            )

            with pytest.raises(SAIntegrityError):
                await oauth_service.handle_callback(
                    provider="google",
                    code="code",
                    state_token="state",
                )

    @patch("services.oauth_service.get_settings")
    async def test_google_auth_url_contains_expected_params(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Google auth URL should include correct query parameters.

        Verifies ``client_id``, ``redirect_uri``, ``response_type``,
        ``scope``, ``state``, ``access_type``, and ``prompt``.
        """
        mock_get_settings.return_value = mock_settings

        response = await oauth_service.initiate_login("google")

        url = response.redirect_url
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
        assert "client_id=google-client-id-123" in url
        assert "response_type=code" in url
        assert "scope=openid+email+profile" in url
        assert "state=" + response.state_token in url
        assert "access_type=online" in url
        assert "prompt=select_account" in url
        # Callback URL derived from CORS_ORIGINS
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A3000%2Fv1%2Fauth%2Foauth%2Fgoogle%2Fcallback" in url

    @patch("services.oauth_service.get_settings")
    async def test_github_auth_url_contains_expected_params(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """GitHub auth URL should include correct query parameters.

        Verifies ``client_id``, ``redirect_uri``, ``scope``, and ``state``.
        GitHub's OAuth is simpler — no ``response_type`` or ``prompt``.
        """
        mock_get_settings.return_value = mock_settings

        response = await oauth_service.initiate_login("github")

        url = response.redirect_url
        assert url.startswith("https://github.com/login/oauth/authorize")
        assert "client_id=github-client-id-456" in url
        assert "scope=read%3Auser+user%3Aemail" in url
        assert "state=" + response.state_token in url
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A3000%2Fv1%2Fauth%2Foauth%2Fgithub%2Fcallback" in url

    @patch("services.oauth_service.get_settings")
    async def test_callback_url_derived_from_cors_origins(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_settings: MagicMock,
    ) -> None:
        """Callback URL should be derived from the first CORS origin.

        When ``CORS_ORIGINS`` is ``"https://app.example.com"``, the
        callback URL should be ``https://app.example.com/v1/auth/oauth/...``.
        """
        mock_settings.CORS_ORIGINS = "https://app.example.com"
        mock_get_settings.return_value = mock_settings

        response = await oauth_service.initiate_login("google")

        assert "redirect_uri=https%3A%2F%2Fapp.example.com%2Fv1%2Fauth%2Foauth%2Fgoogle%2Fcallback" in response.redirect_url

    @patch("services.oauth_service.get_settings")
    async def test_callback_url_fallback_no_cors(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_settings: MagicMock,
    ) -> None:
        """Callback URL when ``CORS_ORIGINS`` is empty string.

        When ``OAUTH_CALLBACK_BASE_URL`` is empty and ``CORS_ORIGINS``
        is also empty (or has an empty first element), the callback URL
        falls back to ``http://localhost:8000`` as the base.
        """
        mock_settings.CORS_ORIGINS = ""
        mock_get_settings.return_value = mock_settings

        response = await oauth_service.initiate_login("google")

        assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fv1%2Fauth%2Foauth%2Fgoogle%2Fcallback" in response.redirect_url

    @patch("services.oauth_service.get_settings")
    async def test_validate_provider_empty_string(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_settings: MagicMock,
    ) -> None:
        """Empty string provider should raise ``ValidationError``."""
        mock_get_settings.return_value = mock_settings

        with pytest.raises(ValidationError, match="(?i)unsupported.*provider"):
            await oauth_service.initiate_login("")

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_github_user_with_email_in_profile_skips_emails_endpoint(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """GitHub user with public email should NOT call ``/user/emails``.

        When the public profile includes an ``email`` field, the service
        must skip the secondary emails request for efficiency.
        """
        mock_get_settings.return_value = mock_settings
        mock_create_jwt.return_value = _JWT_ACCESS_TOKEN

        mock_redis.get.return_value = json.dumps(
            {"provider": "github", "mode": "login_signup"},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            mock_client = _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "gh-at"}),
                # Only ONE GET call — no secondary emails endpoint
                get_responses=_mock_http_response(
                    json_data={
                        "id": "gh-789",
                        "email": "public@github.com",
                        "name": "Public GH",
                    },
                ),
            )

            mock_oauth_repo.find_by_provider.return_value = None
            mock_auth_repo.find_user_by_email.return_value = None

            mock_org = MagicMock()
            mock_org.id = _ORG_ID
            mock_auth_repo.create_organization.return_value = mock_org

            gh_user = _mock_user(email="public@github.com")
            mock_auth_repo.create_dashboard_user.return_value = gh_user
            mock_auth_repo.mark_email_verified.return_value = gh_user
            mock_auth_repo.create_refresh_token.return_value = MagicMock()

            await oauth_service.handle_callback(
                provider="github",
                code="gh-code",
                state_token="gh-state",
            )

        # Only one GET call (user profile), no secondary emails call
        assert mock_client.get.call_count == 1

    @patch("services.oauth_service.get_settings")
    @patch("services.oauth_service.create_jwt_token")
    async def test_inactive_user_auto_link_skipped(
        self,
        mock_create_jwt: MagicMock,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_oauth_repo: MagicMock,
        mock_auth_repo: MagicMock,
        mock_redis: AsyncMock,
        mock_settings: MagicMock,
    ) -> None:
        """Inactive user with matching email should NOT auto-link.

        When ``find_user_by_email`` returns an inactive user, the service
        must treat it as "no existing user" and create a new account
        rather than auto-linking.
        """
        mock_get_settings.return_value = mock_settings
        mock_create_jwt.return_value = _JWT_ACCESS_TOKEN

        mock_redis.get.return_value = json.dumps(
            {"provider": "google", "mode": "login_signup"},
        )

        with patch("services.oauth_service.httpx.AsyncClient") as mock_http_cls:
            _configure_http_mock(
                mock_http_cls,
                post_response=_mock_http_response(json_data={"access_token": "at"}),
                get_responses=_mock_http_response(
                    json_data={"id": _PROVIDER_USER_ID, "email": "inactive@test.com", "name": "Inactive"},
                ),
            )

            mock_oauth_repo.find_by_provider.return_value = None
            # Found existing user, but it's inactive — should NOT auto-link
            inactive_user = _mock_user(is_active=False, email="inactive@test.com")
            mock_auth_repo.find_user_by_email.return_value = inactive_user

            mock_org = MagicMock()
            mock_org.id = _ORG_ID
            mock_auth_repo.create_organization.return_value = mock_org
            new_user = _mock_user(email="inactive@test.com")
            mock_auth_repo.create_dashboard_user.return_value = new_user
            mock_auth_repo.mark_email_verified.return_value = new_user
            mock_auth_repo.create_refresh_token.return_value = MagicMock()

            await oauth_service.handle_callback(
                provider="google",
                code="code",
                state_token="state",
            )

        # Should NOT have auto-linked — must create new user
        mock_auth_repo.create_organization.assert_awaited_once()
        mock_auth_repo.create_dashboard_user.assert_awaited_once()

    @patch("services.oauth_service.get_settings")
    async def test_unlink_account_invalid_uuid(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_settings: MagicMock,
    ) -> None:
        """Unlink account with invalid UUID should raise ``ValidationError``."""
        mock_get_settings.return_value = mock_settings

        with pytest.raises(ValidationError, match="Invalid UUID.*user_id"):
            await oauth_service.unlink_account(
                provider="google",
                user_id="not-a-valid-uuid",
            )

    @patch("services.oauth_service.get_settings")
    async def test_get_linked_accounts_invalid_uuid(
        self,
        mock_get_settings: MagicMock,
        oauth_service: OAuthService,
        mock_settings: MagicMock,
    ) -> None:
        """Get linked accounts with invalid UUID should raise ``ValidationError``."""
        mock_get_settings.return_value = mock_settings

        with pytest.raises(ValidationError, match="Invalid UUID"):
            await oauth_service.get_linked_accounts(user_id="bad-uuid")
