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
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest_asyncio.fixture(loop_scope="function")
async def isolated_search_session(
    isolated_auth_client: AsyncClient, isolated_project_id: UUID
) -> str:
    """Create a session for ingest + search tests and return its external ID."""
    resp = await isolated_auth_client.post(
        f"/v1/projects/{isolated_project_id}/sessions",
        json={"external_id": "search-session"},
    )
    assert resp.status_code == 201, f"Session creation failed: {resp.text}"
    return "search-session"


@pytest.fixture(autouse=True)
async def _wire_graph_backend(isolated_app: Any) -> Any:
    """Attach the graph-backend dispatcher to the isolated app.

    ``graph_backend_dispatcher`` is normally set by the FastAPI lifespan,
    which the ``isolated_app`` fixture does not run.  Without it every
    ``/search`` request raises ``AttributeError`` on
    ``app.state.graph_backend_dispatcher``.  ``init_dispatcher()`` only
    registers backend classes — no I/O.
    """
    from core.graph_backend import init_dispatcher

    isolated_app.state.graph_backend_dispatcher = init_dispatcher()
    return isolated_app


@dataclass
class _FakeEmbedResponse:
    embeddings: list[list[float]] | None = None


class _FakeEmbedBackend:
    async def embed(self, texts, model=None) -> _FakeEmbedResponse:
        return _FakeEmbedResponse(
            embeddings=[[0.0] * 1536 for _ in texts]
        )


async def _fake_resolve_backend(provider=None, org_config=None) -> _FakeEmbedBackend:
    return _FakeEmbedBackend()


@pytest.fixture(autouse=True)
def _fake_embedding_backend(monkeypatch) -> None:
    """Stub the embedding backend for the vector search leg.

    ``HybridRetriever._embed_query`` imports ``core.llm.resolve_backend``
    at call time; in the test environment no embedding backend is
    configured, so resolution raises and the whole search 503s.  The fake
    returns a 1536-dim zero vector per text — matching the
    ``episodes.embedding`` ``vector(1536)`` column so the pgvector ``<=>``
    operator works.
    """
    monkeypatch.setattr("core.llm.resolve_backend", _fake_resolve_backend)


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSearchFacts:
    """Tests for search returning facts."""

    @pytest.mark.asyncio
    async def test_search_returns_facts_by_bm25(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_search_session: str,
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
                "session_id": isolated_search_session,
                "facts": [
                    {"subject": "Alice", "predicate": "likes", "object": "hiking"},
                    {"subject": "Bob", "predicate": "enjoys", "object": "mountain biking"},
                ],
            },
        )

        # Search — facts persist synchronously, GIN updates are commit-sync
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

    @pytest.mark.asyncio
    async def test_search_returns_facts_and_episodes_default(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
        isolated_search_session: str,
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
                        "session_id": isolated_search_session,
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
                "session_id": isolated_search_session,
                "facts": [
                    {"subject": "User", "predicate": "likes", "object": "hiking"},
                ],
            },
        )

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
