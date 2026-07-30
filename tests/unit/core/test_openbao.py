"""Unit tests for OpenBao HTTP client — mock-based, no OpenBao required.

Covers:
- AppRole authentication (success, invalid creds, connection errors)
- Token lifecycle (auto-renew, expiry)
- KV v2 read/write/list/delete
- System config read/write (CAS-aware)
- Org config read/write/namespace management
- Transit encrypt/decrypt/rotate/rewrap
- HTTP error mapping (4xx → typed exceptions, 5xx → connection error)
- 429 retry logic with exponential backoff
- Timeout and connection-error propagation
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest

from core.openbao import (
    KV_MOUNT,
    ORG_NAMESPACE_PREFIX,
    SYSTEM_KEY_MAPPING,
    _MAX_RETRIES,
    OpenBaoClient,
)
from core.openbao_exceptions import (
    OpenBaoAuthError,
    OpenBaoConnectionError,
    OpenBaoError,
    OpenBaoNamespaceError,
    OpenBaoRateLimitError,
    OpenBaoSecretNotFoundError,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_http() -> AsyncMock:
    """Return a clean mock ``httpx.AsyncClient`` for injection.

    Default response: ``200 OK`` with an empty JSON body ``{"data": {"data": {}}}``
    so that ``_kv_read``, ``_request``, etc. can decode it without raising.
    """
    client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.json.return_value = {"data": {"data": {}}}
    client.request = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    return client


@pytest.fixture
def bao_client(mock_http: AsyncMock) -> OpenBaoClient:
    """Return an :class:`OpenBaoClient` with the HTTP layer fully mocked and
    a synthetic client token already set (no real ``__aenter__`` call).
    """
    client = OpenBaoClient(
        "http://localhost:8200",
        "role-id",
        "secret-id",
        timeout=5.0,
        namespace="system/",
        root_token_path=None,  # skip file I/O
    )
    client._http = mock_http
    client._client_token = "s.test-token-abc123"
    return client


def _make_response(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    """Build a ``httpx.Response``-shaped MagicMock with the given status and JSON body."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# AppRole authentication
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestAppRoleAuthentication:
    """Exercise ``_authenticate`` — the AppRole login handshake."""

    @pytest.mark.asyncio
    async def test_successful_approle_login(self, mock_http: AsyncMock) -> None:
        """A valid AppRole login returns a client token."""
        mock_http.post.return_value = _make_response(
            200,
            {
                "auth": {
                    "client_token": "s.generated-token",
                    "lease_duration": 3600,
                },
            },
        )
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )
        client._http = mock_http

        await client._authenticate()

        assert client._client_token == "s.generated-token"
        mock_http.post.assert_awaited_once_with(
            "/v1/auth/approle/login",
            headers={"X-Vault-Namespace": "system/"},
            json={"role_id": "role-id", "secret_id": "secret-id"},
        )

    @pytest.mark.asyncio
    async def test_successful_login_root_namespace(self, mock_http: AsyncMock) -> None:
        """No namespace header when namespace is empty string."""
        mock_http.post.return_value = _make_response(
            200,
            {"auth": {"client_token": "s.root-token", "lease_duration": 3600}},
        )
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            namespace="",
            root_token_path=None,
        )
        client._http = mock_http

        await client._authenticate()

        # No X-Vault-Namespace header for root namespace
        call_kwargs = mock_http.post.call_args.kwargs
        assert "X-Vault-Namespace" not in call_kwargs.get("headers", {})

    @pytest.mark.asyncio
    async def test_invalid_credentials(self, mock_http: AsyncMock) -> None:
        """A non-200 response from AppRole login raises ``OpenBaoAuthError``."""
        mock_http.post.return_value = _make_response(
            400,
            {"errors": ["invalid role or secret"]},
        )
        client = OpenBaoClient(
            "http://localhost:8200",
            "bad-role",
            "bad-secret",
            root_token_path=None,
        )
        client._http = mock_http

        with pytest.raises(OpenBaoAuthError, match="AppRole login failed"):
            await client._authenticate()

    @pytest.mark.asyncio
    async def test_connection_refused(self, mock_http: AsyncMock) -> None:
        """A network-level ``ConnectError`` raises ``OpenBaoConnectionError``."""
        mock_http.post.side_effect = httpx.ConnectError("Connection refused")
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )
        client._http = mock_http

        with pytest.raises(OpenBaoConnectionError, match="Cannot connect"):
            await client._authenticate()

    @pytest.mark.asyncio
    async def test_timeout_on_login(self, mock_http: AsyncMock) -> None:
        """A ``TimeoutException`` during login raises ``OpenBaoConnectionError``."""
        mock_http.post.side_effect = httpx.TimeoutException("timed out")
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )
        client._http = mock_http

        with pytest.raises(OpenBaoConnectionError, match="Timeout"):
            await client._authenticate()

    @pytest.mark.asyncio
    async def test_malformed_response_missing_token(self, mock_http: AsyncMock) -> None:
        """A 200 response without ``auth.client_token`` raises ``OpenBaoAuthError``."""
        mock_http.post.return_value = _make_response(200, {"auth": {}})
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )
        client._http = mock_http

        with pytest.raises(OpenBaoAuthError, match="missing 'auth.client_token'"):
            await client._authenticate()

    @pytest.mark.asyncio
    async def test_no_http_client(self) -> None:
        """Calling ``_authenticate`` without an HTTP client raises immediately."""
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )

        with pytest.raises(OpenBaoConnectionError, match="not initialised"):
            await client._authenticate()


