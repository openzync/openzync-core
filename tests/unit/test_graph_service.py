"""Unit tests for GraphService — mocked graph backend."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.exceptions import EntityNotFoundError
from services.graph_service import GraphService


@pytest.mark.unit
class TestGraphService:
    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")

    @pytest.mark.asyncio
    async def test_get_entities_no_backend(self) -> None:
        """Without a backend, returns empty defaults."""
        service = GraphService(graph_backend=None)
        result = await service.get_entities(self.ORG_ID, self.PROJECT_ID)
        assert result == {"items": [], "next_cursor": None, "has_more": False}

    @pytest.mark.asyncio
    async def test_get_entities_with_backend(self) -> None:
        """With a backend, returns paginated entities."""
        mock_backend = AsyncMock()
        mock_backend.list_entities.return_value = {
            "items": [{"id": str(uuid4()), "name": "Entity1", "type": "Test"}],
            "next_cursor": None,
            "has_more": False,
        }
        service = GraphService(graph_backend=mock_backend)

        result = await service.get_entities(self.ORG_ID, self.PROJECT_ID)
        assert len(result["items"]) == 1
        assert result["items"][0]["name"] == "Entity1"

    @pytest.mark.asyncio
    async def test_get_entity_no_backend_raises(self) -> None:
        """Without a backend, raises EntityNotFoundError."""
        service = GraphService(graph_backend=None)

        with pytest.raises(EntityNotFoundError):
            await service.get_entity(self.ORG_ID, self.PROJECT_ID, uuid4())

    @pytest.mark.asyncio
    async def test_get_entity_with_backend_not_found(self) -> None:
        """With a backend but no entity found, raises."""
        mock_backend = AsyncMock()
        mock_backend.get_entity_with_edges.return_value = None
        service = GraphService(graph_backend=mock_backend)

        with pytest.raises(EntityNotFoundError):
            await service.get_entity(self.ORG_ID, self.PROJECT_ID, uuid4())

    @pytest.mark.asyncio
    async def test_delete_entity_no_backend(self) -> None:
        """Without a backend, returns False."""
        service = GraphService(graph_backend=None)

        result = await service.delete_entity(self.ORG_ID, self.PROJECT_ID, uuid4())
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_entity_with_backend(self) -> None:
        """With a backend, delegates delete call."""
        mock_backend = AsyncMock()
        mock_backend.delete_entity.return_value = True
        service = GraphService(graph_backend=mock_backend)

        result = await service.delete_entity(self.ORG_ID, self.PROJECT_ID, uuid4())
        assert result is True
