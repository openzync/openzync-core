"""Integration tests for API-key authentication middleware.

Every test gets per-test DB isolation via ``isolated_app`` and
``isolated_auth_client`` fixtures from ``conftest.py`` — no state leaks
between tests.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.integration
class TestAuthIntegration:
    """Validate auth middleware behaviour end-to-end."""

    HEALTH_ENDPOINT = "/health"
    # /v1/users is a protected endpoint — requires valid API key.
    PROTECTED_ENDPOINT = "/v1/users"

    @pytest.fixture
    async def anon_client(self, isolated_app: pytest.fixture) -> AsyncClient:
        """Unauthenticated HTTP client backed by the isolated app."""
        transport = ASGITransport(app=isolated_app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    async def test_no_key_returns_200_for_public_endpoint(
        self, anon_client: AsyncClient
    ) -> None:
        """Public endpoints (like health) should work without an API key."""
        resp = await anon_client.get(self.HEALTH_ENDPOINT)
        assert resp.status_code == 200

    async def test_invalid_key_returns_401(self, anon_client: AsyncClient) -> None:
        """A request with a bogus API key should be rejected with 401."""
        anon_client.headers["Authorization"] = "Bearer oz_live_invalidkey_xxxxxxxxxx"
        resp = await anon_client.get(self.PROTECTED_ENDPOINT)
        assert resp.status_code == 401

        # Verify the response body follows RFC 7807
        body = resp.json()
        assert "type" in body
        assert body["status"] == 401

    async def test_missing_auth_header_returns_401(
        self, anon_client: AsyncClient
    ) -> None:
        """A request with no ``Authorization`` header should be rejected."""
        resp = await anon_client.get(self.PROTECTED_ENDPOINT)
        assert resp.status_code == 401

    async def test_valid_key_returns_200(
        self, isolated_auth_client: AsyncClient
    ) -> None:
        """A request carrying a valid API key should succeed."""
        resp = await isolated_auth_client.get(self.HEALTH_ENDPOINT)
        assert resp.status_code == 200

    async def test_malformed_auth_header_returns_401(
        self, anon_client: AsyncClient
    ) -> None:
        """A header that doesn't match ``Bearer <key>`` should be rejected."""
        anon_client.headers["Authorization"] = "Basic not_a_bearer_token"
        resp = await anon_client.get(self.PROTECTED_ENDPOINT)
        assert resp.status_code == 401
