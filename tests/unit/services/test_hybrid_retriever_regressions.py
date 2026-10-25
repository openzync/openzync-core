"""Unit tests: hybrid-retrieval regression paths (W2 + graph merge + sort).

- W2: ``hybrid_search`` with ``session_factory=None`` returns the same
  results as with a sibling-session factory (no session-concurrency error).
- Graph merge: backend items missing ``id``/``distance`` are skipped,
  search still succeeds.
- ``sort=recent`` with mixed datetime / ISO-string / missing dates is
  ordered DESC with no ``TypeError``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from services.hybrid_retriever import HybridRetriever

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

_EMBEDDING = [0.1, 0.2, 0.3]


def _stub_legs(service: HybridRetriever) -> None:
    """Stub all five retrieval legs with deterministic results."""
    service._embed_query = AsyncMock(return_value=list(_EMBEDDING))  # type: ignore[method-assign]
    service._vector_search_episodes = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"id": "v1", "score": 0.9, "created_at": "2026-01-01T00:00:00Z"}]
    )
    service._vector_search_facts = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"id": "vf1", "score": 0.8}]
    )
    service._bm25_search_episodes = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"id": "b1", "score": 0.7}]
    )
    service._bm25_search_facts = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"id": "bf1", "score": 0.6}]
    )
    service._graph_bfs_search = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"id": "e1", "distance": 0.1}]
    )


def _factory(sibling_db: AsyncMock) -> MagicMock:
    """Build a sibling-session factory yielding ``sibling_db``."""
    context = AsyncMock()
    context.__aenter__.return_value = sibling_db
    factory = MagicMock(return_value=context)
    return factory


class TestSessionFactoryParity:
    """W2: sequential (``None``) vs gathered (factory) paths agree."""

    @pytest.mark.asyncio
    async def test_none_factory_results_correct(self) -> None:
        """``session_factory=None`` → correct merged results, no raise."""
        service = HybridRetriever(db=AsyncMock(), org_id=ORG_ID)
        _stub_legs(service)

        result = await service.hybrid_search("q", PROJECT_ID)

        assert {e["id"] for e in result["episodes"]} == {"v1", "b1"}
        assert {f["id"] for f in result["facts"]} == {"vf1", "bf1"}
        assert [e["id"] for e in result["entities"]] == ["e1"]
        assert result["total_items"] == 5

    @pytest.mark.asyncio
    async def test_factory_gathered_path_matches_sequential(self) -> None:
        """Factory path returns the same merged results as the ``None`` path."""
        sibling_db = AsyncMock()
        service = HybridRetriever(
            db=AsyncMock(), org_id=ORG_ID, session_factory=_factory(sibling_db)
        )
        # note: the factory path runs legs on scoped sibling-session copies,
        # so stub at class level — instance stubs would not apply there.
        with (
            patch_set_tenant(),
            patch_release_tenant(),
            patch.object(
                HybridRetriever,
                "_embed_query",
                new=AsyncMock(return_value=list(_EMBEDDING)),
            ),
            patch.object(
                HybridRetriever,
                "_vector_search_episodes",
                new=AsyncMock(
                    return_value=[
                        {
                            "id": "v1",
                            "score": 0.9,
                            "created_at": "2026-01-01T00:00:00Z",
                        }
                    ]
                ),
            ),
            patch.object(
                HybridRetriever,
                "_vector_search_facts",
                new=AsyncMock(return_value=[{"id": "vf1", "score": 0.8}]),
            ),
            patch.object(
                HybridRetriever,
                "_bm25_search_episodes",
                new=AsyncMock(return_value=[{"id": "b1", "score": 0.7}]),
            ),
            patch.object(
                HybridRetriever,
                "_bm25_search_facts",
                new=AsyncMock(return_value=[{"id": "bf1", "score": 0.6}]),
            ),
            patch.object(
                HybridRetriever,
                "_graph_bfs_search",
                new=AsyncMock(return_value=[{"id": "e1", "distance": 0.1}]),
            ),
        ):
            result = await service.hybrid_search("q", PROJECT_ID)

        assert {e["id"] for e in result["episodes"]} == {"v1", "b1"}
        assert {f["id"] for f in result["facts"]} == {"vf1", "bf1"}
        assert [e["id"] for e in result["entities"]] == ["e1"]
        assert result["total_items"] == 5


def patch_set_tenant() -> Any:
    """Patch ``set_tenant_context`` in the retriever namespace."""
    return patch("services.hybrid_retriever.set_tenant_context", new=AsyncMock())


def patch_release_tenant() -> Any:
    """Patch ``release_tenant_context`` in the retriever namespace."""
    return patch("services.hybrid_retriever.release_tenant_context", new=AsyncMock())


class TestGraphMergeSkipsMalformed:
    """Graph merge: items missing ``id``/``distance`` are skipped."""

    def _service(self, items: list[Any]) -> HybridRetriever:
        backend = AsyncMock()
        backend.retrieve_graph.return_value = items
        return HybridRetriever(
            db=AsyncMock(), org_id=ORG_ID, graph_backends=[backend]
        )

    @pytest.mark.asyncio
    async def test_missing_id_and_distance_skipped(self) -> None:
        """ID-less / non-dict items skipped; distance-less sorts last."""
        service = self._service(
            [
                {"name": "no-id"},
                {"id": ""},
                {"id": None},
                "not-a-dict",
                {"id": "good", "distance": 0.2},
                {"id": "no-distance"},
            ]
        )

        result = await service._graph_bfs_search("q", PROJECT_ID)

        assert [i["id"] for i in result] == ["good", "no-distance"]

    @pytest.mark.asyncio
    async def test_non_numeric_distance_sorts_last(self) -> None:
        """Non-numeric distance never raises — sorts after numeric ones."""
        service = self._service(
            [
                {"id": "bad-dist", "distance": "near"},
                {"id": "none-dist", "distance": None},
                {"id": "bool-dist", "distance": True},
                {"id": "close", "distance": 0.1},
            ]
        )

        result = await service._graph_bfs_search("q", PROJECT_ID)

        assert result[0]["id"] == "close"
        assert len(result) == 4


class TestSortRecentMixedDates:
    """``sort=recent`` with mixed shapes → ordered DESC, no ``TypeError``."""

    @pytest.mark.asyncio
    async def test_mixed_created_at_ordered_no_typeerror(self) -> None:
        """Datetime / ISO-string / missing / garbage dates order newest-first."""
        service = HybridRetriever(db=AsyncMock(), org_id=ORG_ID)
        service._embed_query = AsyncMock(return_value=list(_EMBEDDING))  # type: ignore[method-assign]
        service._vector_search_episodes = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {"id": "dt", "score": 0.9,
                 "created_at": datetime(2026, 6, 1, tzinfo=UTC)},
                {"id": "missing", "score": 0.8},
                {"id": "garbage", "score": 0.7, "created_at": "not-a-date"},
            ]
        )
        service._vector_search_facts = AsyncMock(return_value=[])  # type: ignore[method-assign]
        service._bm25_search_episodes = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {"id": "iso", "score": 0.6, "created_at": "2026-03-01T00:00:00Z"},
            ]
        )
        service._bm25_search_facts = AsyncMock(return_value=[])  # type: ignore[method-assign]
        service._graph_bfs_search = AsyncMock(return_value=[])  # type: ignore[method-assign]

        result = await service.hybrid_search("q", PROJECT_ID, sort="recent")

        # Newest first: datetime (Jun) > ISO string (Mar) > missing/garbage.
        assert [e["id"] for e in result["episodes"]][:2] == ["dt", "iso"]
        assert len(result["episodes"]) == 4
