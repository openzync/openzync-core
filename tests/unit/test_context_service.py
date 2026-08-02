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
        mock_retriever._org_config = None
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

    def _build_with_org_config(
        self, org_config: OrgConfigBase | None,
    ) -> ContextService:
        """Construct a ContextService with a given (possibly None) org config."""
        svc = ContextService(
            db=AsyncMock(),
            org_id=self.ORG_ID,
            redis=AsyncMock(),
            org_config=org_config,
        )
        # Swap in a mocked retriever so construction side effects don't matter.
        svc._retriever = AsyncMock(spec=HybridRetriever)
        return svc

    def test_constructs_without_org_config(self) -> None:
        """Bootstrap orgs with no config fall back to the default cache TTL."""
        svc = self._build_with_org_config(None)
        assert svc._cache is not None
        assert svc._cache._default_ttl == 300

    def test_constructs_with_null_context_cache_ttl(self) -> None:
        """Orgs with a null context_cache_ttl fall back to the default TTL."""
        svc = self._build_with_org_config(OrgConfigBase(context_cache_ttl=None))
        assert svc._cache is not None
        assert svc._cache._default_ttl == 300

    def test_cache_service_still_rejects_none_ttl(self) -> None:
        """The CacheService guard is intact — the caller must coalesce."""
        with pytest.raises(ValueError):
            CacheService(AsyncMock(), default_ttl=None)