# ═══════════════════════════════════════════════════════════════════════════════
# Token management
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestTokenManagement:
    """Token lifecycle: access, expiry-based re-auth, missing-token guard."""

    @pytest.mark.asyncio
    async def test_token_property_returns_token(self, bao_client: OpenBaoClient) -> None:
        """``_token`` returns the stored client token."""
        assert bao_client._token == "s.test-token-abc123"

    @pytest.mark.asyncio
    async def test_token_property_raises_when_not_authenticated(self) -> None:
        """``_token`` raises ``OpenBaoAuthError`` when token is ``None``."""
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )
        with pytest.raises(OpenBaoAuthError, match="Not authenticated"):
            _ = client._token

    @pytest.mark.asyncio
    async def test_ensure_auth_reauthenticates_when_expired(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """``_ensure_auth`` triggers re-auth when the token is past its expiry threshold."""
        # Set token_expires_at in the past
        bao_client._token_expires_at = time.monotonic() - 60.0
        # Set up the re-auth response
        mock_http.post.return_value = _make_response(
            200,
            {
                "auth": {
                    "client_token": "s.renewed-token",
                    "lease_duration": 3600,
                },
            },
        )

        await bao_client._ensure_auth()

        assert bao_client._client_token == "s.renewed-token"
        mock_http.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ensure_auth_does_not_reauthenticate_when_fresh(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """``_ensure_auth`` is a no-op when the token is still valid."""
        bao_client._token_expires_at = time.monotonic() + 3600.0  # 1 hour from now

        await bao_client._ensure_auth()

        mock_http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_headers_includes_token(self, bao_client: OpenBaoClient) -> None:
        """``_headers`` always includes ``X-Vault-Token``."""
        headers = bao_client._headers()
        assert headers["X-Vault-Token"] == "s.test-token-abc123"

    @pytest.mark.asyncio
    async def test_headers_includes_namespace_when_provided(
        self,
        bao_client: OpenBaoClient,
    ) -> None:
        """``_headers`` adds ``X-Vault-Namespace`` when a namespace is passed."""
        headers = bao_client._headers(namespace="org_abc123/")
        assert headers["X-Vault-Namespace"] == "org_abc123/"

    @pytest.mark.asyncio
    async def test_headers_omits_namespace_when_not_provided(
        self,
        bao_client: OpenBaoClient,
    ) -> None:
        """``_headers`` omits ``X-Vault-Namespace`` when no namespace is passed."""
        headers = bao_client._headers()
        assert "X-Vault-Namespace" not in headers


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP error mapping
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestErrorMapping:
    """Static ``_raise_on_error`` maps HTTP status → typed exception."""

    def test_200_is_no_op(self) -> None:
        OpenBaoClient._raise_on_error(_make_response(200), "some/path")
        # Should not raise

    def test_204_is_no_op(self) -> None:
        OpenBaoClient._raise_on_error(_make_response(204), "some/path")
        # Should not raise

    def test_401_raises_auth_error(self) -> None:
        with pytest.raises(OpenBaoAuthError, match="permission denied"):
            resp = _make_response(401, {"errors": ["permission denied"]})
            OpenBaoClient._raise_on_error(resp, "secret/data/key")

    def test_403_raises_auth_error(self) -> None:
        with pytest.raises(OpenBaoAuthError, match="forbidden"):
            resp = _make_response(403, {"errors": ["forbidden"]})
            OpenBaoClient._raise_on_error(resp, "secret/data/key")

    def test_404_raises_secret_not_found(self) -> None:
        with pytest.raises(OpenBaoSecretNotFoundError, match="not found"):
            resp = _make_response(404, {"errors": ["not found"]})
            OpenBaoClient._raise_on_error(resp, "secret/data/missing")

    def test_412_raises_namespace_error(self) -> None:
        with pytest.raises(OpenBaoNamespaceError, match="namespace error"):
            resp = _make_response(412, {"errors": ["namespace error"]})
            OpenBaoClient._raise_on_error(resp, "sys/namespaces/org_x")

    def test_429_raises_rate_limit_error(self) -> None:
        with pytest.raises(OpenBaoRateLimitError, match="rate limit"):
            resp = _make_response(429, {"errors": ["rate limit"]})
            OpenBaoClient._raise_on_error(resp, "some/path")

    def test_500_raises_connection_error(self) -> None:
        with pytest.raises(OpenBaoConnectionError, match="Server error"):
            resp = _make_response(500, {"errors": ["internal error"]})
            OpenBaoClient._raise_on_error(resp, "some/path")

    def test_503_raises_connection_error(self) -> None:
        with pytest.raises(OpenBaoConnectionError, match="Server error"):
            resp = _make_response(503, {"errors": ["unavailable"]})
            OpenBaoClient._raise_on_error(resp, "some/path")

    def test_418_raises_generic_error(self) -> None:
        with pytest.raises(OpenBaoError, match="HTTP 418"):
            resp = _make_response(418, {"errors": ["teapot"]})
            OpenBaoClient._raise_on_error(resp, "some/path")

    def test_no_json_body_falls_back_to_reason_phrase(self) -> None:
        with pytest.raises(OpenBaoAuthError, match="Unauthorized"):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 401
            resp.json.side_effect = ValueError("no json")
            resp.reason_phrase = "Unauthorized"
            OpenBaoClient._raise_on_error(resp, "some/path")

    def test_empty_errors_uses_reason_phrase(self) -> None:
        with pytest.raises(OpenBaoAuthError, match="Unauthorized"):
            resp = _make_response(401, {"errors": []})
            resp.reason_phrase = "Unauthorized"
            OpenBaoClient._raise_on_error(resp, "some/path")


# ═══════════════════════════════════════════════════════════════════════════════
# _request — low-level request helper
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestRequestHelper:
    """Low-level ``_request`` path (retry logic, error wrapping)."""

    @pytest.mark.asyncio
    async def test_successful_request(self, bao_client: OpenBaoClient, mock_http: AsyncMock) -> None:
        """A 200 response is returned directly."""
        resp = await bao_client._request("GET", "config/data/key")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_http_client_raises(self) -> None:
        """Calling ``_request`` without an HTTP client raises immediately."""
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )
        with pytest.raises(OpenBaoConnectionError, match="not initialised"):
            await client._request("GET", "config/data/key")

    @pytest.mark.asyncio
    async def test_connection_error_wraps_httpx_connect_error(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """``httpx.ConnectError`` becomes ``OpenBaoConnectionError``."""
        mock_http.request.side_effect = httpx.ConnectError("Connection refused")
        with pytest.raises(OpenBaoConnectionError, match="Cannot connect"):
            await bao_client._request("GET", "config/data/key")

    @pytest.mark.asyncio
    async def test_timeout_error_wraps_httpx_timeout(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """``httpx.TimeoutException`` becomes ``OpenBaoConnectionError``."""
        mock_http.request.side_effect = httpx.TimeoutException("timed out")
        with pytest.raises(OpenBaoConnectionError, match="Timeout"):
            await bao_client._request("GET", "config/data/key")

    @pytest.mark.asyncio
    async def test_url_is_prefixed_with_v1(self, bao_client: OpenBaoClient, mock_http: AsyncMock) -> None:
        """The request URL is prefixed with ``/v1/``."""
        await bao_client._request("GET", "config/data/key")
        call_args = mock_http.request.call_args
        assert call_args[0][1] == "/v1/config/data/key"

    @pytest.mark.asyncio
    async def test_url_strips_leading_slash(self, bao_client: OpenBaoClient, mock_http: AsyncMock) -> None:
        """Leading slashes in path are stripped before adding ``/v1/`` prefix."""
        await bao_client._request("GET", "/config/data/key")
        call_args = mock_http.request.call_args
        assert call_args[0][1] == "/v1/config/data/key"


# ═══════════════════════════════════════════════════════════════════════════════
# 429 retry logic
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestRateLimitRetry:
    """``_request`` retries up to ``_MAX_RETRIES`` on 429 with backoff."""

    @pytest.mark.asyncio
    async def test_retries_on_429_and_succeeds(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """A 429 is retried; eventual 200 succeeds."""
        rate_limited = _make_response(429, {"errors": ["rate limit"]})
        success = _make_response(200, {"data": {"data": {"key": "val"}}})
        mock_http.request.side_effect = [rate_limited, rate_limited, success]

        resp = await bao_client._request("GET", "config/data/key")

        assert resp.status_code == 200
        assert mock_http.request.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_retries(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Persistent 429 raises ``OpenBaoRateLimitError`` after all retries."""
        rate_limited = _make_response(429, {"errors": ["rate limit"]})
        mock_http.request.return_value = rate_limited

        with pytest.raises(OpenBaoRateLimitError):
            await bao_client._request("GET", "config/data/key")

        assert mock_http.request.call_count == _MAX_RETRIES

    @pytest.mark.asyncio
    async def test_uses_exponential_backoff(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Each retry waits ``2 ** attempt`` seconds."""
        rate_limited = _make_response(429, {"errors": ["rate limit"]})
        mock_http.request.return_value = rate_limited

        with patch.object(asyncio, "sleep", AsyncMock()) as mock_sleep:
            with pytest.raises(OpenBaoRateLimitError):
                await bao_client._request("GET", "config/data/key")

            # Expect sleeps: 1s, 4s (2^0, 2^1, 2^2 but only 2 attempts needed for 3 retries)
            assert mock_sleep.call_count == 2  # (3 retries - 1 extra)
            # 2^0 = 1, 2^1 = 2
            assert mock_sleep.call_args_list[0][0][0] == 1.0
            assert mock_sleep.call_args_list[1][0][0] == 2.0


# ═══════════════════════════════════════════════════════════════════════════════
# KV v2 — read
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestKvRead:
    """``_kv_read`` — reading secrets from the KV v2 engine."""

    @pytest.mark.asyncio
    async def test_read_existing_key(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Reading an existing key returns the data dict."""
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"data": {"value": "secret-value"}}},
        )
        data = await bao_client._kv_read("config/data/mykey")
        assert data == {"value": "secret-value"}

    @pytest.mark.asyncio
    async def test_read_nested_key(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Reading a nested path works correctly."""
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"data": {"nested": {"key": "deep-value"}}}},
        )
        data = await bao_client._kv_read("config/data/nested/key")
        assert data == {"nested": {"key": "deep-value"}}

    @pytest.mark.asyncio
    async def test_read_missing_key_raises(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Reading a missing key raises ``OpenBaoSecretNotFoundError``."""
        mock_http.request.return_value = _make_response(
            404,
            {"errors": ["no secret found"]},
        )
        with pytest.raises(OpenBaoSecretNotFoundError):
            await bao_client._kv_read("config/data/missing")

    @pytest.mark.asyncio
    async def test_read_with_meta_returns_version(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """``include_meta=True`` returns ``(data, version)`` tuple."""
        mock_http.request.return_value = _make_response(
            200,
            {
                "data": {
                    "data": {"value": "v2-value"},
                    "metadata": {"version": 2},
                },
            },
        )
        data, version = await bao_client._kv_read("config/data/mykey", include_meta=True)
        assert data == {"value": "v2-value"}
        assert version == 2

    @pytest.mark.asyncio
    async def test_read_with_meta_missing_metadata_defaults_to_zero(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """When metadata is absent, version defaults to 0."""
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"data": {"value": "v1"}}},
        )
        data, version = await bao_client._kv_read("config/data/mykey", include_meta=True)
        assert version == 0

    @pytest.mark.asyncio
    async def test_read_with_namespace_header(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Passing a namespace sends the ``X-Vault-Namespace`` header."""
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"data": {"value": "ns-value"}}},
        )
        ns = "org_abc123/"
        await bao_client._kv_read("config/data/mykey", namespace=ns)
        call_headers = mock_http.request.call_args.kwargs.get("headers", {})
        assert call_headers.get("X-Vault-Namespace") == ns


# ═══════════════════════════════════════════════════════════════════════════════
# KV v2 — write
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestKvWrite:
    """``_kv_write`` — writing secrets to the KV v2 engine."""

    @pytest.mark.asyncio
    async def test_write_creates_key(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Writing a key sends a POST with the data."""
        mock_http.request.return_value = _make_response(200)

        await bao_client._kv_write("config/data/mykey", {"value": "new-value"})

        mock_http.request.assert_awaited_once()
        call_kwargs = mock_http.request.call_args.kwargs
        assert call_kwargs["json"] == {"data": {"value": "new-value"}, "options": {}}

    @pytest.mark.asyncio
    async def test_write_with_cas_version(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Write with CAS includes the version in options."""
        await bao_client._kv_write("config/data/mykey", {"value": "v3"}, cas_version=3)
        call_kwargs = mock_http.request.call_args.kwargs
        assert call_kwargs["json"] == {"data": {"value": "v3"}, "options": {"cas": 3}}

    @pytest.mark.asyncio
    async def test_write_with_namespace(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Write within a namespace sends the header."""
        ns = "org_abc123/"
        await bao_client._kv_write("config/data/mykey", {"value": "ns-val"}, namespace=ns)
        call_headers = mock_http.request.call_args.kwargs.get("headers", {})
        assert call_headers.get("X-Vault-Namespace") == ns


# ═══════════════════════════════════════════════════════════════════════════════
# KV v2 — list
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestKvList:
    """``_kv_list`` — listing keys at a metadata path."""

    @pytest.mark.asyncio
    async def test_list_keys(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Listing returns the key names from the response."""
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"keys": ["key1", "key2", "key3"]}},
        )
        keys = await bao_client._kv_list("config/metadata/")
        assert keys == ["key1", "key2", "key3"]

    @pytest.mark.asyncio
    async def test_list_empty_path(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """An empty path returns an empty list."""
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"keys": []}},
        )
        keys = await bao_client._kv_list("config/metadata/")
        assert keys == []

    @pytest.mark.asyncio
    async def test_list_missing_path_raises(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Listing a non-existent path raises ``OpenBaoSecretNotFoundError``."""
        mock_http.request.return_value = _make_response(
            404,
            {"errors": ["no keys found"]},
        )
        with pytest.raises(OpenBaoSecretNotFoundError):
            await bao_client._kv_list("config/metadata/missing/")

    @pytest.mark.asyncio
    async def test_list_uses_list_method(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Listing uses the HTTP LIST method."""
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"keys": ["k1"]}},
        )
        await bao_client._kv_list("config/metadata/")
        assert mock_http.request.call_args[0][0] == "LIST"


