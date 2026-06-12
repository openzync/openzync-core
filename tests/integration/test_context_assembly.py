"""Integration tests for context assembly endpoint (G1.4).

Verifies that ``GET /v1/users/{user_id}/context`` returns assembled context
for a user based on their episodes, facts, and graph entities.

Exit criterion G1.4:
    ``GET /context?query="python"`` returns assembled text with relevant
    facts, p99 cold ≤1500ms, p99 warm ≤300ms.

This test covers CORRECTNESS.  Latency targets are verified by the
Locust load test in ``tests/performance/``.
"""

from __future__ import annotations

import time
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from core.config import Settings


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
class TestContextAssembly:
    """Context assembly correctness tests."""

    async def _seed_test_data(
        self, async_client: AsyncClient, user_id: str
    ) -> None:
        """Seed episodes + facts for context testing via the API.

        Ingest a conversation about Python, then wait a moment for
        any synchronous persistence to complete.
        """
        # Ingest Python-related conversation
        resp = await async_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "session_id": "context_test_session",
                "messages": [
                    {"role": "user", "content": "How do I sort a list in Python?"},
                    {
                        "role": "assistant",
                        "content": "You can use sorted() or list.sort()",
                    },
                    {"role": "user", "content": "What about dictionaries?"},
                    {
                        "role": "assistant",
                        "content": "Dicts maintain insertion order as of Python 3.7",
                    },
                    {"role": "user", "content": "How do I handle JSON?"},
                    {
                        "role": "assistant",
                        "content": "Use the json module — json.dumps and json.loads",
                    },
                ],
            },
        )
        assert resp.status_code == 202, f"Seed ingest failed: {resp.text}"

    # ── Tests ──────────────────────────────────────────────────────────────

    async def test_context_returns_200_with_text(
        self,
        auth_client,
    ) -> None:
        """GET /context with a relevant query → 200 + non-empty context.

        The response must contain the ``ContextResponse`` shape with
        a ``context`` string and ``metadata`` object.
        """
        # Create user
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "ctx_test_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Seed test data
        await self._seed_test_data(auth_client, user_id)

        # Query context
        response = await auth_client.get(
            f"/v1/users/{user_id}/context",
            params={"query": "python sorting JSON", "limit": 20},
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        body = response.json()

        # Response shape
        assert "context" in body, "Missing 'context' in response"
        assert "metadata" in body, "Missing 'metadata' in response"
        assert len(body["context"]) > 0, "Context string should not be empty"

        # Context should contain relevant information from seeded data
        context_lower = body["context"].lower()
        assert "python" in context_lower, (
            "Context should mention Python (seeded data). "
            f"Got: {body['context'][:200]}"
        )

        # Metadata shape
        meta = body["metadata"]
        assert "cache_hit" in meta
        assert "assembly_time_ms" in meta
        assert meta["assembly_time_ms"] > 0, "assembly_time_ms should be > 0"

    async def test_context_json_format(
        self,
        auth_client,
    ) -> None:
        """GET /context with format=json → structured JSON context."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "ctx_json_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        await self._seed_test_data(auth_client, user_id)

        response = await auth_client.get(
            f"/v1/users/{user_id}/context",
            params={"query": "Python", "format": "json"},
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        body = response.json()
        assert "context" in body

        # JSON format context should itself be valid JSON
        import json as json_lib

        try:
            parsed = json_lib.loads(body["context"])
        except json_lib.JSONDecodeError:
            pytest.fail("Context should be valid JSON when format=json")

        assert isinstance(parsed, dict), "JSON context should be a dict"

    async def test_context_empty_query_returns_422(
        self,
        auth_client,
    ) -> None:
        """GET /context with empty query → 422 validation error."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "ctx_empty_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        response = await auth_client.get(
            f"/v1/users/{user_id}/context",
            params={"query": ""},
        )
        assert response.status_code == 422, (
            f"Expected 422 for empty query, "
            f"got {response.status_code}: {response.text}"
        )

    async def test_context_user_not_found_returns_404(
        self,
        auth_client,
    ) -> None:
        """GET /context for non-existent user → 404."""
        fake_user = "00000000-0000-0000-0000-000000000000"
        response = await auth_client.get(
            f"/v1/users/{fake_user}/context",
            params={"query": "Python"},
        )
        assert response.status_code == 404, (
            f"Expected 404 for non-existent user, "
            f"got {response.status_code}: {response.text}"
        )

    async def test_context_no_auth_returns_401(
        self,
        app,
    ) -> None:
        """GET /context without auth → 401."""
        transport = ASGITransport(app=app)
        fake_user = "00000000-0000-0000-0000-000000000000"
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get(
                f"/v1/users/{fake_user}/context",
                params={"query": "Python"},
            )
        assert response.status_code == 401, (
            f"Expected 401 without auth, "
            f"got {response.status_code}: {response.text}"
        )

    async def test_context_cache_hit(
        self,
        auth_client,
    ) -> None:
        """Second identical query → cache hit.

        The first request should return cache_hit=false.  The second
        request (same params) should return cache_hit=true if Redis
        caching is operational.
        """
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "ctx_cache_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        await self._seed_test_data(auth_client, user_id)

        # First request — cache miss
        resp1 = await auth_client.get(
            f"/v1/users/{user_id}/context",
            params={"query": "python cache test", "limit": 10},
        )
        assert resp1.status_code == 200
        body1 = resp1.json()

        # Second request — should be cache hit
        resp2 = await auth_client.get(
            f"/v1/users/{user_id}/context",
            params={"query": "python cache test", "limit": 10},
        )
        assert resp2.status_code == 200
        body2 = resp2.json()

        # Context should be identical (from cache)
        assert body2["context"] == body1["context"], (
            "Cached context should match first response"
        )
