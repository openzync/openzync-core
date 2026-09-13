"""Integration tests for context assembly (G1.4).

Verifies that ``GET /v1/projects/{project_id}/context`` assembles context
for a project from its episodes, facts, and graph entities.

Exit criterion G1.4:
    ``GET /context?query="python"`` returns assembled text with relevant
    facts, p99 cold ≤1500ms, p99 warm ≤300ms.

This test covers CORRECTNESS.  Latency targets are verified by the
Locust load test in ``tests/performance/``.

Two styles of test live here:

1. **Service-level** (mirroring ``TestContextAssemblyWithReranker`` in
   ``test_reranker_pipeline.py``): the search legs of ``HybridRetriever``
   are mocked so the RRF merge → format → cache pipeline is exercised
   with controlled data.  This avoids depending on a configured embedding
   backend at the HTTP layer.
2. **HTTP-level**: request-validation and auth behaviour against the real
   endpoint, using the per-test isolation fixtures.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.organization_config import OrgConfigBase
from services.context_service import ContextService
from services.hybrid_retriever import HybridRetriever

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


PROJECT_ID = uuid4()


# ═══════════════════════════════════════════════════════════════════════════════
# Shared fixtures (mirrors test_reranker_pipeline.py)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_db_session() -> AsyncMock:
    """Mock DB session — no real queries executed in these tests.

    The individual search methods on ``HybridRetriever`` are mocked
    separately, so the session just needs to be a valid ``AsyncSession``
    for instantiation.

    The ``execute`` → ``scalars`` → ``all`` chain is configured so that
    ``EpisodeBlobRepository`` methods (called during context assembly
    for episodes with results) don't raise ``AttributeError`` on the
    mock coroutine chain.
    """
    from unittest.mock import MagicMock

    mock = AsyncMock(spec=AsyncSession)

    # Wire the execute → scalars → all chain so blob-lookup calls
    # like ``await session.execute(...).scalars().all()`` work.
    # ``scalars()`` is synchronous in SQLAlchemy (not async), so the
    # result wrapper uses ``MagicMock`` (not ``AsyncMock``).
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []

    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars

    mock.execute.return_value = mock_result

    return mock


@pytest.fixture
def sample_search_results() -> dict[str, Any]:
    """Controlled RRF-worthy results for the ``HybridRetriever`` search legs.

    ``episode_vector_results`` and ``episode_bm25_results`` overlap in
    ``id`` so the RRF merge can deduplicate and fuse scores.
    """
    return {
        "episode_vector": [
            {"id": "ep-1", "content": "Python is a programming language", "score": 0.92},
            {"id": "ep-2", "content": "FastAPI is a Python web framework", "score": 0.75},
        ],
        "episode_bm25": [
            {"id": "ep-1", "content": "Python is a programming language", "score": 0.85},
        ],
        "fact_vector": [
            {"id": "fact-1", "content": "Guido van Rossum created Python", "score": 0.88},
        ],
        "fact_bm25": [
            {"id": "fact-1", "content": "Guido van Rossum created Python", "score": 0.80},
        ],
    }


def _build_mocked_retriever(
    db: AsyncSession,
    org_id_val: UUID,
    sample: dict[str, Any],
    *,
    org_config: OrgConfigBase | None = None,
) -> HybridRetriever:
    """Construct a ``HybridRetriever`` with all search legs mocked.

    Each internal search method is replaced with an ``AsyncMock`` that
    returns controlled data, so tests focus on the RRF merge, formatting,
    and caching steps without needing real DB content or an embedding
    backend.

    Args:
        db: A mock async session.
        org_id_val: Organisation UUID (for tenant isolation).
        sample: The controlled search results to return.
        org_config: Optional org-level configuration.

    Returns:
        A ``HybridRetriever`` instance with mocked search legs.
    """
    retriever = HybridRetriever(
        db=db,
        org_id=org_id_val,
        redis=None,
        graph_backends=[],
        org_config=org_config,
        reranker=None,
    )

    retriever._embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])
    retriever._vector_search_episodes = AsyncMock(
        return_value=sample["episode_vector"],
    )
    retriever._vector_search_facts = AsyncMock(
        return_value=sample["fact_vector"],
    )
    retriever._bm25_search_episodes = AsyncMock(
        return_value=sample["episode_bm25"],
    )
    retriever._bm25_search_facts = AsyncMock(
        return_value=sample["fact_bm25"],
    )
    retriever._graph_bfs_search = AsyncMock(return_value=[])

    return retriever


def _build_context_service(
    mock_db: AsyncMock,
    sample: dict[str, Any],
    *,
    org_config: OrgConfigBase | None = None,
    redis: Any = None,
) -> ContextService:
    """Build a ``ContextService`` with a fully mocked retriever."""
    org_config = org_config or OrgConfigBase(reranker_backend=None)
    retriever = _build_mocked_retriever(
        mock_db,
        ORG_ID,
        sample,
        org_config=org_config,
    )
    service = ContextService(
        db=mock_db,
        org_id=ORG_ID,
        redis=redis,
        graph_backends=[],
        org_config=org_config,
    )
    # Replace the internal retriever with our pre-configured one
    service._retriever = retriever
    return service


# ═══════════════════════════════════════════════════════════════════════════════
# Service-level tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextAssembly:
    """Context assembly correctness tests."""

    async def test_context_returns_200_with_text(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
    ) -> None:
        """assemble() with a relevant query → non-empty text context.

        The result must contain the ``context`` string and ``metadata``
        object, and include content from the seeded (mocked) results.
        """
        service = _build_context_service(
            mock_db_session,
            sample_search_results,
            org_config=OrgConfigBase(reranker_backend=None),
        )

        result = await service.assemble(
            project_id=PROJECT_ID,
            query="python sorting JSON",
            limit=20,
            format="text",
        )

        # Response shape
        assert "context" in result, "Missing 'context' in result"
        assert "metadata" in result, "Missing 'metadata' in result"
        assert len(result["context"]) > 0, "Context string should not be empty"

        # Context should contain relevant information from the sample data
        context_lower = result["context"].lower()
        assert "python" in context_lower, (
            "Context should mention Python (seeded data). "
            f"Got: {result['context'][:200]}"
        )

        # Metadata shape
        meta = result["metadata"]
        assert "cache_hit" in meta
        assert "assembly_time_ms" in meta
        assert meta["assembly_time_ms"] > 0, "assembly_time_ms should be > 0"

    async def test_context_json_format(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
    ) -> None:
        """assemble() with format="json" → structured JSON context."""
        import orjson

        service = _build_context_service(
            mock_db_session,
            sample_search_results,
            org_config=OrgConfigBase(reranker_backend=None),
        )

        result = await service.assemble(
            project_id=PROJECT_ID,
            query="Python",
            limit=20,
            format="json",
        )
        assert "context" in result

        # JSON format context should itself be valid JSON
        try:
            parsed = orjson.loads(result["context"].encode())
        except orjson.JSONDecodeError:
            pytest.fail("Context should be valid JSON when format=json")

        assert isinstance(parsed, dict), "JSON context should be a dict"
        assert "episodes" in parsed, "JSON context should have an episodes array"
        assert parsed["episodes"], "Episodes from the sample data should be present"

    async def test_context_cache_hit(
        self,
        mock_db_session: AsyncMock,
        sample_search_results: dict[str, Any],
        redis_client: Any,
    ) -> None:
        """Second identical query → cache hit.

        The first assemble() returns cache_hit=false.  The second (same
        params) returns cache_hit=true when Redis caching is operational.
        """
        org_config = OrgConfigBase(
            reranker_backend=None,
            context_cache_ttl=30,
        )
        service = _build_context_service(
            mock_db_session,
            sample_search_results,
            org_config=org_config,
            redis=redis_client,
        )

        # First request — cache miss
        result1 = await service.assemble(
            project_id=PROJECT_ID,
            query="python cache test",
            limit=10,
            format="text",
        )
        assert result1["metadata"]["cache_hit"] is False

        # Second request — should be cache hit
        result2 = await service.assemble(
            project_id=PROJECT_ID,
            query="python cache test",
            limit=10,
            format="text",
        )
        assert result2["metadata"]["cache_hit"] is True, (
            "Second identical query should be served from cache"
        )

        # Context should be identical (from cache)
        assert result2["context"] == result1["context"], (
            "Cached context should match first response"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP-level tests — validation + auth
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextHttp:
    """HTTP behaviour for ``GET /v1/projects/{project_id}/context``."""

    async def test_context_empty_query_returns_422(
        self,
        isolated_auth_client: AsyncClient,
        isolated_project_id: UUID,
    ) -> None:
        """GET /context with empty query → 422 validation error."""
        response = await isolated_auth_client.get(
            f"/v1/projects/{isolated_project_id}/context",
            params={"query": ""},
        )
        assert response.status_code == 422, (
            f"Expected 422 for empty query, "
            f"got {response.status_code}: {response.text}"
        )

    async def test_context_foreign_project_returns_403(
        self,
        isolated_auth_client: AsyncClient,
    ) -> None:
        """GET /context for a project the API key is not scoped to → 403."""
        foreign_project = "00000000-0000-0000-0000-000000000000"
        response = await isolated_auth_client.get(
            f"/v1/projects/{foreign_project}/context",
            params={"query": "Python"},
        )
        assert response.status_code == 403, (
            f"Expected 403 for foreign project, "
            f"got {response.status_code}: {response.text}"
        )

    async def test_context_no_auth_returns_401(
        self,
        isolated_app: Any,
    ) -> None:
        """GET /context without auth → 401."""
        transport = ASGITransport(app=isolated_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get(
                "/v1/projects/00000000-0000-0000-0000-000000000000/context",
                params={"query": "Python"},
            )
        assert response.status_code == 401, (
            f"Expected 401 without auth, "
            f"got {response.status_code}: {response.text}"
        )
