"""Graph service — business logic for knowledge-graph query operations.

This service wraps a ``GraphBackend`` implementation (e.g.
``PostgresGraphBackend`` or legacy ``FalkorDBBackend``) to provide
a clean service-layer interface for the graph query endpoints.

Every method enforces org_id and project_id isolation. All methods gracefully
degrade when no graph backend is available — returning empty results rather
than erroring.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.exceptions import EntityNotFoundError
from packages.graphiti_client.interface import GraphBackend

logger = logging.getLogger(__name__)


class GraphService:
    """Service layer for knowledge-graph query operations.

    Args:
        graph_backend: An initialised ``GraphBackend`` implementation
            (e.g. ``FalkorDBBackend``).  May be ``None`` if the graph
            backend is not available — all methods gracefully return
            empty results.
    """

    def __init__(self, graph_backend: GraphBackend | None = None) -> None:
        self._backend = graph_backend

    # ── Public API ──────────────────────────────────────────────────────────────

    async def get_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List entity nodes with optional type filter and cursor pagination.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID to scope the query.
            entity_type: Optional filter by entity type.
            limit: Maximum results per page (max 200).
            cursor: Opaque cursor for pagination.

        Returns:
            A dict with ``items``, ``next_cursor``, and ``has_more`` keys.
            Returns empty page when graph backend is unavailable.
        """
        if self._backend is None:
            logger.debug(
                "graph_service.backend_unavailable", extra={"operation": "get_entities"}
            )
            return {"items": [], "next_cursor": None, "has_more": False}

        return await self._backend.list_entities(
            org_id=org_id,
            project_id=project_id,
            entity_type=entity_type,
            limit=min(limit, 200),
            cursor=cursor,
        )

    async def get_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
    ) -> dict[str, Any]:
        """Get a single entity node with all its incident edges.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID to scope the query.
            entity_id: The UUID of the entity to fetch.

        Returns:
            A dict with ``node`` and ``edges`` keys.

        Raises:
            EntityNotFoundError: If the entity does not exist.
        """
        if self._backend is None:
            logger.debug(
                "graph_service.backend_unavailable",
                extra={"operation": "get_entity", "entity_id": str(entity_id)},
            )
            raise EntityNotFoundError(
                message=f"Entity {entity_id} not found — graph backend is not available.",
                detail={"entity_id": str(entity_id)},
            )

        result = await self._backend.get_entity_with_edges(
            org_id=org_id, project_id=project_id, entity_id=entity_id
        )
        if result is None:
            raise EntityNotFoundError(
                message=f"Entity {entity_id} not found in the knowledge graph.",
                detail={"entity_id": str(entity_id), "org_id": str(org_id)},
            )

        return result

    async def delete_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
    ) -> bool:
        """Delete an entity node from the knowledge graph.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID to scope the query.
            entity_id: The UUID of the entity to delete.

        Returns:
            ``True`` if deleted, ``False`` if not found.

        Raises:
            ExternalServiceError: If the graph operation fails.
        """
        if self._backend is None:
            logger.debug(
                "graph_service.backend_unavailable",
                extra={"operation": "delete_entity", "entity_id": str(entity_id)},
            )
            return False

        return await self._backend.delete_entity(
            org_id=org_id, project_id=project_id, entity_id=entity_id
        )

    async def get_edges(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        subject_id: UUID | None = None,
        predicate: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List relationship edges with optional filters.

        If ``subject_id`` is provided, lists edges for that specific entity.
        Otherwise, returns empty (scoped entity-edge listing is
        entity-specific; global edge listing is not supported at the
        service layer — use the entity-level edge endpoint instead).

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID to scope the query.
            subject_id: Optional filter by source entity UUID.
            predicate: Optional filter by edge label.
            limit: Maximum results per page.
            cursor: Opaque cursor for pagination.

        Returns:
            A dict with ``items``, ``next_cursor``, and ``has_more`` keys.
        """
        if self._backend is None:
            logger.debug(
                "graph_service.backend_unavailable", extra={"operation": "get_edges"}
            )
            return {"items": [], "next_cursor": None, "has_more": False}

        if subject_id is not None:
            return await self._backend.list_entity_edges(
                org_id=org_id,
                project_id=project_id,
                entity_id=subject_id,
                predicate=predicate,
                limit=min(limit, 200),
                cursor=cursor,
            )

        # Global edge listing is not supported without a subject_id.
        logger.warning(
            "graph_service.get_edges_without_subject",
            extra={"org_id": str(org_id)},
        )
        return {"items": [], "next_cursor": None, "has_more": False}

    async def get_communities(
        self,
        org_id: UUID,
        project_id: UUID,
    ) -> list[dict[str, Any]]:
        """List community summary nodes.

        Communities are created by the scheduled ``summarise_community`` ARQ
        worker, which runs Label Propagation on the entity graph and stores
        community entities in ``graph_entities`` with ``entity_type='community'``.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID to scope the query.

        Returns:
            A list of community dicts with ``id``, ``name``, ``summary``,
            ``member_count``, and ``created_at`` keys.  Returns an empty list
            when the graph backend is unavailable or no communities exist yet.
        """
        if self._backend is None:
            logger.debug(
                "graph_service.backend_unavailable",
                extra={"operation": "get_communities"},
            )
            return []

        result = await self._backend.list_entities(
            org_id=org_id,
            project_id=project_id,
            entity_type="community",
            limit=200,
        )
        items: list[dict[str, Any]] = result.get("items", [])
        # member_count is stored in attributes at creation time
        for item in items:
            attrs = item.get("attributes") or {}
            item["member_count"] = attrs.get("member_count", 0)
        return items
