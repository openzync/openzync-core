"""Entity repository — interface to the graph backend for entity CRUD.

This repository delegates to a ``GraphBackend`` instance (either
``PostgresGraphBackend`` or ``FalkorDBBackend``) for all entity and
relationship operations.  Gracefully degrades when no backend is
available — all public methods return ``None``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.graphiti_client.backends.postgres import PostgresGraphBackend
from packages.graphiti_client.interface import GraphBackend

logger = logging.getLogger(__name__)


class EntityRepository:
    """Manages entity nodes and relationships in the knowledge graph.

    Delegates all operations to the configured ``GraphBackend``.

    Args:
        db: An async SQLAlchemy session (request-scoped).
        graph_backend: An initialised ``GraphBackend`` instance.  If
            ``None``, all operations gracefully return ``None``.
    """

    def __init__(
        self,
        db: AsyncSession,
        graph_backend: GraphBackend | None = None,
    ) -> None:
        self._db = db
        self._backend = graph_backend or PostgresGraphBackend(db)

    @property
    def is_available(self) -> bool:
        """``True`` when a graph backend is initialised and ready."""
        return self._backend is not None

    # ── Entity CRUD ───────────────────────────────────────────────────────────

    async def upsert_entity(
        self,
        org_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict | None:
        """Create an entity or return existing one by name.

        Uses name-based lookup via ``search_entities``.  If found,
        returns the existing entity.  Otherwise creates a new one.

        Returns ``None`` if the backend is unavailable.
        """
        if self._backend is None:
            return None

        # Check if entity with this name already exists
        existing = await self.get_entity_by_name(org_id, name)
        if existing is not None:
            logger.debug(
                "entity_repository.entity_exists",
                extra={"entity_name": name, "entity_id": existing.get("id")},
            )
            return existing

        # Create new entity
        try:
            entity = await self._backend.create_entity(
                org_id=org_id,
                name=name,
                entity_type=entity_type,
                summary=summary,
            )
            logger.info(
                "entity_repository.entity_created",
                extra={
                    "org_id": str(org_id),
                    "entity_name": name,
                    "entity_type": entity_type,
                    "entity_id": entity.get("id"),
                },
            )
            return entity
        except Exception as exc:
            logger.error(
                "entity_repository.create_failed",
                extra={
                    "org_id": str(org_id),
                    "entity_name": name,
                    "entity_type": entity_type,
                    "error": str(exc),
                },
            )
            return None

    async def get_entity_by_name(
        self,
        org_id: UUID,
        name: str,
    ) -> dict | None:
        """Find an entity by name using fuzzy (contains) matching.

        The LLM often extracts full names ("Alice Johnson") for entities
        but uses shorter variants ("Alice") in relationship references.
        This method uses substring matching to handle these cases.
        """
        if self._backend is None:
            return None

        try:
            results = await self._backend.search_entities(
                org_id=org_id,
                query=name,
                limit=10,
            )
            name_lower = name.lower().strip()
            # First pass: exact match (highest confidence)
            for r in results:
                if r.get("name", "").lower().strip() == name_lower:
                    return r
            # Second pass: contains match (handles "Alice" inside "Alice Johnson")
            for r in results:
                if name_lower in r.get("name", "").lower():
                    return r
            # Third pass: reversed contains (handles "Alice Johnson" searching for "Alice J")
            for r in results:
                if r.get("name", "").lower().strip() in name_lower:
                    return r
            return None
        except Exception as exc:
            logger.warning(
                "entity_repository.search_failed",
                extra={"org_id": str(org_id), "entity_name": name, "error": str(exc)},
            )
            return None

    async def get_entity_by_id(
        self,
        org_id: UUID,
        entity_id: UUID,
    ) -> dict | None:
        """Retrieve an entity by its UUID."""
        if self._backend is None:
            return None

        try:
            return await self._backend.get_entity(org_id=org_id, entity_id=entity_id)
        except Exception as exc:
            logger.error(
                "entity_repository.get_by_id_failed",
                extra={
                    "org_id": str(org_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            return None

    # ── Relationship CRUD ──────────────────────────────────────────────────────

    async def upsert_relationship(
        self,
        subject: str,
        predicate: str,
        obj: str,
        org_id: UUID,
    ) -> dict | None:
        """Create a relationship between two entities by name.

        Looks up both entities by name, then creates a directed edge.

        Returns ``None`` if the backend is unavailable or either entity
        is not found.
        """
        if self._backend is None:
            return None

        # Look up both endpoints by name
        subject_node = await self.get_entity_by_name(org_id, subject)
        if subject_node is None:
            logger.warning(
                "entity_repository.relationship_subject_not_found",
                extra={"org_id": str(org_id), "subject": subject},
            )
            return None

        object_node = await self.get_entity_by_name(org_id, obj)
        if object_node is None:
            logger.warning(
                "entity_repository.relationship_object_not_found",
                extra={"org_id": str(org_id), "object": obj},
            )
            return None

        # Create relationship
        try:
            relationship = await self._backend.create_relationship(
                org_id=org_id,
                source_id=UUID(subject_node["id"]),
                target_id=UUID(object_node["id"]),
                relationship_type=predicate,
            )
            logger.info(
                "entity_repository.relationship_created",
                extra={
                    "org_id": str(org_id),
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "edge_id": relationship.get("id"),
                },
            )
            return relationship
        except Exception as exc:
            logger.warning(
                "entity_repository.relationship_create_duplicate",
                extra={
                    "org_id": str(org_id),
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "error": str(exc),
                },
            )
            return None
