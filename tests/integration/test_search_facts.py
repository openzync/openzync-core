"""Integration tests for hybrid search with facts and graph.

Endpoints under test:

    GET /v1/projects/{project_id}/search  — Hybrid search across project memory

Covers:
    1.  Search returns facts via BM25 after ingestion
    2.  Search filters by type (types=facts)
    3.  Search returns empty for entities (graph backend unavailable)
    4.  Empty query string → 422

Auth strategy:
    Each test creates a fresh org via the admin bootstrap fixture and
    uses ``isolated_auth_client`` (pre-authenticated) for all authenticated calls.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from httpx import AsyncClient


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
class TestSearchFacts:
    """Tests for search returning facts."""

    @pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
    @pytest.mark.asyncio
    async def test_search_returns_facts_by_bm25(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """POST /facts then GET /v1/projects/{project_id}/search?query=hiking → results contain fact."""
        # Create user
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "search_facts_user"},
        )
        assert user_resp.status_code == 201

        # Ingest a fact — project-scoped
        await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts",
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

        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/search",
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
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /search?types=entities → empty (no graph backend)."""
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "search_entities_user"},
        )
        assert user_resp.status_code == 201

        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/search",
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
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /search without query → 422."""
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "search_no_query_user"},
        )
        assert user_resp.status_code == 201

        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/search",
        )
        assert resp.status_code == 422

    @pytest.mark.skip(reason="Needs per-test DB isolation — see TODO")
    @pytest.mark.asyncio
    async def test_search_returns_facts_and_episodes_default(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /v1/projects/{project_id}/search?query=... returns facts and episodes by default."""
        user_resp = await isolated_auth_client.post(
            "/v1/users",
            json={"external_id": "search_default_types_user"},
        )
        assert user_resp.status_code == 201

        # Ingest an episode — project-scoped, multipart form-data
        await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/memory",
            data={
                "data": json.dumps(
                    {
                        "messages": [
                            {"role": "user", "content": "I love mountain hiking in Colorado"},
                        ],
                    }
                ),
            },
        )

        # Ingest a fact — project-scoped
        await isolated_auth_client.post(
            f"/v1/projects/{isolated_project_id}/facts",
            json={
                "facts": [
                    {"subject": "User", "predicate": "likes", "object": "hiking"},
                ],
            },
        )

        import asyncio
        await asyncio.sleep(0.5)

        # Search without types (defaults to episodes,facts)
        resp = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/search",
            params={"query": "hiking"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert len(body["results"]) >= 1, (
            f"Expected at least 1 result, got {len(body['results'])}. "
            f"Body: {body}"
        )
