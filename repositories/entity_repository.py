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
        project_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict | None:
        """Create or update an entity (upsert by org_id + name).

        Delegates to the graph backend which uses ``ON CONFLICT DO UPDATE``
        on ``(organization_id, name)`` — the unique constraint added in
        migration 0012 ensures no duplicates.

        Returns ``None`` if the backend is unavailable.
        """
        if self._backend is None:
            return None

        # Check if entity with this name already exists (for logging only)
        existing = await self.get_entity_by_name(org_id, project_id, name)
        action = "created"
        changed_fields: list[str] = []

        if existing is not None:
            action = "existing"
            # Detect what would change
            if existing.get("type") != entity_type:
                # Only flag if the new type is more specific
                if existing.get("type") == "Custom" and entity_type != "Custom":
                    changed_fields.append("entity_type")
            if summary and existing.get("summary", "") != summary:
                changed_fields.append("summary")
            if changed_fields:
                action = "updated"

        try:
            entity = await self._backend.create_entity(
                org_id=org_id,
                project_id=project_id,
                name=name,
                entity_type=entity_type,
                summary=summary,
            )
            logger.info(
                "entity_repository.entity_upserted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_name": name,
                    "entity_type": entity_type,
                    "entity_id": entity.get("id"),
                    "action": action,
                    "changed_fields": ",".join(changed_fields)
                    if changed_fields
                    else None,
                },
            )
            return entity
        except Exception as exc:
            logger.error(
                "entity_repository.upsert_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_name": name,
                    "entity_type": entity_type,
                    "action": action,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return None

    async def get_entity_by_name(
        self,
        org_id: UUID,
        project_id: UUID,
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
                project_id=project_id,
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
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_name": name,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return None

    async def get_entity_by_id(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
    ) -> dict | None:
        """Retrieve an entity by its UUID."""
        if self._backend is None:
            return None

        try:
            return await self._backend.get_entity(
                org_id=org_id, project_id=project_id, entity_id=entity_id
            )
        except Exception as exc:
            logger.error(
                "entity_repository.get_by_id_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
                exc_info=True,
            )
            return None

    # ── Relationship CRUD ──────────────────────────────────────────────────────

    async def upsert_relationship(
        self,
        subject: str,
        predicate: str,
        obj: str,
        org_id: UUID,
        project_id: UUID,
    ) -> dict | None:
        """Create a relationship between two entities by name.

        Looks up both entities by name, then creates a directed edge.

        Returns ``None`` if the backend is unavailable or either entity
        is not found.
        """
        if self._backend is None:
            return None

        # Look up both endpoints by name
        subject_node = await self.get_entity_by_name(org_id, project_id, subject)
        if subject_node is None:
            logger.warning(
                "entity_repository.relationship_subject_not_found",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "subject": subject,
                },
            )
            return None

        object_node = await self.get_entity_by_name(org_id, project_id, obj)
        if object_node is None:
            logger.warning(
                "entity_repository.relationship_object_not_found",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "object": obj,
                },
            )
            return None

        # Create relationship
        try:
            relationship = await self._backend.create_relationship(
                org_id=org_id,
                project_id=project_id,
                source_id=UUID(subject_node["id"]),
                target_id=UUID(object_node["id"]),
                relationship_type=predicate,
            )
            logger.info(
                "entity_repository.relationship_created",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
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
                    "project_id": str(project_id),
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return None
