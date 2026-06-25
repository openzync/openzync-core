"""Unit tests for ContextService — context assembly with mocked retriever."""

from __future__ import annotations

from unittest.mock import AsyncMock, PropertyMock
from uuid import UUID, uuid4

import pytest

from schemas.organization_config import OrgConfigBase
from services.cache_service import CacheService
from services.context_service import ContextService
from services.hybrid_retriever import HybridRetriever


@pytest.mark.unit
class TestContextService:
    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

    @pytest.fixture
    def service(self) -> ContextService:
        mock_db = AsyncMock()
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_graph_backend = AsyncMock()

        svc = ContextService(
            db=mock_db,
            org_id=self.ORG_ID,
            redis=mock_redis,
            graph_backends=[mock_graph_backend],
            org_config=OrgConfigBase(context_cache_ttl=300),
        )
        # Mock the internal retriever to control its output
        mock_retriever = AsyncMock(spec=HybridRetriever)
        mock_retriever.hybrid_search.return_value = {
            "episodes": [],
            "facts": [],
            "entities": [],
            "communities": [],
            "source_counts": {"episodes": {}, "facts": {}, "entities": {}},
            "total_items": 0,
        }
        svc._retriever = mock_retriever
        return svc

    @pytest.mark.asyncio
    async def test_assemble_returns_context_and_metadata(
        self, service: ContextService,
    ) -> None:
        """Assemble returns context string with metadata."""
        project_id = uuid4()
        result = await service.assemble(
            project_id=project_id, query="test query", limit=10, format="text",
        )
        assert "context" in result
        assert "metadata" in result
        assert len(result["context"]) > 0

    @pytest.mark.asyncio
    async def test_assemble_json_format(
        self, service: ContextService,
    ) -> None:
        """Assemble with json format returns JSON-context."""
        project_id = uuid4()
        result = await service.assemble(
            project_id=project_id, query="test query", limit=10, format="json",
        )
        assert "context" in result
        import orjson
        parsed = orjson.loads(result["context"].encode())
        assert isinstance(parsed, dict)

    @pytest.mark.asyncio
    async def test_assemble_includes_source_counts(
        self, service: ContextService,
    ) -> None:
        """Metadata includes source counts after assembly."""
        project_id = uuid4()
        result = await service.assemble(
            project_id=project_id, query="test", limit=10,
        )
        meta = result["metadata"]
        assert "source_counts" in meta
        assert "assembly_time_ms" in meta
