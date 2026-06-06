"""Graph service — business logic for knowledge-graph query operations.

This service wraps ``FalkorDBBackend`` (via ``GraphBackend`` ABC) to provide
a clean service-layer interface for the graph query endpoints.

Every method enforces org_id isolation. All methods gracefully degrade when
the graph backend (Graphiti / FalkorDB) is not available — returning empty
results rather than erroring.
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
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List entity nodes with optional type filter and cursor pagination.

        Args:
            org_id: The authenticated organization UUID.
            entity_type: Optional filter by entity type.
            limit: Maximum results per page (max 200).
            cursor: Opaque cursor for pagination.

        Returns:
            A dict with ``items``, ``next_cursor``, and ``has_more`` keys.
            Returns empty page when graph backend is unavailable.
        """
        if self._backend is None:
            logger.debug("graph_service.backend_unavailable", extra={"operation": "get_entities"})
            return {"items": [], "next_cursor": None, "has_more": False}

        return await self._backend.list_entities(
            org_id=org_id,
            entity_type=entity_type,
            limit=min(limit, 200),
            cursor=cursor,
        )

    async def get_entity(
        self,
        org_id: UUID,
        entity_id: UUID,
    ) -> dict[str, Any]:
        """Get a single entity node with all its incident edges.

        Args:
            org_id: The authenticated organization UUID.
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

        result = await self._backend.get_entity_with_edges(org_id=org_id, entity_id=entity_id)
        if result is None:
            raise EntityNotFoundError(
                message=f"Entity {entity_id} not found in the knowledge graph.",
                detail={"entity_id": str(entity_id), "org_id": str(org_id)},
            )

        return result

    async def delete_entity(
        self,
        org_id: UUID,
        entity_id: UUID,
    ) -> bool:
        """Delete an entity node from the knowledge graph.

        Args:
            org_id: The authenticated organization UUID.
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

        return await self._backend.delete_entity(org_id=org_id, entity_id=entity_id)

    async def get_edges(
        self,
        org_id: UUID,
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
            subject_id: Optional filter by source entity UUID.
            predicate: Optional filter by edge label.
            limit: Maximum results per page.
            cursor: Opaque cursor for pagination.

        Returns:
            A dict with ``items``, ``next_cursor``, and ``has_more`` keys.
        """
        if self._backend is None:
            logger.debug("graph_service.backend_unavailable", extra={"operation": "get_edges"})
            return {"items": [], "next_cursor": None, "has_more": False}

        if subject_id is not None:
            return await self._backend.list_entity_edges(
                org_id=org_id,
                entity_id=subject_id,
                predicate=predicate,
                limit=min(limit, 200),
                cursor=cursor,
            )

        # Global edge listing is not supported without a subject_id.
        # The router should validate this, but we handle gracefully here.
        logger.warning(
            "graph_service.get_edges_without_subject",
            extra={"org_id": str(org_id)},
        )
        return {"items": [], "next_cursor": None, "has_more": False}

    async def get_communities(
        self,
        org_id: UUID,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """List community summary nodes.

        Community detection is a scheduled background task (Phase 2c).
        Until then, this always returns an empty list.

        Args:
            org_id: The authenticated organization UUID.

        Returns:
            An empty list (community detection not yet implemented).
        """
        # TechLead note: Community detection runs as a nightly ARQ task
        # (summarise_community worker). Once implemented, this method will
        # query CommunityNode instances via the graph backend.
        logger.debug("graph_service.communities_not_implemented")
        return []
