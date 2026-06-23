"""Tests for error mapping and HTTP error handling."""

from __future__ import annotations

import pytest
from httpx import Response

from openzync._errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    ValidationError,
    raise_on_error,
)


class TestErrorMapping:
    """Tests for ``raise_on_error``."""

    def test_401_raises_authentication_error(self):
        with pytest.raises(AuthenticationError):
            raise_on_error(401, {"detail": "Missing auth"})

    def test_404_raises_not_found_error(self):
        with pytest.raises(NotFoundError):
            raise_on_error(404, {"detail": "Not found"})

    def test_429_raises_rate_limit_error(self):
        with pytest.raises(RateLimitError):
            raise_on_error(429, {"detail": "Too many requests"})

    def test_422_raises_validation_error(self):
        with pytest.raises(ValidationError):
            raise_on_error(422, {"detail": "Invalid input"})

    def test_custom_message(self):
        with pytest.raises(NotFoundError) as exc:
            raise_on_error(404, {"detail": "User not found", "user_id": "abc"})
        assert "User not found" in str(exc.value)

    def test_unknown_status_falls_back(self):
        with pytest.raises(Exception):
            raise_on_error(599, {"detail": "Unknown error"})


class TestHTTPRetry:
    """Tests for HTTP retry logic."""

    @pytest.mark.asyncio
    async def test_retry_on_429(self, async_client, mock_http):
        """429 status triggers retry, then succeeds."""
        user_id = "u1"
        # First call returns 429, second returns 200
        call_count = 0

        def handler(request):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return Response(429, json={"detail": "Rate limited"})
            return Response(200, json={
                "id": "u1", "external_id": "alice", "name": "Alice",
                "organization_id": "org-1",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "is_deleted": False,
                "message_count": 0, "fact_count": 0, "session_count": 0,
            })

        mock_http.get("/v1/users/u1").side_effect = handler

        user = await async_client.users.get("u1")
        assert user.name == "Alice"
        assert call_count == 2
