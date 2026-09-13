"""Unit tests — HybridRetriever threads ``query_time`` into the graph BFS leg.

Phase 3: ``_graph_bfs_search`` forwards the same effective-at instant the
fact legs filter on as ``as_of`` to every backend's ``retrieve_graph``, so
a historical query sees the graph topology exactly as it was at that
instant — superseded edges are not traversed (scenario 15).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from services.hybrid_retriever import HybridRetriever

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
QUERY_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _service(backends: list[AsyncMock] | None = None) -> HybridRetriever:
    return HybridRetriever(
        db=AsyncMock(),
        org_id=ORG_ID,
        graph_backends=backends or [],
    )


class TestGraphBFSSearchAsOf:
    """Scenario 15 — the effective-at instant reaches ``retrieve_graph``."""

    @pytest.mark.asyncio
    async def test_query_time_passed_as_as_of(self) -> None:
        """``query_time`` is forwarded as ``as_of`` to each backend."""
        backend = AsyncMock()
        backend.retrieve_graph = AsyncMock(return_value=[])
        service = _service([backend])

        await service._graph_bfs_search("who reports to Alice", PROJECT_ID, query_time=QUERY_TIME)

        backend.retrieve_graph.assert_awaited_once_with(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            query="who reports to Alice",
            as_of=QUERY_TIME,
        )

    @pytest.mark.asyncio
    async def test_no_query_time_passes_none(self) -> None:
        """Without ``query_time`` the backend resolves as-of to now itself."""
        backend = AsyncMock()
        backend.retrieve_graph = AsyncMock(return_value=[])
        service = _service([backend])

        await service._graph_bfs_search("query", PROJECT_ID)

        backend.retrieve_graph.assert_awaited_once_with(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            query="query",
            as_of=None,
        )

    @pytest.mark.asyncio
    async def test_all_backends_receive_same_instant(self) -> None:
        """Multiple backends all get the identical ``as_of`` value."""
        backends = [AsyncMock(), AsyncMock()]
        for b in backends:
            b.retrieve_graph = AsyncMock(return_value=[])
        service = _service(backends)

        await service._graph_bfs_search("query", PROJECT_ID, query_time=QUERY_TIME)

        for b in backends:
            kwargs = b.retrieve_graph.await_args.kwargs
            assert kwargs["as_of"] == QUERY_TIME