# ═══════════════════════════════════════════════════════════════════════════════
# KV v2 — delete
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestKvDelete:
    """``_kv_delete`` — deleting secrets from the KV v2 engine."""

    @pytest.mark.asyncio
    async def test_delete_existing_key(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Deleting a key sends a DELETE request."""
        mock_http.request.return_value = _make_response(204)
        await bao_client._kv_delete("config/data/mykey")
        assert mock_http.request.call_args[0][0] == "DELETE"

    @pytest.mark.asyncio
    async def test_delete_missing_key_raises(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Deleting a missing key raises ``OpenBaoSecretNotFoundError``."""
        mock_http.request.return_value = _make_response(
            404,
            {"errors": ["not found"]},
        )
        with pytest.raises(OpenBaoSecretNotFoundError):
            await bao_client._kv_delete("config/data/missing")

    @pytest.mark.asyncio
    async def test_delete_with_namespace(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Delete within a namespace sends the header."""
        ns = "org_abc123/"
        await bao_client._kv_delete("config/data/mykey", namespace=ns)
        call_headers = mock_http.request.call_args.kwargs.get("headers", {})
        assert call_headers.get("X-Vault-Namespace") == ns


# ═══════════════════════════════════════════════════════════════════════════════
# System configuration
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestSystemConfig:
    """System-level configuration (``config/data/system``)."""

    @pytest.mark.asyncio
    async def test_read_system_config_returns_dict(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Reading system config returns the data dict."""
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"data": {"OZ_DATABASE_URL": "postgresql://..."}}},
        )
        config = await bao_client.read_system_config()
        assert config == {"OZ_DATABASE_URL": "postgresql://..."}

    @pytest.mark.asyncio
    async def test_read_system_config_empty_when_not_found(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """When the system secret doesn't exist, an empty dict is returned."""
        mock_http.request.return_value = _make_response(
            404,
            {"errors": ["not found"]},
        )
        config = await bao_client.read_system_config()
        assert config == {}

    @pytest.mark.asyncio
    async def test_write_system_config_cas(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Writing system config uses CAS-aware read-modify-write."""
        # First call (read): version 2
        # Second call (write): POST with cas=2
        mock_http.request.side_effect = [
            _make_response(200, {
                "data": {
                    "data": {"OZ_DATABASE_URL": "old"},
                    "metadata": {"version": 2},
                },
            }),
            _make_response(200),
        ]
        await bao_client.write_system_config({"OZ_SECRET_KEY": "new-key"})
        assert mock_http.request.call_count == 2
        write_call = mock_http.request.call_args_list[1]
        assert write_call.kwargs["json"]["options"]["cas"] == 2

    @pytest.mark.asyncio
    async def test_write_system_config_when_missing(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Writing system config when no secret exists yet uses version 0."""
        mock_http.request.side_effect = [
            _make_response(404, {"errors": ["not found"]}),
            _make_response(200),
        ]
        await bao_client.write_system_config({"OZ_DATABASE_URL": "new-url"})
        write_call = mock_http.request.call_args_list[1]
        assert write_call.kwargs["json"]["options"]["cas"] == 0

    @pytest.mark.asyncio
    async def test_write_system_config_merges_keys(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Writing merges new keys into existing config (does not replace)."""
        mock_http.request.side_effect = [
            _make_response(200, {
                "data": {
                    "data": {"OZ_EXISTING_KEY": "existing-value"},
                    "metadata": {"version": 1},
                },
            }),
            _make_response(200),
        ]
        await bao_client.write_system_config({"OZ_NEW_KEY": "new-value"})
        write_call = mock_http.request.call_args_list[1]
        merged = write_call.kwargs["json"]["data"]
        assert merged["OZ_EXISTING_KEY"] == "existing-value"
        assert merged["OZ_NEW_KEY"] == "new-value"


# ═══════════════════════════════════════════════════════════════════════════════
# Org configuration
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestOrgConfig:
    """Per-org configuration — namespace management and config CRUD."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    @pytest.mark.asyncio
    async def test_read_org_config_returns_keys(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Reading org config lists keys and fetches each one."""
        mock_http.request.side_effect = [
            # _kv_list response
            _make_response(200, {"data": {"keys": ["llm_api_key", "graph_backend"]}}),
            # _kv_read for llm_api_key
            _make_response(200, {"data": {"data": {"value": "sk-123"}}}),
            # _kv_read for graph_backend
            _make_response(200, {"data": {"data": {"value": "surrealdb"}}}),
        ]
        config = await bao_client.read_org_config(self.ORG_ID)
        assert config == {"llm_api_key": "sk-123", "graph_backend": "surrealdb"}

    @pytest.mark.asyncio
    async def test_read_org_config_empty_when_no_config(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """When no org config exists, an empty dict is returned."""
        mock_http.request.return_value = _make_response(
            404,
            {"errors": ["not found"]},
        )
        config = await bao_client.read_org_config(self.ORG_ID)
        assert config == {}

    @pytest.mark.asyncio
    async def test_read_org_config_skips_failed_reads(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """If a key disappears between list and read, it's skipped."""
        mock_http.request.side_effect = [
            _make_response(200, {"data": {"keys": ["key_a", "key_b"]}}),
            _make_response(200, {"data": {"data": {"value": "val_a"}}}),
            _make_response(404, {"errors": ["not found"]}),
        ]
        config = await bao_client.read_org_config(self.ORG_ID)
        assert config == {"key_a": "val_a"}

    @pytest.mark.asyncio
    async def test_read_org_config_uses_correct_namespace(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """The org namespace is correctly constructed."""
        mock_http.request.side_effect = [
            _make_response(200, {"data": {"keys": []}}),
        ]
        await bao_client.read_org_config(self.ORG_ID)
        list_call_headers = mock_http.request.call_args.kwargs.get("headers", {})
        expected_ns = f"system/{ORG_NAMESPACE_PREFIX}{self.ORG_ID}/"
        assert list_call_headers.get("X-Vault-Namespace") == expected_ns

    @pytest.mark.asyncio
    async def test_write_org_config_writes_each_key(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Writing org config sends one POST per key."""
        mock_http.request.return_value = _make_response(200)
        await bao_client.write_org_config(
            self.ORG_ID,
            {"llm_api_key": "sk-123", "graph_backend": "surrealdb"},
        )
        assert mock_http.request.call_count == 2

    @pytest.mark.asyncio
    async def test_write_org_config_deletes_none_values(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """A ``None`` value triggers a DELETE instead of a POST."""
        mock_http.request.return_value = _make_response(204)
        await bao_client.write_org_config(self.ORG_ID, {"llm_api_key": None})
        assert mock_http.request.call_args[0][0] == "DELETE"

    @pytest.mark.asyncio
    async def test_create_org_namespace_creates_and_enables_kv(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Creating an org namespace creates the namespace and enables KV v2."""
        # create_namespace → POST to sys/namespaces/org_<uuid> → 204
        # enable_kv_v2 → POST to sys/mounts/config → 204
        mock_http.post.return_value = _make_response(204)
        mock_http.request.return_value = _make_response(200)  # not used in these paths

        await bao_client.create_org_namespace(self.ORG_ID)

        # The namespace endpoint is called directly via _http.post (not _request)
        assert mock_http.post.call_count == 2  # 1 for create_namespace, 1 for enable_kv_v2

    @pytest.mark.asyncio
    async def test_delete_org_namespace(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Deleting an org namespace sends a DELETE to sys/namespaces."""
        mock_http.request.return_value = _make_response(204)
        await bao_client.delete_org_namespace(self.ORG_ID)
        assert mock_http.request.call_args[0][0] == "DELETE"
        assert f"sys/namespaces/{ORG_NAMESPACE_PREFIX}{self.ORG_ID}" in mock_http.request.call_args[0][1]

    def test_org_ns_static_method(self) -> None:
        """``_org_ns`` builds the correct namespace path."""
        org_id = UUID("00000000-0000-0000-0000-000000000001")
        ns = OpenBaoClient._org_ns(org_id, parent="system/")
        assert ns == f"system/{ORG_NAMESPACE_PREFIX}{org_id}/"

    def test_org_ns_no_parent(self) -> None:
        """``_org_ns`` with empty parent returns just the prefix + id."""
        org_id = UUID("00000000-0000-0000-0000-000000000002")
        ns = OpenBaoClient._org_ns(org_id, parent="")
        assert ns == f"{ORG_NAMESPACE_PREFIX}{org_id}/"


# ═══════════════════════════════════════════════════════════════════════════════
# Namespace management
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestNamespaceManagement:
    """Raw namespace create / delete operations."""

    @pytest.mark.asyncio
    async def test_create_namespace_success(self, bao_client: OpenBaoClient, mock_http: AsyncMock) -> None:
        """Creating a namespace sends a POST to ``sys/namespaces/<name>``."""
        mock_http.post.return_value = _make_response(204)
        await bao_client.create_namespace("org_abc123", parent="system/")
        mock_http.post.assert_awaited_once()
        assert "/v1/sys/namespaces/org_abc123" in mock_http.post.call_args[0][0]

    @pytest.mark.asyncio
    async def test_create_namespace_already_exists(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """HTTP 400 with \"already exists\" is silently ignored."""
        mock_http.post.return_value = _make_response(400, {"errors": ["already exists"]})
        await bao_client.create_namespace("org_existing")
        # Should not raise

    @pytest.mark.asyncio
    async def test_create_namespace_all_400_is_noop(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """The code treats ALL 400 as \"already exists\" — no-op."""
        mock_http.post.return_value = _make_response(400, {"errors": ["bad request"]})
        await bao_client.create_namespace("org_invalid")
        # Should not raise — 400 is silently ignored (idempotent)

    @pytest.mark.asyncio
    async def test_create_namespace_other_errors_propagate(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Non-400 errors still propagate."""
        mock_http.post.return_value = _make_response(403, {"errors": ["forbidden"]})
        with pytest.raises(OpenBaoAuthError):
            await bao_client.create_namespace("org_invalid")

    @pytest.mark.asyncio
    async def test_create_namespace_no_http_client(self) -> None:
        """Without an HTTP client, create raises immediately."""
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )
        with pytest.raises(OpenBaoConnectionError, match="not initialised"):
            await client.create_namespace("org_abc123")

    @pytest.mark.asyncio
    async def test_delete_namespace(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Deleting a namespace sends a DELETE request."""
        mock_http.request.return_value = _make_response(204)
        await bao_client.delete_namespace("org_abc123")
        assert mock_http.request.call_args[0][0] == "DELETE"
        assert "sys/namespaces/org_abc123" in mock_http.request.call_args[0][1]


# ═══════════════════════════════════════════════════════════════════════════════
# Enable engines
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestEnableEngines:
    """Enabling KV v2 and Transit engines."""

    @pytest.mark.asyncio
    async def test_enable_kv_v2_success(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Enabling KV v2 sends a POST to ``sys/mounts/<path>``."""
        mock_http.post.return_value = _make_response(204)
        await bao_client.enable_kv_v2("config")
        mock_http.post.assert_awaited_once()
        assert "/v1/sys/mounts/config" in mock_http.post.call_args[0][0]

    @pytest.mark.asyncio
    async def test_enable_kv_v2_already_exists(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """HTTP 400 with \"already in use\" is silently ignored."""
        mock_http.post.return_value = _make_response(400, {"errors": ["path already in use"]})
        await bao_client.enable_kv_v2("config")
        # Should not raise

    @pytest.mark.asyncio
    async def test_enable_kv_v2_no_http_client(self) -> None:
        """Without an HTTP client, enable raises immediately."""
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )
        with pytest.raises(OpenBaoConnectionError, match="not initialised"):
            await client.enable_kv_v2("config")

    @pytest.mark.asyncio
    async def test_enable_transit_engine_success(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Enabling Transit sends a POST with type ``transit``."""
        mock_http.post.return_value = _make_response(204)
        await bao_client.enable_transit_engine("transit")
        mock_http.post.assert_awaited_once()
        assert mock_http.post.call_args.kwargs["json"]["type"] == "transit"

    @pytest.mark.asyncio
    async def test_enable_transit_engine_already_exists(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """HTTP 400 on enabling transit is silently ignored."""
        mock_http.post.return_value = _make_response(400)
        await bao_client.enable_transit_engine("transit")
        # Should not raise

    @pytest.mark.asyncio
    async def test_enable_transit_engine_no_http_client(self) -> None:
        """Without an HTTP client, enable raises immediately."""
        client = OpenBaoClient(
            "http://localhost:8200",
            "role-id",
            "secret-id",
            root_token_path=None,
        )
        with pytest.raises(OpenBaoConnectionError, match="not initialised"):
            await client.enable_transit_engine("transit")


# ═══════════════════════════════════════════════════════════════════════════════
# Transit engine (encryption-as-a-service)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestTransitEngine:
    """Encrypt, decrypt, create keys, rotate, rewrap."""

    PLAINTEXT = "hello-world"

    @pytest.mark.asyncio
    async def test_encrypt_data(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Encrypting data sends base64-encoded plaintext."""
        import base64

        mock_http.request.return_value = _make_response(
            200,
            {"data": {"ciphertext": "vault:v1:abc123"}},
        )
        ct = await bao_client.encrypt_data("my-key", self.PLAINTEXT)
        assert ct == "vault:v1:abc123"

        call_json = mock_http.request.call_args.kwargs.get("json", {})
        assert call_json["plaintext"] == base64.b64encode(self.PLAINTEXT.encode()).decode()

    @pytest.mark.asyncio
    async def test_decrypt_data(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Decrypting data returns decoded plaintext."""
        import base64

        pt_b64 = base64.b64encode(self.PLAINTEXT.encode()).decode()
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"plaintext": pt_b64}},
        )
        pt = await bao_client.decrypt_data("my-key", "vault:v1:abc123")
        assert pt == self.PLAINTEXT

    @pytest.mark.asyncio
    async def test_encrypt_decrypt_round_trip(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Encrypt then decrypt returns the original plaintext."""
        import base64

        pt_b64 = base64.b64encode(self.PLAINTEXT.encode()).decode()
        # First call = encrypt → returns ciphertext
        # Second call = decrypt → returns base64 plaintext
        mock_http.request.side_effect = [
            _make_response(200, {"data": {"ciphertext": "vault:v2:xyz"}}),
            _make_response(200, {"data": {"plaintext": pt_b64}}),
        ]
        ct = await bao_client.encrypt_data("my-key", self.PLAINTEXT)
        pt = await bao_client.decrypt_data("my-key", ct)
        assert pt == self.PLAINTEXT

    @pytest.mark.asyncio
    async def test_encrypt_with_context(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Encryption with AAD context passes the context parameter."""
        import base64

        mock_http.request.return_value = _make_response(
            200,
            {"data": {"ciphertext": "vault:v1:with-ctx"}},
        )
        await bao_client.encrypt_data("my-key", self.PLAINTEXT, context="user-abc")
        call_json = mock_http.request.call_args.kwargs.get("json", {})
        assert "context" in call_json
        assert call_json["context"] == base64.b64encode(b"user-abc").decode()

    @pytest.mark.asyncio
    async def test_decrypt_with_context(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Decryption with AAD context passes the context parameter."""
        import base64

        mock_http.request.return_value = _make_response(
            200,
            {"data": {"plaintext": base64.b64encode(b"hello").decode()}},
        )
        await bao_client.decrypt_data("my-key", "vault:v1:ct", context="user-abc")
        call_json = mock_http.request.call_args.kwargs.get("json", {})
        assert "context" in call_json
        assert call_json["context"] == base64.b64encode(b"user-abc").decode()

    @pytest.mark.asyncio
    async def test_encrypt_missing_key_raises(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Encrypting with a non-existent key raises ``OpenBaoSecretNotFoundError``."""
        mock_http.request.return_value = _make_response(
            404,
            {"errors": ["key not found"]},
        )
        with pytest.raises(OpenBaoSecretNotFoundError):
            await bao_client.encrypt_data("missing-key", "data")

    @pytest.mark.asyncio
    async def test_decrypt_invalid_ciphertext_raises(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Decrypting invalid ciphertext raises ``OpenBaoError``."""
        mock_http.request.return_value = _make_response(
            400,
            {"errors": ["invalid ciphertext"]},
        )
        with pytest.raises(OpenBaoError):
            await bao_client.decrypt_data("my-key", "invalid-ciphertext")

    @pytest.mark.asyncio
    async def test_create_encryption_key(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Creating an encryption key sends a POST to ``<mount>/keys/<name>``."""
        mock_http.request.return_value = _make_response(204)
        await bao_client.create_encryption_key("my-key", key_type="aes256-gcm96")
        assert mock_http.request.call_args[0][0] == "POST"
        assert "transit/keys/my-key" in mock_http.request.call_args[0][1]

    @pytest.mark.asyncio
    async def test_create_encryption_key_already_exists(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Creating a key that already exists is silently ignored."""
        mock_http.request.return_value = _make_response(
            400,
            {"errors": ["key already exists"]},
        )
        await bao_client.create_encryption_key("existing-key")
        # Should not raise

    @pytest.mark.asyncio
    async def test_rotate_encryption_key(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Rotating a key sends a POST to ``<mount>/keys/<name>/rotate``."""
        mock_http.request.return_value = _make_response(204)
        await bao_client.rotate_encryption_key("my-key")
        assert "transit/keys/my-key/rotate" in mock_http.request.call_args[0][1]

    @pytest.mark.asyncio
    async def test_rewrap_data(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Rewrap sends ciphertext and returns new ciphertext."""
        mock_http.request.return_value = _make_response(
            200,
            {"data": {"ciphertext": "vault:v2:rewrapped"}},
        )
        ct = await bao_client.rewrap_data("my-key", "vault:v1:old")
        assert ct == "vault:v2:rewrapped"
        call_json = mock_http.request.call_args.kwargs.get("json", {})
        assert call_json["ciphertext"] == "vault:v1:old"

    @pytest.mark.asyncio
    async def test_rewrap_with_context(
        self,
        bao_client: OpenBaoClient,
        mock_http: AsyncMock,
    ) -> None:
        """Rewrap with AAD context passes the context parameter."""
        import base64

        mock_http.request.return_value = _make_response(
            200,
            {"data": {"ciphertext": "vault:v2:wrapped"}},
        )
        await bao_client.rewrap_data("my-key", "vault:v1:old", context="org-42")
        call_json = mock_http.request.call_args.kwargs.get("json", {})
        assert "context" in call_json
        assert call_json["context"] == base64.b64encode(b"org-42").decode()


# ═══════════════════════════════════════════════════════════════════════════════
# Context manager (__aenter__ / __aexit__)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestContextManager:
    """``__aenter__`` / ``__aexit__`` lifecycle."""

    @pytest.mark.asyncio
    async def test_aenter_creates_http_and_authenticates(self) -> None:
        """Entering the context creates an HTTP client and authenticates."""
        with (
            patch("httpx.AsyncClient") as mock_http_cls,
            patch("builtins.open", side_effect=OSError("no root token")),
        ):
            mock_instance = AsyncMock()
            mock_http_cls.return_value = mock_instance
            mock_instance.post.return_value = _make_response(
                200,
                {"auth": {"client_token": "s.new-token", "lease_duration": 3600}},
            )

            async with OpenBaoClient(
                "http://localhost:8200",
                "role-id",
                "secret-id",
                timeout=5.0,
                namespace="system/",
            ) as client:
                assert client._client_token == "s.new-token"
                mock_http_cls.assert_called_once_with(
                    base_url="http://localhost:8200",
                    timeout=5.0,
                    http2=True,
                )

    @pytest.mark.asyncio
    async def test_aexit_closes_http(self) -> None:
        """Exiting the context closes the HTTP client."""
        with (
            patch("httpx.AsyncClient") as mock_http_cls,
            patch("builtins.open", side_effect=OSError("no root token")),
        ):
            mock_instance = AsyncMock()
            mock_http_cls.return_value = mock_instance
            mock_instance.post.return_value = _make_response(
                200,
                {"auth": {"client_token": "s.t", "lease_duration": 3600}},
            )

            async with OpenBaoClient(
                "http://localhost:8200",
                "role-id",
                "secret-id",
            ):
                pass

            mock_instance.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aenter_raises_on_auth_failure(self) -> None:
        """If authentication fails, the context manager propagates the error."""
        with (
            patch("httpx.AsyncClient") as mock_http_cls,
            patch("builtins.open", side_effect=OSError("no root token")),
        ):
            mock_instance = AsyncMock()
            mock_http_cls.return_value = mock_instance
            mock_instance.post.return_value = _make_response(
                401,
                {"errors": ["permission denied"]},
            )

            with pytest.raises(OpenBaoAuthError):
                async with OpenBaoClient(
                    "http://localhost:8200",
                    "bad-role",
                    "bad-secret",
                ):
                    pass  # pragma: no cover


# ═══════════════════════════════════════════════════════════════════════════════
# Root token loading
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestRootTokenLoading:
    """Root token file read during ``__aenter__``."""

    @pytest.mark.asyncio
    async def test_loads_root_token_from_file(self) -> None:
        """When the root token file exists, it is read and stored."""
        with (
            patch("httpx.AsyncClient") as mock_http_cls,
            patch("builtins.open") as mock_open,
        ):
            mock_instance = AsyncMock()
            mock_http_cls.return_value = mock_instance
            mock_instance.post.return_value = _make_response(
                200,
                {"auth": {"client_token": "s.approle", "lease_duration": 3600}},
            )
            # Simulate root token file
            mock_file = MagicMock()
            mock_file.read.return_value = "s.root-token-value\n"
            mock_open.return_value.__enter__.return_value = mock_file

            async with OpenBaoClient(
                "http://localhost:8200",
                "role-id",
                "secret-id",
                root_token_path="/tmp/root-token",
            ) as client:
                assert client._root_token == "s.root-token-value"
                mock_open.assert_called_once_with("/tmp/root-token")

    @pytest.mark.asyncio
    async def test_root_token_file_missing_is_ignored(self) -> None:
        """When the root token file is missing, the client still works."""
        with (
            patch("httpx.AsyncClient") as mock_http_cls,
            patch("builtins.open", side_effect=OSError("file not found")),
        ):
            mock_instance = AsyncMock()
            mock_http_cls.return_value = mock_instance
            mock_instance.post.return_value = _make_response(
                200,
                {"auth": {"client_token": "s.approle", "lease_duration": 3600}},
            )

            async with OpenBaoClient(
                "http://localhost:8200",
                "role-id",
                "secret-id",
            ) as client:
                assert client._root_token is None


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestOpenBaoConstants:
    """Module-level constants are correctly defined."""

    def test_system_key_mapping_has_expected_keys(self) -> None:
        """All expected OZ_ keys are present in the mapping."""
        assert "OZ_DATABASE_URL" in SYSTEM_KEY_MAPPING
        assert "OZ_REDIS_URL" in SYSTEM_KEY_MAPPING
        assert "OZ_SECRET_KEY" in SYSTEM_KEY_MAPPING
        assert "OZ_WEBHOOK_SIGNING_SECRET" in SYSTEM_KEY_MAPPING

    def test_kv_mount_default(self) -> None:
        assert KV_MOUNT == "config"

    def test_org_namespace_prefix(self) -> None:
        assert ORG_NAMESPACE_PREFIX == "org_"

    def test_system_key_mapping_is_dict(self) -> None:
        assert isinstance(SYSTEM_KEY_MAPPING, dict)
        # Every key maps to the same value (OZ_ prefix transformation)
        for k, v in SYSTEM_KEY_MAPPING.items():
            assert k == v  # all are identity mappings
