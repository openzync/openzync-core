"""Unit tests for GraphService — mocked graph backend."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.exceptions import (
    EntityNotFoundError,
    GraphBackendUnavailableError,
    NotFoundError,
)
from services.graph_service import GraphService


@pytest.mark.unit
class TestGraphService:
    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")

    @pytest.mark.asyncio
    async def test_get_entities_no_backend(self) -> None:
        """Without a backend, raises GraphBackendUnavailableError."""
        service = GraphService(graph_backend=None)
        with pytest.raises(GraphBackendUnavailableError):
            await service.get_entities(self.ORG_ID, self.PROJECT_ID)

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
        """Without a backend, raises GraphBackendUnavailableError."""
        service = GraphService(graph_backend=None)

        with pytest.raises(GraphBackendUnavailableError):
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
        """Without a backend, raises GraphBackendUnavailableError."""
        service = GraphService(graph_backend=None)

        with pytest.raises(GraphBackendUnavailableError):
            await service.delete_entity(self.ORG_ID, self.PROJECT_ID, uuid4())

    @pytest.mark.asyncio
    async def test_delete_entity_with_backend(self) -> None:
        """With a backend, delegates delete call."""
        mock_backend = AsyncMock()
        mock_backend.delete_entity.return_value = True
        service = GraphService(graph_backend=mock_backend)

        result = await service.delete_entity(self.ORG_ID, self.PROJECT_ID, uuid4())
        assert result is True

    # ── ensure_user_exists ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_ensure_user_exists_success(self) -> None:
        """With user_repo returning a user, no error is raised."""
        mock_user_repo = AsyncMock()
        mock_user_repo.get_by_uuid.return_value = AsyncMock()  # non-None = found
        service = GraphService(graph_backend=AsyncMock(), user_repo=mock_user_repo)

        # Should not raise
        await service.ensure_user_exists(self.ORG_ID, uuid4())

        mock_user_repo.get_by_uuid.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ensure_user_exists_not_found(self) -> None:
        """With user_repo returning None, raises NotFoundError."""
        mock_user_repo = AsyncMock()
        mock_user_repo.get_by_uuid.return_value = None
        service = GraphService(graph_backend=AsyncMock(), user_repo=mock_user_repo)

        with pytest.raises(NotFoundError):
            await service.ensure_user_exists(self.ORG_ID, uuid4())

    @pytest.mark.asyncio
    async def test_ensure_user_exists_no_repo(self) -> None:
        """Without user_repo, raises NotFoundError."""
        service = GraphService(graph_backend=AsyncMock(), user_repo=None)

        with pytest.raises(NotFoundError):
            await service.ensure_user_exists(self.ORG_ID, uuid4())

    # ── get_entities (session-scoped) ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_entities_with_session_id(self) -> None:
        """When session_id is provided, uses get_entities_for_session."""
        entity_id = uuid4()
        mock_backend = AsyncMock()
        mock_backend.get_entities_for_session.return_value = [
            {"id": entity_id, "name": "SessionEntity", "entity_type": "person", "summary": "A person"},
        ]
        service = GraphService(graph_backend=mock_backend)

        result = await service.get_entities(
            self.ORG_ID, self.PROJECT_ID, session_id=uuid4(),
        )

        assert len(result["items"]) == 1
        assert result["items"][0]["name"] == "SessionEntity"
        assert result["items"][0]["type"] == "person"
        assert result["next_cursor"] is None
        assert result["has_more"] is False
        mock_backend.get_entities_for_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_entities_with_session_id_and_type_filter(self) -> None:
        """session_id + entity_type filter applied client-side."""
        mock_backend = AsyncMock()
        mock_backend.get_entities_for_session.return_value = [
            {"id": uuid4(), "name": "Alice", "entity_type": "person", "summary": ""},
            {"id": uuid4(), "name": "Org", "entity_type": "organization", "summary": ""},
            {"id": uuid4(), "name": "Bob", "entity_type": "person", "summary": ""},
        ]
        service = GraphService(graph_backend=mock_backend)

        result = await service.get_entities(
            self.ORG_ID, self.PROJECT_ID,
            entity_type="person", session_id=uuid4(),
        )

        assert len(result["items"]) == 2
        for item in result["items"]:
            assert item["type"] == "person"

    # ── get_entity ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_entity_success(self) -> None:
        """With backend returning a result, returns the entity with edges."""
        entity_id = uuid4()
        expected = {
            "node": {"id": str(entity_id), "name": "TestEntity"},
            "edges": [{"id": "e1", "source_id": str(entity_id), "target_id": str(uuid4())}],
        }
        mock_backend = AsyncMock()
        mock_backend.get_entity_with_edges.return_value = expected
        service = GraphService(graph_backend=mock_backend)

        result = await service.get_entity(self.ORG_ID, self.PROJECT_ID, entity_id)

        assert result == expected
        mock_backend.get_entity_with_edges.assert_awaited_once_with(
            org_id=self.ORG_ID, project_id=self.PROJECT_ID, entity_id=entity_id,
        )

    # ── delete_entity ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_entity_not_found(self) -> None:
        """With backend returning False, returns False."""
        mock_backend = AsyncMock()
        mock_backend.delete_entity.return_value = False
        service = GraphService(graph_backend=mock_backend)

        result = await service.delete_entity(self.ORG_ID, self.PROJECT_ID, uuid4())

        assert result is False

    # ── get_edges ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_edges_no_backend(self) -> None:
        """Without a backend, raises GraphBackendUnavailableError."""
        service = GraphService(graph_backend=None)

        with pytest.raises(GraphBackendUnavailableError):
            await service.get_edges(self.ORG_ID, self.PROJECT_ID)

    @pytest.mark.asyncio
    async def test_get_edges_with_subject_id(self) -> None:
        """Single subject_id, delegates to list_entity_edges."""
        subject_id = uuid4()
        expected = {
            "items": [{"id": "e1", "source_id": str(subject_id), "target_id": str(uuid4())}],
            "next_cursor": None,
            "has_more": False,
        }
        mock_backend = AsyncMock()
        mock_backend.list_entity_edges.return_value = expected
        service = GraphService(graph_backend=mock_backend)

        result = await service.get_edges(self.ORG_ID, self.PROJECT_ID, subject_id=subject_id)

        assert result == expected
        mock_backend.list_entity_edges.assert_awaited_once_with(
            org_id=self.ORG_ID, project_id=self.PROJECT_ID,
            entity_id=subject_id, predicate=None, limit=50, cursor=None,
        )

    @pytest.mark.asyncio
    async def test_get_edges_with_subject_ids(self) -> None:
        """Multiple subject_ids, fetches in parallel, deduplicates."""
        eid_a = uuid4()
        eid_b = uuid4()
        mock_backend = AsyncMock()
        mock_backend.list_entity_edges.side_effect = [
            {"items": [{"id": "e1"}, {"id": "e2"}], "next_cursor": None, "has_more": False},
            {"items": [{"id": "e3"}], "next_cursor": None, "has_more": False},
        ]
        service = GraphService(graph_backend=mock_backend)

        result = await service.get_edges(
            self.ORG_ID, self.PROJECT_ID, subject_ids=[eid_a, eid_b],
        )

        assert len(result["items"]) == 3
        assert result["next_cursor"] is None
        assert result["has_more"] is False
        # Each subject got a call
        assert mock_backend.list_entity_edges.await_count == 2

    @pytest.mark.asyncio
    async def test_get_edges_with_subject_ids_dedup(self) -> None:
        """Same edge returned by two subjects — only one in result."""
        eid_a = uuid4()
        eid_b = uuid4()
        mock_backend = AsyncMock()
        # Both subjects return a shared edge "e2"
        mock_backend.list_entity_edges.side_effect = [
            {"items": [{"id": "e1"}, {"id": "e2"}], "next_cursor": None, "has_more": False},
            {"items": [{"id": "e2"}, {"id": "e3"}], "next_cursor": None, "has_more": False},
        ]
        service = GraphService(graph_backend=mock_backend)

        result = await service.get_edges(
            self.ORG_ID, self.PROJECT_ID, subject_ids=[eid_a, eid_b],
        )

        # e2 should appear only once
        ids = [e["id"] for e in result["items"]]
        assert ids == ["e1", "e2", "e3"]
        assert len(result["items"]) == 3

    @pytest.mark.asyncio
    async def test_get_edges_without_subject(self, caplog: pytest.LogCaptureFixture) -> None:
        """No subject_id or subject_ids — returns empty result with warning log."""
        mock_backend = AsyncMock()
        service = GraphService(graph_backend=mock_backend)
        caplog.set_level(logging.WARNING)

        result = await service.get_edges(self.ORG_ID, self.PROJECT_ID)

        assert result == {"items": [], "next_cursor": None, "has_more": False}
        assert any(
            "graph_service.get_edges_without_subject" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_get_edges_with_predicate_filter(self) -> None:
        """predicate passed through to backend."""
        subject_id = uuid4()
        mock_backend = AsyncMock()
        mock_backend.list_entity_edges.return_value = {
            "items": [], "next_cursor": None, "has_more": False,
        }
        service = GraphService(graph_backend=mock_backend)

        await service.get_edges(
            self.ORG_ID, self.PROJECT_ID,
            subject_id=subject_id, predicate="knows",
        )

        mock_backend.list_entity_edges.assert_awaited_once_with(
            org_id=self.ORG_ID, project_id=self.PROJECT_ID,
            entity_id=subject_id, predicate="knows", limit=50, cursor=None,
        )

    # ── get_communities ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_communities_no_backend(self) -> None:
        """Without a backend, raises GraphBackendUnavailableError."""
        service = GraphService(graph_backend=None)

        with pytest.raises(GraphBackendUnavailableError):
            await service.get_communities(self.ORG_ID, self.PROJECT_ID)

    @pytest.mark.asyncio
    async def test_get_communities_success(self) -> None:
        """Backend returns community entities with member_count from attributes."""
        community_id = uuid4()
        mock_backend = AsyncMock()
        mock_backend.list_entities.return_value = {
            "items": [
                {
                    "id": community_id,
                    "name": "Tech Cluster",
                    "entity_type": "community",
                    "summary": "A tech community",
                    "attributes": {"member_count": 42},
                },
            ],
            "next_cursor": None,
            "has_more": False,
        }
        service = GraphService(graph_backend=mock_backend)

        result = await service.get_communities(self.ORG_ID, self.PROJECT_ID)

        assert len(result) == 1
        assert result[0]["name"] == "Tech Cluster"
        assert result[0]["member_count"] == 42
        mock_backend.list_entities.assert_awaited_once_with(
            org_id=self.ORG_ID, project_id=self.PROJECT_ID,
            entity_type="community", limit=200,
        )

    @pytest.mark.asyncio
    async def test_get_communities_no_member_count(self) -> None:
        """Community without member_count attribute defaults to 0."""
        mock_backend = AsyncMock()
        mock_backend.list_entities.return_value = {
            "items": [
                {
                    "id": uuid4(),
                    "name": "No Count Community",
                    "entity_type": "community",
                    "summary": "",
                    "attributes": None,
                },
            ],
            "next_cursor": None,
            "has_more": False,
        }
        service = GraphService(graph_backend=mock_backend)

        result = await service.get_communities(self.ORG_ID, self.PROJECT_ID)

        assert len(result) == 1
        assert result[0]["member_count"] == 0
