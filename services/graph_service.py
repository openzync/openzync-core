"""Graph service — business logic for knowledge-graph query operations.

This service wraps a ``GraphBackend`` implementation (typically
``PostgresGraphBackend``) to provide a clean service-layer interface
for the graph query endpoints.

Every method enforces org_id isolation. All methods raise
``GraphBackendUnavailableError`` when no graph backend is available.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.events import EventType
from core.exceptions import (
    EntityNotFoundError,
    GraphBackendUnavailableError,
    NotFoundError,
)
from packages.graph_backend.interface import GraphBackend
from repositories.fact_repository import FactRepository
from repositories.user_repository import UserRepository
from services.webhook_service import WebhookService

logger = logging.getLogger(__name__)


class GraphService:
    """Service layer for knowledge-graph query operations.

    Args:
        graph_backend: An initialised ``GraphBackend`` implementation
            (e.g. ``PostgresGraphBackend``).  May be ``None`` if the
            graph backend is not available — all methods raise
            ``GraphBackendUnavailableError``.
        user_repo: Optional ``UserRepository`` for user existence checks.
            When provided, ``ensure_user_exists`` can be called by
            routers before graph queries.
        fact_repo: Optional ``FactRepository`` for session-scoped entity
            queries. When provided, ``get_entities`` accepts a ``session_id``
            to scope results to entities linked to a specific session.
    """

    def __init__(
        self,
        graph_backend: GraphBackend | None = None,
        user_repo: UserRepository | None = None,
        fact_repo: FactRepository | None = None,
        webhook_service: WebhookService | None = None,
    ) -> None:
        self._backend = graph_backend
        self._user_repo = user_repo
        self._fact_repo = fact_repo
        self._webhook_service = webhook_service

    # ── User validation (moved from router layer) ───────────────────────────────

    async def ensure_user_exists(self, org_id: UUID, user_id: UUID) -> None:
        """Verify the user exists in the organization.

        Args:
            org_id: The authenticated organization UUID.
            user_id: The requested user UUID.

        Raises:
            NotFoundError: If the user does not exist in the organization
                or no user repository is configured.
        """
        if self._user_repo is None:
            raise NotFoundError(
                message=f"User {user_id} not found — user repo not configured.",
                detail={"user_id": str(user_id)},
            )
        user = await self._user_repo.get_by_uuid(org_id, user_id)
        if user is None:
            raise NotFoundError(
                message=f"User {user_id} not found in organization {org_id}",
                detail={"user_id": str(user_id), "org_id": str(org_id)},
            )

    # ── Public API ──────────────────────────────────────────────────────────────

    async def get_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        session_id: UUID | None = None,
    ) -> dict[str, Any]:
        """List entity nodes with optional type filter and cursor pagination.

        When ``session_id`` is provided, entities are scoped to those linked
        to episodes in the specified session.  Cursor pagination is not
        supported for session-scoped queries — all matching entities are
        returned in a single page.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID for intra-org isolation.
            entity_type: Optional filter by entity type.
            limit: Maximum results per page (max 200).
            cursor: Opaque cursor for pagination.
            session_id: Optional session UUID to scope entities.

        Returns:
            A dict with ``items``, ``next_cursor``, and ``has_more`` keys.

        Raises:
            GraphBackendUnavailableError: If the graph backend is not
                available for this organization.
        """
        if self._backend is None:
            raise GraphBackendUnavailableError(
                "Graph backend is not available for this organization.",
                detail={"operation": "get_entities"},
            )

        # Session-scoped query — delegate to graph backend.
        if session_id is not None:
            entities = await self._backend.get_entities_for_session(
                org_id=org_id,
                project_id=project_id,
                session_id=session_id,
            )

            # Apply optional entity_type filter client-side
            if entity_type:
                entities = [e for e in entities if e.get("entity_type") == entity_type]

            return {
                "items": [
                    {
                        "id": str(e["id"]),
                        "name": e["name"],
                        "type": e["entity_type"],
                        "summary": e.get("summary", ""),
                        "metadata": {},
                        "created_at": None,
                    }
                    for e in entities
                ],
                "next_cursor": None,
                "has_more": False,
            }

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
            project_id: The project UUID for intra-org isolation.
            entity_id: The UUID of the entity to fetch.

        Returns:
            A dict with ``node`` and ``edges`` keys.

        Raises:
            GraphBackendUnavailableError: If the graph backend is not
                available for this organization.
            EntityNotFoundError: If the entity does not exist in the
                knowledge graph.
        """
        if self._backend is None:
            raise GraphBackendUnavailableError(
                "Graph backend is not available for this organization.",
                detail={"operation": "get_entity", "entity_id": str(entity_id)},
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
            project_id: The project UUID for intra-org isolation.
            entity_id: The UUID of the entity to delete.

        Returns:
            ``True`` if deleted, ``False`` if not found.

        Raises:
            GraphBackendUnavailableError: If the graph backend is not
                available for this organization.
            ExternalServiceError: If the graph operation fails.
        """
        if self._backend is None:
            raise GraphBackendUnavailableError(
                "Graph backend is not available for this organization.",
                detail={"operation": "delete_entity", "entity_id": str(entity_id)},
            )

        return await self._backend.delete_entity(
            org_id=org_id, project_id=project_id, entity_id=entity_id
        )

    async def get_edges(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        subject_id: UUID | None = None,
        subject_ids: list[UUID] | None = None,
        predicate: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List relationship edges with optional filters.

        If ``subject_id`` is provided, lists edges for that specific entity.
        If ``subject_ids`` is provided, fetches edges for all given entities
        in parallel and deduplicates by ``(sorted source_id, target_id)``.
        Returns empty when neither is provided.

        Args:
            org_id: The authenticated organization UUID.
            project_id: The project UUID for intra-org isolation.
            subject_id: Optional filter by source entity UUID.
            subject_ids: Batch of entity UUIDs to fetch edges for.
            predicate: Optional filter by edge label.
            limit: Maximum results per page (per-subject cap in batch mode).
            cursor: Opaque cursor for pagination.

        Returns:
            A dict with ``items``, ``next_cursor``, and ``has_more`` keys.

        Raises:
            GraphBackendUnavailableError: If the graph backend is not
                available for this organization.
        """
        if self._backend is None:
            raise GraphBackendUnavailableError(
                "Graph backend is not available for this organization.",
                detail={"operation": "get_edges"},
            )

        if subject_ids is not None:
            # Batch: fetch edges for all subjects in parallel, deduplicate
            import asyncio

            per_subject_limit = min(limit, 200)
            results = await asyncio.gather(*[
                self._backend.list_entity_edges(
                    org_id=org_id,
                    project_id=project_id,
                    entity_id=eid,
                    predicate=predicate,
                    limit=per_subject_limit,
                )
                for eid in subject_ids
            ])
            seen: set[str] = set()
            items: list[dict[str, Any]] = []
            for r in results:
                for edge in r.get("items", []):
                    # Dedup by edge id — preserves distinct edges between
                    # the same pair with different types/relationships.
                    key = str(edge.get("id", ""))
                    if key and key not in seen:
                        seen.add(key)
                        items.append(edge)
            return {"items": items, "next_cursor": None, "has_more": False}

        if subject_id is not None:
            return await self._backend.list_entity_edges(
                org_id=org_id,
                project_id=project_id,
                entity_id=subject_id,
                predicate=predicate,
                limit=min(limit, 200),
                cursor=cursor,
            )

        # no subject provided — warn and return empty
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
            project_id: The project UUID for intra-org isolation.

        Returns:
            A list of community dicts with ``id``, ``name``, ``summary``,
            ``member_count``, and ``created_at`` keys.

        Raises:
            GraphBackendUnavailableError: If the graph backend is not
                available for this organization.
        """
        if self._backend is None:
            raise GraphBackendUnavailableError(
                "Graph backend is not available for this organization.",
                detail={"operation": "get_communities"},
            )

        result = await self._backend.list_entities(
            org_id=org_id,
            project_id=project_id,
            entity_type="community",
            limit=200,
        )
        items: list[dict[str, Any]] = result.get("items", [])
        # member_count is stored in attributes at creation time
        for item in items:
            attrs = item["attributes"] if item.get("attributes") is not None else {}
            item["member_count"] = attrs.get("member_count", 0)
        return items
