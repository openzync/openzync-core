"""Integration tests for hybrid search with facts and graph.

Endpoints under test:

    GET /v1/users/{user_id}/search  — Hybrid search across user memory

Covers:
    1.  Search returns facts via BM25 after ingestion
    2.  Search filters by type (types=facts)
    3.  Search returns empty for entities (graph backend unavailable)
    4.  Empty query string → 422

Auth strategy:
    Each test creates a fresh org via the admin bootstrap fixture and
    uses ``auth_client`` (pre-authenticated) for all authenticated calls.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSearchFacts:
    """Tests for search returning facts."""

    @pytest.mark.asyncio
    async def test_search_returns_facts_by_bm25(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """POST /facts then GET /search?query=hiking → results contain fact."""
        # Create user
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "search_facts_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Ingest a fact
        await auth_client.post(
            f"/v1/users/{user_id}/facts",
            json={
                "facts": [
                    {"subject": "Alice", "predicate": "likes", "object": "hiking"},
                    {"subject": "Bob", "predicate": "enjoys", "object": "mountain biking"},
                ],
            },
        )

        # Search — facts may take a moment to be indexed (GIN is sync)
        import asyncio
        await asyncio.sleep(0.5)

        resp = await auth_client.get(
            f"/v1/users/{user_id}/search",
            params={"query": "hiking", "types": "facts"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "results" in body
        # Should find the hiking fact
        hiking_results = [r for r in body["results"] if "hiking" in r.get("content", "")]
        assert len(hiking_results) >= 1, (
            f"Expected at least 1 fact about hiking, got {len(hiking_results)}. "
            f"Results: {body['results']}"
        )

    @pytest.mark.asyncio
    async def test_search_returns_empty_for_entities(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /search?types=entities → empty (no graph backend)."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "search_entities_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/search",
            params={"query": "test", "types": "entities"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        # Without graph backend, entities should be empty
        assert body["results"] == []

    @pytest.mark.asyncio
    async def test_search_requires_query(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /search without query → 422."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "search_no_query_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        resp = await auth_client.get(
            f"/v1/users/{user_id}/search",
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_search_returns_facts_and_episodes_default(
        self,
        auth_client: AsyncClient,
    ) -> None:
        """GET /search?query=... returns facts and episodes by default."""
        user_resp = await auth_client.post(
            "/v1/users",
            json={"external_id": "search_default_types_user"},
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["id"]

        # Ingest an episode
        await auth_client.post(
            f"/v1/users/{user_id}/memory",
            json={
                "messages": [
                    {"role": "user", "content": "I love mountain hiking in Colorado"},
                ],
            },
        )

        # Ingest a fact
        await auth_client.post(
            f"/v1/users/{user_id}/facts",
            json={
                "facts": [
                    {"subject": "User", "predicate": "likes", "object": "hiking"},
                ],
            },
        )

        import asyncio
        await asyncio.sleep(0.5)

        # Search without types (defaults to episodes,facts)
        resp = await auth_client.get(
            f"/v1/users/{user_id}/search",
            params={"query": "hiking"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert len(body["results"]) >= 1, (
            f"Expected at least 1 result, got {len(body['results'])}. "
            f"Body: {body}"
        )
