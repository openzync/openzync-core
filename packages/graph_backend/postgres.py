"""PostgreSQL-native graph backend — no external graph DB required.

Implements the ``GraphBackend`` ABC using PostgreSQL tables
(``graph_entities``, ``graph_relationships``, ``graph_episode_entities``)
and recursive CTEs for BFS traversal.

Usage::

    backend = PostgresGraphBackend(db_session)
    entity = await backend.create_entity(org_id, name="Acme", entity_type="company")
    results = await backend.traverse(org_id, start_id, max_depth=2)
"""

from __future__ import annotations

import base64
import orjson
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ExternalServiceError, GraphBackendUnavailableError, NotFoundError
from packages.graph_backend.interface import GraphBackend

logger = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

MAX_TRAVERSAL_DEPTH: int = 5
"""Hard cap on BFS depth to prevent unbounded recursive queries."""

BFS_CTE = """
WITH RECURSIVE bfs AS (
    -- Anchor: start node
    SELECT ge.id, ge.name, ge.entity_type, ge.summary,
           ge.attributes, ge.created_at, 0 AS depth
    FROM graph_entities ge
    WHERE ge.id = :start_id
      AND ge.organization_id = :org_id
      AND ge.project_id = :project_id

    UNION

    -- Recursive: follow active edges (both directions)
    SELECT DISTINCT e.id, e.name, e.entity_type, e.summary,
           e.attributes, e.created_at, bfs.depth + 1
    FROM bfs
    JOIN graph_relationships r
        ON (r.source_id = bfs.id OR r.target_id = bfs.id)
        AND r.invalid_at IS NULL
        AND r.project_id = :project_id
    JOIN graph_entities e
        ON (e.id = CASE
            WHEN r.source_id = bfs.id THEN r.target_id
            ELSE r.source_id
        END)
    WHERE bfs.depth < :max_depth
      AND e.organization_id = :org_id
      AND e.project_id = :project_id
      AND (:edge_types_null OR r.relationship_type = ANY(:edge_types))
)
SELECT DISTINCT ON (bfs.id) bfs.id, bfs.name, bfs.entity_type,
       bfs.summary, bfs.attributes, bfs.created_at, bfs.depth
FROM bfs
ORDER BY bfs.id, bfs.depth
"""

SEARCH_ENTITIES_SQL = """
SELECT ge.id, ge.name, ge.entity_type, ge.summary,
       ge.attributes, ge.created_at,
       -- Relevance score: combine trigram similarity + full-text rank
       COALESCE(similarity(ge.name, :query), 0) * 0.6
       + COALESCE(ts_rank(to_tsvector('english', coalesce(ge.summary, '')),
                            plainto_tsquery('english', :query)), 0) * 0.4
       AS score
FROM graph_entities ge
WHERE ge.organization_id = :org_id
  AND ge.project_id = :project_id
  AND (
      ge.name ILIKE '%' || :query || '%'
      OR similarity(ge.name, :query) > 0.2
      OR to_tsvector('english', coalesce(ge.summary, ''))
         @@ plainto_tsquery('english', :query)
  )
  AND (:entity_types_null OR ge.entity_type = ANY(:entity_types))
ORDER BY score DESC
LIMIT :limit
OFFSET :offset
"""

LIST_ENTITIES_SQL = """
SELECT ge.id, ge.name, ge.entity_type, ge.summary,
       ge.attributes, ge.created_at
FROM graph_entities ge
WHERE {where_clause}
ORDER BY ge.created_at ASC, ge.id ASC
LIMIT :limit
"""

LIST_RELATIONSHIPS_SQL = """
SELECT r.id, r.source_id, r.target_id, r.relationship_type,
       r.properties, r.fact, r.confidence,
       r.valid_from, r.valid_to, r.created_at
FROM graph_relationships r
WHERE {where_clause}
ORDER BY r.created_at DESC
LIMIT :limit
"""


class PostgresGraphBackend(GraphBackend):
    """PostgreSQL-native graph backend.

    Stores entities and relationships in dedicated PostgreSQL tables.
    Uses recursive CTEs for BFS traversal and pg_trgm + pgvector for
    entity search.

    Args:
        db: An async SQLAlchemy session. Must be request-scoped —
            the caller (usually a FastAPI dependency) is responsible
            for session lifecycle.
        max_traversal_depth: Maximum BFS depth (default 2, max 5).
    """

    def __init__(
        self,
        db: AsyncSession,
        max_traversal_depth: int = 2,
    ) -> None:
        self._db = db
        self._max_depth = min(max_traversal_depth, MAX_TRAVERSAL_DEPTH)

    # ── Entity CRUD ────────────────────────────────────────────────────────────

    async def create_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict:
        """Create or update an entity node (upsert by org_id + name).

        Uses ``ON CONFLICT (organization_id, name) DO UPDATE`` so that
        duplicate extractions (same entity name in the same org) update
        the entity_type and summary with the latest information rather
        than silently dropping it or creating duplicate rows.

        Entity type is only **upgraded** — if the existing type is
        ``"Custom"`` and the new one is specific (e.g. ``"Person"``),
        it will be updated.  A specific type is never downgraded to
        ``"Custom"``.

        ``project_id`` is stored in the row for project-scoped queries.

        Raises:
            ExternalServiceError: If the insert fails.
        """
        try:
            # Normalise to lowercase so the case-sensitive unique constraint
            # (organization_id, name) correctly deduplicates "Nikita" ↔ "nikita".
            name = name.lower().strip()

            result = await self._db.execute(
                text(
                    """
                    INSERT INTO graph_entities
                        (organization_id, project_id, name, entity_type, summary)
                    VALUES (:org_id, :project_id, :name, :type, :summary)
                    ON CONFLICT (organization_id, name)
                    DO UPDATE SET
                        entity_type = CASE
                            WHEN graph_entities.entity_type = 'Custom'
                                 AND :type != 'Custom' THEN :type
                            ELSE graph_entities.entity_type
                        END,
                        summary = CASE
                            WHEN :summary != '' THEN :summary
                            ELSE graph_entities.summary
                        END,
                        updated_at = now()
                    RETURNING id, name, entity_type, summary, attributes,
                              created_at, updated_at
                    """
                ),
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "name": name,
                    "type": entity_type,
                    "summary": summary if summary is not None else "",
                },
            )
            row = result.one()
            entity = self._row_to_entity(row)

            # Determine action by checking if updated_at differs from created_at
            # within the same statement (PostgreSQL xmin/xmax trick won't work
            # with RETURNING).  If updated_at > created_at by more than a small
            # delta it was an update; otherwise a fresh insert.
            action = "created"
            if row.updated_at and row.created_at:
                delta = (row.updated_at - row.created_at).total_seconds()
                if delta > 0.5:
                    action = "updated"

            logger.info(
                "pg_graph.entity_upserted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": entity["id"],
                    "entity_type": entity_type,
                    "action": action,
                },
            )
            return entity
        except Exception as exc:
            logger.error(
                "pg_graph.create_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "name": name,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to create entity '{name}': {exc}",
                detail={"org_id": str(org_id), "name": name},
            ) from exc

    async def get_entity(
        self, org_id: UUID, project_id: UUID, entity_id: UUID
    ) -> dict | None:
        """Retrieve an entity node by ID, scoped to org and project."""
        try:
            result = await self._db.execute(
                text(
                    """
                    SELECT id, name, entity_type, summary, attributes, created_at
                    FROM graph_entities
                    WHERE id = :entity_id
                      AND organization_id = :org_id
                      AND project_id = :project_id
                    """
                ),
                {
                    "entity_id": str(entity_id),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            row = result.one_or_none()
            return self._row_to_entity(row) if row else None
        except Exception as exc:
            logger.error(
                "pg_graph.get_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to retrieve entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    async def update_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
        *,
        name: str | None = None,
        entity_type: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Update an entity's mutable fields.

        Only the provided fields are changed; ``None`` fields are left
        untouched.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_id: UUID of the entity to update.
            name: New name, or ``None`` to leave unchanged.
            entity_type: New type label, or ``None`` to leave unchanged.
            summary: New summary text, or ``None`` to leave unchanged.

        Returns:
            The updated entity dict with at minimum ``id``, ``name``,
            ``entity_type``, ``summary``, and ``updated_at`` keys.

        Raises:
            NotFoundError: If no entity with the given ID exists.
        """
        updates: dict[str, object] = {}
        update_cols: list[str] = []

        if name is not None:
            updates["name"] = name
            update_cols.append("name = :name")
        if entity_type is not None:
            updates["entity_type"] = entity_type
            update_cols.append("entity_type = :entity_type")
        if summary is not None:
            updates["summary"] = summary
            update_cols.append("summary = :summary")

        if not update_cols:
            entity = await self.get_entity(org_id, project_id, entity_id)
            if entity is None:
                raise NotFoundError(
                    message=f"Entity {entity_id} not found",
                    detail={"org_id": str(org_id), "entity_id": str(entity_id)},
                )
            return entity

        updates["org_id"] = str(org_id)
        updates["project_id"] = str(project_id)
        updates["entity_id"] = str(entity_id)
        set_clause = ", ".join(update_cols)

        try:
            result = await self._db.execute(
                text(
                    f"""
                    UPDATE graph_entities
                    SET {set_clause}, updated_at = now()
                    WHERE id = :entity_id
                      AND organization_id = :org_id
                      AND project_id = :project_id
                    RETURNING id, name, entity_type, summary, attributes,
                              created_at, updated_at
                    """
                ),
                updates,
            )
            row = result.one_or_none()
            if row is None:
                raise NotFoundError(
                    message=f"Entity {entity_id} not found",
                    detail={"org_id": str(org_id), "entity_id": str(entity_id)},
                )
            return self._row_to_entity(row)
        except NotFoundError:
            raise
        except Exception as exc:
            logger.error(
                "pg_graph.update_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to update entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    async def delete_entity(
        self, org_id: UUID, project_id: UUID, entity_id: UUID
    ) -> bool:
        """Delete an entity — cascades to relationships and episode links.

        Returns:
            ``True`` if the entity existed and was deleted.
        """
        try:
            result = await self._db.execute(
                text(
                    """
                    DELETE FROM graph_entities
                    WHERE id = :entity_id
                      AND organization_id = :org_id
                      AND project_id = :project_id
                    RETURNING id
                    """
                ),
                {
                    "entity_id": str(entity_id),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            deleted = result.rowcount > 0
            if deleted:
                logger.info(
                    "pg_graph.entity_deleted",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "entity_id": str(entity_id),
                    },
                )
            return deleted
        except Exception as exc:
            logger.error(
                "pg_graph.delete_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to delete entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    # ── Relationship CRUD ─────────────────────────────────────────────────────

    async def create_relationship(
        self,
        org_id: UUID,
        project_id: UUID,
        source_id: UUID,
        target_id: UUID,
        relationship_type: str,
        properties: dict | None = None,
        confidence: float | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> dict:
        """Create or update a directed relationship between two entities.

        Uses ``ON CONFLICT DO UPDATE`` so that duplicate extractions
        (same source, target, type) **update** the existing relationship
        with newer properties, fact text, and confidence rather than
        silently dropping the information.

        Temporal semantics:
        - ``valid_from`` defaults to now().
        - ``valid_to`` and ``invalid_at`` are NULL (active until invalidated).

        ``project_id`` is stored in the row for project-scoped queries.

        Raises:
            ExternalServiceError: On FK violations or unexpected DB errors.
        """
        try:
            # Use a savepoint so a failed relationship does NOT roll back
            # entity inserts that happened earlier in the same transaction.
            async with self._db.begin_nested():
                result = await self._db.execute(
                    text(
                        """
                        INSERT INTO graph_relationships
                            (organization_id, project_id, source_id, target_id,
                             relationship_type, properties, fact, confidence,
                             valid_from, valid_to, created_at)
                        VALUES
                            (:org_id, :project_id, :source_id, :target_id,
                             :rel_type, CAST(:properties AS jsonb), :fact, :confidence,
                             COALESCE(:valid_from, now()), :valid_to, now())
                        ON CONFLICT (source_id, target_id, relationship_type)
                        WHERE invalid_at IS NULL
                        DO UPDATE SET
                            properties = CAST(:properties AS jsonb),
                            fact = :fact,
                            confidence = GREATEST(graph_relationships.confidence, :confidence),
                            valid_from = LEAST(graph_relationships.valid_from, COALESCE(:valid_from, now())),
                            valid_to = CASE
                                WHEN :valid_to IS NULL THEN NULL
                                WHEN graph_relationships.valid_to IS NULL THEN NULL
                                ELSE GREATEST(graph_relationships.valid_to, :valid_to)
                            END,
                            updated_at = now()
                        RETURNING id, source_id, target_id, relationship_type,
                                  properties, fact, confidence,
                                  valid_from, valid_to, created_at, updated_at
                        """
                    ),
                    {
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "source_id": str(source_id),
                        "target_id": str(target_id),
                        "rel_type": relationship_type,
                        "properties": orjson.dumps(properties if properties is not None else {}).decode("utf-8"),
                        "fact": "",
                        "confidence": confidence if confidence is not None else 1.0,
                        "valid_from": valid_from,
                        "valid_to": valid_to,
                    },
                )
                row = result.one()

            relationship = self._row_to_relationship(row)

            # Detect insert vs update via created_at/updated_at delta
            action = "created"
            if row.updated_at and row.created_at:
                delta = (row.updated_at - row.created_at).total_seconds()
                if delta > 0.5:
                    action = "updated"

            logger.info(
                "pg_graph.relationship_upserted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "type": relationship_type,
                    "action": action,
                },
            )
            return relationship
        except Exception as exc:
            logger.error(
                "pg_graph.create_relationship_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "relationship_type": relationship_type,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to create relationship '{relationship_type}': {exc}",
                detail={
                    "org_id": str(org_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                },
            ) from exc

    async def expire_relationship(
        self,
        org_id: UUID,
        project_id: UUID,
        relationship_id: UUID,
    ) -> bool:
        """Mark a relationship as invalidated (soft-delete).

        Sets ``invalid_at`` to now(). Returns ``True`` if expired.
        """
        try:
            result = await self._db.execute(
                text(
                    """
                    UPDATE graph_relationships
                    SET invalid_at = now()
                    WHERE id = :rel_id
                      AND organization_id = :org_id
                      AND project_id = :project_id
                      AND invalid_at IS NULL
                    RETURNING id
                    """
                ),
                {
                    "rel_id": str(relationship_id),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            return result.rowcount > 0
        except Exception as exc:
            logger.error(
                "pg_graph.expire_relationship_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "relationship_id": str(relationship_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to expire relationship {relationship_id}: {exc}",
                detail={
                    "org_id": str(org_id),
                    "relationship_id": str(relationship_id),
                },
            ) from exc

    async def get_relationships(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
        relationship_type: str | None = None,
        at_time: datetime | None = None,
    ) -> list[dict]:
        """Get all active relationships for an entity.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            entity_id: The entity whose relationships to fetch.
            relationship_type: Optional filter by type.
            at_time: Only return relationships valid at this time
                (defaults to now).

        Returns:
            List of relationship dicts.
        """
        at_time = at_time or datetime.now(timezone.utc)
        conditions = """
            r.organization_id = :org_id
            AND r.project_id = :project_id
            AND (r.source_id = :entity_id OR r.target_id = :entity_id)
            AND r.invalid_at IS NULL
            AND (r.valid_from IS NULL OR r.valid_from <= :at_time)
            AND (r.valid_to IS NULL OR r.valid_to >= :at_time)
        """
        params: dict[str, object] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "entity_id": str(entity_id),
            "at_time": at_time,
            "limit": 201,
        }
        if relationship_type:
            conditions += " AND r.relationship_type = :rel_type"
            params["rel_type"] = relationship_type

        try:
            result = await self._db.execute(
                text(
                    f"""
                    SELECT r.id, r.source_id, r.target_id, r.relationship_type,
                           r.properties, r.fact, r.confidence,
                           r.valid_from, r.valid_to, r.created_at
                    FROM graph_relationships r
                    WHERE {conditions}
                    ORDER BY r.created_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
            return [self._row_to_relationship(row) for row in result.all()]
        except Exception as exc:
            logger.error(
                "pg_graph.get_relationships_failed",
                extra={
                    "org_id": str(org_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get relationships for entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    # ── BFS Traversal ────────────────────────────────────────────────────────

    async def traverse(
        self,
        org_id: UUID,
        project_id: UUID,
        start_node_id: UUID,
        max_depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> list[dict]:
        """Traverse the graph outward from a starting node.

        Uses a recursive CTE for BFS. Both incoming and outgoing edges
        are followed. Only active (non-invalidated) relationships are
        traversed.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            start_node_id: UUID of the node to start from.
            max_depth: Maximum hops (capped at MAX_TRAVERSAL_DEPTH).
            edge_types: If provided, only follow edges with these types.
                ``None`` = all types. Empty list = no edges (returns start).

        Returns:
            List of node dicts with ``depth`` field indicating hop count.
            Includes the start node at depth 0.
        """
        # Distinguish None (all types) from [] (no types)
        if edge_types is not None and len(edge_types) == 0:
            # No edge types to follow — return just the start node
            start = await self.get_entity(org_id, project_id, start_node_id)
            if start is None:
                return []
            start["depth"] = 0
            return [start]

        max_depth = min(max_depth, self._max_depth)
        edge_types_null = edge_types is None

        try:
            # Set statement timeout to prevent runaway queries
            await self._db.execute(text("SET LOCAL statement_timeout = '5s'"))

            result = await self._db.execute(
                text(BFS_CTE),
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "start_id": str(start_node_id),
                    "max_depth": max_depth,
                    "edge_types": edge_types if edge_types is not None else [],
                    "edge_types_null": edge_types_null,
                },
            )
            nodes = []
            for row in result.all():
                node = self._row_to_entity(row)
                node["depth"] = row.depth
                nodes.append(node)
            return nodes
        except Exception as exc:
            logger.error(
                "pg_graph.traverse_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "start_node": str(start_node_id),
                    "max_depth": max_depth,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to traverse from node {start_node_id}: {exc}",
                detail={
                    "org_id": str(org_id),
                    "start_node_id": str(start_node_id),
                },
            ) from exc

    async def traverse_iterative(
        self,
        org_id: UUID,
        project_id: UUID,
        start_id: UUID,
        max_depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> list[dict]:
        """Iterative BFS fallback — more round-trips but avoids deep CTE.

        Recommended for graphs > 100K nodes where the recursive CTE
        may exceed the statement timeout.
        """
        from collections import deque

        if edge_types is not None and len(edge_types) == 0:
            start = await self.get_entity(org_id, project_id, start_id)
            if start is None:
                return []
            start["depth"] = 0
            return [start]

        max_depth = min(max_depth, self._max_depth)
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque()
        queue.append((str(start_id), 0))
        nodes: list[dict] = []

        type_filter = edge_types is not None

        while queue:
            current_id, depth = queue.popleft()
            if current_id in visited:
                continue
            visited.add(current_id)

            # Fetch current node
            entity = await self.get_entity(org_id, project_id, UUID(current_id))
            if entity:
                entity["depth"] = depth
                nodes.append(entity)

            if depth >= max_depth:
                continue

            # Fetch direct neighbours
            try:
                result = await self._db.execute(
                    text(
                        """
                        SELECT CASE
                            WHEN r.source_id = :eid THEN r.target_id
                            ELSE r.source_id
                        END AS neighbour_id
                        FROM graph_relationships r
                        WHERE r.organization_id = :org_id
                          AND r.project_id = :project_id
                          AND r.invalid_at IS NULL
                          AND (r.source_id = :eid OR r.target_id = :eid)
                          AND (:types_null OR r.relationship_type = ANY(:edge_types))
                        """
                    ),
                    {
                        "eid": current_id,
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "types_null": not type_filter,
                        "edge_types": edge_types if edge_types is not None else [],
                    },
                )
                for row in result.all():
                    neighbour_id = str(row.neighbour_id)
                    if neighbour_id not in visited:
                        queue.append((neighbour_id, depth + 1))
            except Exception as exc:
                logger.error(
                    "pg_graph.traverse_iterative.neighbour_fetch_failed",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "entity_id": current_id,
                    },
                    exc_info=True,
                )
                raise GraphBackendUnavailableError(
                    f"PostgreSQL graph traversal neighbour fetch failed for entity {current_id}."
                ) from exc

        return nodes

    # ── Search ───────────────────────────────────────────────────────────────

    async def search_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        query: str,
        types: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Search entities by name or summary.

        Combines trigram similarity (fuzzy name match) with full-text search
        on summaries.  Weighted 60% name match, 40% summary match.
        Scoped to the given project.

        Returns entities sorted by relevance score descending.
        """
        try:
            # Pass entity_types_null flag to handle None vs typed array
            result = await self._db.execute(
                text(SEARCH_ENTITIES_SQL),
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "query": query,
                    "entity_types": types if types is not None else [],
                    "entity_types_null": types is None,
                    "limit": limit,
                    "offset": offset,
                },
            )
            entities = []
            for row in result.all():
                entity = self._row_to_entity(row)
                entity["score"] = (
                    float(row.score)
                    if hasattr(row, "score") and row.score is not None
                    else 0.0
                )
                entities.append(entity)
            return entities
        except Exception as exc:
            logger.error(
                "pg_graph.search_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "query": query,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Entity search failed: {exc}",
                detail={"org_id": str(org_id), "query": query},
            ) from exc

    async def retrieve_graph(
        self,
        org_id: UUID,
        project_id: UUID,
        query: str,
        *,
        match_limit: int = 5,
        max_depth: int = 2,
        max_results: int = 50,
    ) -> list[dict]:
        """Search entities matching query, then BFS-traverse outward.

        Combines entity text search with graph traversal into a single
        call so the caller (HybridRetriever) can run multiple backends
        in parallel and merge results.

        Steps:
          1. Search entities whose name or summary matches the query.
          2. For each matched entity, BFS-traverse to depth ``max_depth``.
          3. Deduplicate by entity id, shape results with distance key.
          4. Sort by distance ascending and limit.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            query: Free-text search string.
            match_limit: Max entities to match before traversal.
            max_depth: Max BFS depth from each matched entity.
            max_results: Max total results to return.

        Returns:
            Entity dicts with id, name, type, summary, and distance keys.
            Distance 0 = directly matched, 1+ = reached via traversal.
        """
        try:
            # Step 1: Search for entities matching the query
            matched_entities = await self.search_entities(
                org_id=org_id,
                project_id=project_id,
                query=query,
                limit=match_limit,
            )

            if not matched_entities:
                return []

            # Step 2: BFS traverse from each matched entity
            seen: set[str] = set()
            results: list[dict] = []

            for entity in matched_entities:
                entity_id_str = entity.get("id", "")
                if not entity_id_str or entity_id_str in seen:
                    continue
                seen.add(entity_id_str)

                # Add the matched entity itself with distance 0
                results.append({
                    "id": entity_id_str,
                    "name": entity.get("name", ""),
                    "type": entity.get("type", ""),
                    "summary": entity.get("summary", ""),
                    "distance": 0,
                })

                # BFS up to max_depth
                try:
                    entity_id = UUID(entity_id_str)
                except (ValueError, TypeError):
                    continue

                try:
                    related = await self.traverse(
                        org_id=org_id,
                        project_id=project_id,
                        start_node_id=entity_id,
                        max_depth=max_depth,
                    )
                except Exception as exc:
                    logger.error(
                        "pg_graph.retrieve_graph.traverse_failed",
                        extra={
                            "entity_id": entity_id_str,
                            "query": query,
                        },
                        exc_info=True,
                    )
                    raise GraphBackendUnavailableError(
                        f"PostgreSQL graph traversal failed for entity {entity_id_str} during retrieve_graph."
                    ) from exc

                for node in related:
                    node_id = node.get("id", "")
                    depth = node.get("depth", 1)
                    if node_id and node_id not in seen:
                        seen.add(node_id)
                        results.append({
                            "id": node_id,
                            "name": node.get("name", ""),
                            "type": node.get("type", ""),
                            "summary": node.get("summary", ""),
                            "distance": depth,
                        })

            # Sort by distance (closest first), limit to max_results
            results.sort(key=lambda x: x.get("distance", 99))
            return results[:max_results]

        except Exception as exc:
            logger.error(
                "pg_graph.retrieve_graph_failed",
                extra={"query": query},
                exc_info=True,
            )
            raise GraphBackendUnavailableError(
                f"PostgreSQL retrieve_graph failed for query '{query}'."
            ) from exc

    # ── Entity Listing ────────────────────────────────────────────────────────

    async def list_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List entities with cursor-based pagination.

        Cursor format: base64-encoded JSON ``{"c": "<created_at>", "i": "<id>"}``
        matching the pattern used by ``UserRepository`` and ``SessionRepository``.
        Scoped to the given project.
        """
        limit = min(limit, 200)

        where_clause = "ge.organization_id = :org_id AND ge.project_id = :project_id"
        params: dict[str, object] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "limit": limit + 1,
        }

        if entity_type:
            where_clause += " AND ge.entity_type = :entity_type"
            params["entity_type"] = entity_type

        if cursor:
            try:
                decoded = orjson.loads(base64.b64decode(cursor))
                cursor_created_at = decoded["c"]
                cursor_id = decoded["i"]
                where_clause += (
                    " AND (ge.created_at, ge.id) > (:cursor_ts, :cursor_id::uuid)"
                )
                params["cursor_ts"] = cursor_created_at
                params["cursor_id"] = cursor_id
            except Exception:
                logger.warning(
                    "pg_graph.list_entities.invalid_cursor", extra={"cursor": cursor}
                )

        try:
            query = LIST_ENTITIES_SQL.format(where_clause=where_clause)
            result = await self._db.execute(text(query), params)
            rows = result.all()
            has_more = len(rows) > limit
            items = [self._row_to_entity(r) for r in rows[:limit]]

            next_cursor = None
            if has_more and items:
                last = items[-1]
                cursor_payload = orjson.dumps({"c": last["created_at"], "i": last["id"]})
                next_cursor = base64.b64encode(cursor_payload).decode()

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except Exception as exc:
            logger.error(
                "pg_graph.list_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_type": entity_type,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to list entities: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

    async def list_entity_edges(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
        *,
        predicate: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List all edges incident to an entity with cursor pagination."""
        limit = min(limit, 200)

        conditions = """
            r.organization_id = :org_id
            AND r.project_id = :project_id
            AND (r.source_id = :eid OR r.target_id = :eid)
        """
        params: dict[str, object] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "eid": str(entity_id),
            "limit": limit + 1,
        }

        if predicate:
            conditions += " AND r.relationship_type = :pred"
            params["pred"] = predicate

        if cursor:
            try:
                decoded = orjson.loads(base64.b64decode(cursor))
                conditions += (
                    " AND (r.created_at, r.id) > (:cursor_ts, :cursor_id::uuid)"
                )
                params["cursor_ts"] = decoded["c"]
                params["cursor_id"] = decoded["i"]
            except Exception:
                logger.warning(
                    "pg_graph.list_entity_edges.invalid_cursor",
                    extra={"cursor": cursor},
                )

        try:
            query = LIST_RELATIONSHIPS_SQL.format(where_clause=conditions)
            result = await self._db.execute(text(query), params)
            rows = result.all()
            has_more = len(rows) > limit
            items = [self._row_to_relationship(r) for r in rows[:limit]]

            next_cursor = None
            if has_more and items:
                last = items[-1]
                cursor_payload = orjson.dumps({"c": last["created_at"], "i": last["id"]})
                next_cursor = base64.b64encode(cursor_payload).decode()

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except Exception as exc:
            logger.error(
                "pg_graph.list_entity_edges_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to list edges for entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    async def get_entity_with_edges(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
    ) -> dict | None:
        """Retrieve an entity with all its incident edges."""
        entity = await self.get_entity(org_id, project_id, entity_id)
        if entity is None:
            return None
        edges = await self.get_relationships(org_id, project_id, entity_id)
        return {"node": entity, "edges": edges}

    # ── Health ────────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Verify the PostgreSQL connection is alive."""
        try:
            await self._db.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ── Group A: Entity-Episode Linking ────────────────────────────────────────

    async def link_entity_to_episode(
        self,
        org_id: UUID,
        project_id: UUID,
        episode_id: UUID,
        entity_id: UUID,
    ) -> None:
        """Record that an entity appears in a specific episode.

        Idempotent — uses ``ON CONFLICT DO NOTHING``.
        """
        try:
            await self._db.execute(
                text("""
                    INSERT INTO graph_episode_entities
                        (episode_id, entity_id, project_id, created_at)
                    VALUES (:episode_id, :entity_id, :project_id, now())
                    ON CONFLICT (episode_id, entity_id) DO NOTHING
                """),
                {
                    "episode_id": str(episode_id),
                    "entity_id": str(entity_id),
                    "project_id": str(project_id),
                },
            )
        except Exception as exc:
            logger.error(
                "pg_graph.link_entity_to_episode_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "episode_id": str(episode_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to link entity {entity_id} to episode {episode_id}: {exc}",
                detail={
                    "org_id": str(org_id),
                    "episode_id": str(episode_id),
                    "entity_id": str(entity_id),
                },
            ) from exc

    async def get_entities_for_session(
        self,
        org_id: UUID,
        project_id: UUID,
        session_id: UUID,
    ) -> list[dict[str, Any]]:
        """Return all distinct graph entities linked to episodes in a session."""
        try:
            result = await self._db.execute(
                text("""
                    WITH session_entities AS (
                        SELECT DISTINCT ge.id, ge.name, ge.entity_type, ge.summary
                        FROM graph_entities ge
                        JOIN graph_episode_entities gee ON ge.id = gee.entity_id
                        JOIN episodes e ON e.id = gee.episode_id
                        WHERE e.session_id = :session_id
                          AND e.organization_id = :org_id
                          AND ge.organization_id = :org_id
                          AND ge.project_id = :project_id
                          AND e.is_deleted = false
                          AND ge.is_merged = false
                    )
                    SELECT * FROM session_entities
                    UNION
                    SELECT ge2.id, ge2.name, ge2.entity_type, ge2.summary
                    FROM graph_entities ge2
                    JOIN graph_relationships gr ON gr.target_id = ge2.id
                    WHERE gr.relationship_type = 'member_of'
                      AND gr.organization_id = :org_id
                      AND gr.project_id = :project_id
                      AND ge2.organization_id = :org_id
                      AND ge2.project_id = :project_id
                      AND ge2.entity_type = 'community'
                      AND ge2.is_merged = false
                      AND gr.source_id IN (SELECT id FROM session_entities)
                    ORDER BY name
                """),
                {
                    "session_id": str(session_id),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            return [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "entity_type": row.entity_type,
                    "summary": row.summary if row.summary else "",
                }
                for row in result.all()
            ]
        except Exception as exc:
            logger.error(
                "pg_graph.get_entities_for_session_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "session_id": str(session_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get entities for session {session_id}: {exc}",
                detail={
                    "org_id": str(org_id),
                    "session_id": str(session_id),
                },
            ) from exc

    async def get_co_occurring_entity_pairs(
        self,
        org_id: UUID,
        project_id: UUID,
        min_co_count: int = 2,
    ) -> list[dict[str, Any]]:
        """Find entity pairs that co-appear in episodes above a threshold."""
        try:
            result = await self._db.execute(
                text("""
                    SELECT
                        a.entity_id AS entity_a_id,
                        ge_a.name AS entity_a_name,
                        b.entity_id AS entity_b_id,
                        ge_b.name AS entity_b_name,
                        COUNT(DISTINCT a.episode_id) AS co_count
                    FROM graph_episode_entities a
                    JOIN graph_episode_entities b
                        ON a.episode_id = b.episode_id
                        AND a.entity_id < b.entity_id
                    JOIN graph_entities ge_a
                        ON ge_a.id = a.entity_id
                    JOIN graph_entities ge_b
                        ON ge_b.id = b.entity_id
                    WHERE a.project_id = :project_id
                    GROUP BY entity_a_id, entity_a_name,
                             entity_b_id, entity_b_name
                    HAVING COUNT(DISTINCT a.episode_id) >= :min_count
                    ORDER BY co_count DESC
                """),
                {
                    "project_id": str(project_id),
                    "min_count": min_co_count,
                },
            )
            return [
                {
                    "entity_a_id": str(row.entity_a_id),
                    "entity_a_name": row.entity_a_name,
                    "entity_b_id": str(row.entity_b_id),
                    "entity_b_name": row.entity_b_name,
                    "co_count": row.co_count,
                }
                for row in result.all()
            ]
        except Exception as exc:
            logger.error(
                "pg_graph.get_co_occurring_pairs_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get co-occurring entity pairs: {exc}",
                detail={"org_id": str(org_id), "project_id": str(project_id)},
            ) from exc

    # ── Group B: Bulk / Merge Operations ───────────────────────────────────────

    async def get_all_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        include_merged: bool = False,
    ) -> list[dict[str, Any]]:
        # ⚠️ BATCH USE ONLY — no pagination, no limit. Potentially millions of rows.
        try:
            result = await self._db.execute(
                text("""
                    SELECT id, name, entity_type, summary, attributes,
                           is_merged, created_at
                    FROM graph_entities
                    WHERE organization_id = :org_id
                      AND project_id = :project_id
                      AND (:include_merged OR is_merged = false)
                    ORDER BY name
                """),
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "include_merged": include_merged,
                },
            )
            return [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "entity_type": row.entity_type,
                    "summary": row.summary if row.summary else "",
                    "is_merged": bool(row.is_merged) if hasattr(row, "is_merged") else False,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in result.all()
            ]
        except Exception as exc:
            logger.error(
                "pg_graph.get_all_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get all entities: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

    async def get_all_relationships(
        self,
        org_id: UUID,
        project_id: UUID,
    ) -> list[dict[str, Any]]:
        # ⚠️ BATCH USE ONLY — no pagination, no limit. Potentially millions of rows.
        try:
            result = await self._db.execute(
                text("""
                    SELECT id, source_id, target_id, relationship_type,
                           properties, fact, confidence,
                           valid_from, valid_to, created_at
                    FROM graph_relationships
                    WHERE organization_id = :org_id
                      AND project_id = :project_id
                      AND invalid_at IS NULL
                    ORDER BY created_at DESC
                """),
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            return [self._row_to_relationship(row) for row in result.all()]
        except Exception as exc:
            logger.error(
                "pg_graph.get_all_relationships_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get all relationships: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

    async def bulk_search_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        query: str,
        *,
        fuzzy_threshold: float = 0.3,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search entities using fuzzy string matching for dedup detection."""
        try:
            result = await self._db.execute(
                text("""
                    SELECT id, name, entity_type, summary, attributes, created_at,
                           similarity(LOWER(name), LOWER(:query)) AS score
                    FROM graph_entities
                    WHERE organization_id = :org_id
                      AND project_id = :project_id
                      AND is_merged = false
                      AND similarity(LOWER(name), LOWER(:query)) > :threshold
                    ORDER BY score DESC
                    LIMIT :limit
                """),
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "query": query,
                    "threshold": fuzzy_threshold,
                    "limit": limit,
                },
            )
            entities = []
            for row in result.all():
                entity = self._row_to_entity(row)
                entity["score"] = (
                    float(row.score)
                    if hasattr(row, "score") and row.score is not None
                    else 0.0
                )
                entities.append(entity)
            return entities
        except Exception as exc:
            logger.error(
                "pg_graph.bulk_search_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "query": query,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Bulk entity search failed: {exc}",
                detail={"org_id": str(org_id), "query": query},
            ) from exc

    async def merge_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        canonical_id: UUID,
        merged_ids: list[UUID],
    ) -> dict[str, Any]:
        """Merge duplicate entities atomically.

        STRICT ATOMICITY CONTRACT: All-or-nothing. Uses a DB savepoint
        so partial state is never visible.
        """
        if not merged_ids:
            return {"rewired_count": 0, "deleted_count": 0, "merged_count": 0}

        # Verify canonical entity exists
        canonical = await self.get_entity(org_id, project_id, canonical_id)
        if canonical is None:
            raise NotFoundError(
                message=f"Canonical entity {canonical_id} not found",
                detail={"org_id": str(org_id), "canonical_id": str(canonical_id)},
            )

        merged_id_strs = [str(mid) for mid in merged_ids]

        try:
            async with self._db.begin_nested():
                # Set statement timeout for CTE-heavy operations
                await self._db.execute(
                    text("SET LOCAL statement_timeout = '10s'")
                )

                # 1. Rewire relationships: source_id
                src_result = await self._db.execute(
                    text("""
                        UPDATE graph_relationships
                        SET source_id = :canonical_id::uuid
                        WHERE organization_id = :org_id
                          AND project_id = :project_id
                          AND source_id = ANY(:merged_ids::uuid[])
                          AND invalid_at IS NULL
                    """),
                    {
                        "canonical_id": str(canonical_id),
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "merged_ids": merged_id_strs,
                    },
                )
                rewired_source = src_result.rowcount

                # 2. Rewire relationships: target_id
                tgt_result = await self._db.execute(
                    text("""
                        UPDATE graph_relationships
                        SET target_id = :canonical_id::uuid
                        WHERE organization_id = :org_id
                          AND project_id = :project_id
                          AND target_id = ANY(:merged_ids::uuid[])
                          AND invalid_at IS NULL
                    """),
                    {
                        "canonical_id": str(canonical_id),
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "merged_ids": merged_id_strs,
                    },
                )
                rewired_target = tgt_result.rowcount

                # 3. Delete duplicate relationships created by rewiring
                #    (same source, target, and type after rewiring)
                del_result = await self._db.execute(
                    text("""
                        DELETE FROM graph_relationships g
                        WHERE organization_id = :org_id
                          AND project_id = :project_id
                          AND invalid_at IS NULL
                          AND g.id NOT IN (
                              SELECT MIN(id)
                              FROM graph_relationships
                              WHERE organization_id = :org_id
                                AND project_id = :project_id
                                AND invalid_at IS NULL
                              GROUP BY source_id, target_id, relationship_type
                          )
                    """),
                    {
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                    },
                )
                deleted_count = del_result.rowcount

                # 4. Mark merged entities
                merge_result = await self._db.execute(
                    text("""
                        UPDATE graph_entities
                        SET is_merged = true, updated_at = now()
                        WHERE organization_id = :org_id
                          AND project_id = :project_id
                          AND id = ANY(:merged_ids::uuid[])
                    """),
                    {
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "merged_ids": merged_id_strs,
                    },
                )
                merged_count = merge_result.rowcount

            logger.info(
                "pg_graph.entities_merged",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "canonical_id": str(canonical_id),
                    "merged_count": merged_count,
                    "rewired_count": rewired_source + rewired_target,
                    "deleted_count": deleted_count,
                },
            )

            return {
                "rewired_count": rewired_source + rewired_target,
                "deleted_count": deleted_count,
                "merged_count": merged_count,
            }
        except Exception as exc:
            logger.error(
                "pg_graph.merge_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "canonical_id": str(canonical_id),
                    "merged_ids": merged_id_strs,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to merge entities: {exc}",
                detail={
                    "org_id": str(org_id),
                    "canonical_id": str(canonical_id),
                },
            ) from exc

    async def create_relationship_bulk(
        self,
        org_id: UUID,
        project_id: UUID,
        relationships: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Batch-create multiple relationships in a single transaction.

        Each dict in ``relationships`` must have ``source_id``, ``target_id``,
        ``relationship_type``. Optional keys: ``confidence``, ``properties``,
        ``valid_from``, ``valid_to``.
        """
        if not relationships:
            return []

        created: list[dict[str, Any]] = []
        try:
            async with self._db.begin_nested():
                for rel in relationships:
                    source_id = rel.get("source_id")
                    target_id = rel.get("target_id")
                    rel_type = rel.get("relationship_type")

                    if not source_id or not target_id or not rel_type:
                        raise ValueError(
                            f"Each relationship must have source_id, target_id, "
                            f"and relationship_type. Got: {rel}"
                        )

                    result = await self._db.execute(
                        text("""
                            INSERT INTO graph_relationships
                                (organization_id, project_id, source_id, target_id,
                                 relationship_type, properties, confidence,
                                 valid_from, valid_to, created_at)
                            VALUES
                                (:org_id, :project_id, :source_id, :target_id,
                                 :rel_type, CAST(:properties AS jsonb), :confidence,
                                 :valid_from, :valid_to, now())
                            ON CONFLICT (source_id, target_id, relationship_type)
                            WHERE invalid_at IS NULL
                            DO UPDATE SET
                                confidence = GREATEST(graph_relationships.confidence, :confidence),
                                updated_at = now()
                            RETURNING id, source_id, target_id, relationship_type,
                                      properties, fact, confidence,
                                      valid_from, valid_to, created_at
                        """),
                        {
                            "org_id": str(org_id),
                            "project_id": str(project_id),
                            "source_id": str(source_id),
                            "target_id": str(target_id),
                            "rel_type": rel_type,
                            "properties": orjson.dumps(
                                rel.get("properties") or {}
                            ).decode("utf-8"),
                            "confidence": rel.get("confidence", 1.0),
                            "valid_from": rel.get("valid_from"),
                            "valid_to": rel.get("valid_to"),
                        },
                    )
                    created.append(self._row_to_relationship(result.one()))

            return created
        except ValueError:
            raise
        except Exception as exc:
            logger.error(
                "pg_graph.create_relationship_bulk_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "count": len(relationships),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to create {len(relationships)} relationships: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

    # ── Group C: Observations ──────────────────────────────────────────────────

    async def upsert_observation(
        self,
        org_id: UUID,
        project_id: UUID,
        subject_entity_id: UUID,
        observation_type: str,
        content: str,
        confidence: float,
        *,
        related_entity_id: UUID | None = None,
        supporting_fact_ids: list[UUID] | None = None,
        supporting_relationship_ids: list[UUID] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        observation_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a graph-topology observation.

        Upsert uses the functional unique index on
        ``(project_id, subject_entity_id, observation_type,
          COALESCE(related_entity_id, sentinel))``.
        """
        try:
            result = await self._db.execute(
                text("""
                    INSERT INTO graph_observations
                        (organization_id, project_id, subject_entity_id,
                         related_entity_id, observation_type, content, confidence,
                         supporting_fact_ids, supporting_relationship_ids,
                         valid_from, valid_to, observation_metadata, updated_at)
                    VALUES
                        (:org_id, :project_id, :subject_entity_id,
                         :related_entity_id, :obs_type, :content, :confidence,
                         :fact_ids, :rel_ids, :valid_from, :valid_to,
                         CAST(:obs_metadata AS jsonb), NOW())
                    ON CONFLICT (project_id, subject_entity_id, observation_type,
                                 COALESCE(related_entity_id,
                                  '00000000-0000-0000-0000-000000000000'::uuid))
                    DO UPDATE SET
                        content = EXCLUDED.content,
                        confidence = EXCLUDED.confidence,
                        supporting_fact_ids = EXCLUDED.supporting_fact_ids,
                        supporting_relationship_ids = EXCLUDED.supporting_relationship_ids,
                        valid_from = EXCLUDED.valid_from,
                        valid_to = EXCLUDED.valid_to,
                        observation_metadata = EXCLUDED.observation_metadata,
                        updated_at = NOW()
                    RETURNING id, subject_entity_id, related_entity_id,
                              observation_type, content, confidence,
                              supporting_fact_ids, supporting_relationship_ids,
                              valid_from, valid_to, observation_metadata,
                              created_at, updated_at
                """),
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "subject_entity_id": str(subject_entity_id),
                    "related_entity_id": str(related_entity_id) if related_entity_id else None,
                    "obs_type": observation_type,
                    "content": content,
                    "confidence": confidence,
                    "fact_ids": (
                        [str(fid) for fid in supporting_fact_ids]
                        if supporting_fact_ids else None
                    ),
                    "rel_ids": (
                        [str(rid) for rid in supporting_relationship_ids]
                        if supporting_relationship_ids else None
                    ),
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "obs_metadata": (
                        orjson.dumps(observation_metadata).decode()
                        if observation_metadata else None
                    ),
                },
            )
            return self._row_to_observation(result.one())
        except Exception as exc:
            logger.error(
                "pg_graph.upsert_observation_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "subject_entity_id": str(subject_entity_id),
                    "observation_type": observation_type,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to upsert observation: {exc}",
                detail={
                    "org_id": str(org_id),
                    "subject_entity_id": str(subject_entity_id),
                },
            ) from exc

    async def get_observations(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        subject_entity_id: UUID | None = None,
        observation_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List observations with optional filters and cursor pagination."""
        limit = min(limit, 200)

        where_clause = (
            "o.organization_id = :org_id AND o.project_id = :project_id"
        )
        params: dict[str, object] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "limit": limit + 1,
        }

        if subject_entity_id is not None:
            where_clause += " AND o.subject_entity_id = :subject_id"
            params["subject_id"] = str(subject_entity_id)

        if observation_type is not None:
            where_clause += " AND o.observation_type = :obs_type"
            params["obs_type"] = observation_type

        if cursor:
            try:
                decoded = orjson.loads(base64.b64decode(cursor))
                cursor_created_at = decoded["c"]
                cursor_id = decoded["i"]
                where_clause += (
                    " AND (o.created_at, o.id) > (:cursor_ts, :cursor_id::uuid)"
                )
                params["cursor_ts"] = cursor_created_at
                params["cursor_id"] = cursor_id
            except Exception:
                logger.warning(
                    "pg_graph.get_observations.invalid_cursor",
                    extra={"cursor": cursor},
                )

        try:
            result = await self._db.execute(
                text(f"""
                    SELECT o.id, o.subject_entity_id, o.related_entity_id,
                           o.observation_type, o.content, o.confidence,
                           o.supporting_fact_ids, o.supporting_relationship_ids,
                           o.valid_from, o.valid_to, o.observation_metadata,
                           o.created_at, o.updated_at
                    FROM graph_observations o
                    WHERE {where_clause}
                    ORDER BY o.created_at ASC, o.id ASC
                    LIMIT :limit
                """),
                params,
            )
            rows = result.all()
            has_more = len(rows) > limit
            items = [self._row_to_observation(r) for r in rows[:limit]]

            next_cursor = None
            if has_more and items:
                last = items[-1]
                cursor_payload = orjson.dumps({
                    "c": last["created_at"],
                    "i": last["id"],
                })
                next_cursor = base64.b64encode(cursor_payload).decode()

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except Exception as exc:
            logger.error(
                "pg_graph.get_observations_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "subject_entity_id": str(subject_entity_id) if subject_entity_id else None,
                    "observation_type": observation_type,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get observations: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

    async def get_entity_appearance_timestamps(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
    ) -> list[datetime]:
        """Get all timestamps when an entity appeared in episodes."""
        try:
            result = await self._db.execute(
                text("""
                    SELECT e.created_at AS episode_created_at
                    FROM graph_episode_entities gee
                    JOIN episodes e
                        ON e.id = gee.episode_id
                        AND e.is_deleted = false
                    WHERE gee.project_id = :project_id
                      AND gee.entity_id = :entity_id
                    ORDER BY e.created_at
                """),
                {
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                },
            )
            return [row.episode_created_at for row in result.all()]
        except Exception as exc:
            logger.error(
                "pg_graph.get_entity_appearance_timestamps_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get appearance timestamps for entity {entity_id}: {exc}",
                detail={
                    "org_id": str(org_id),
                    "entity_id": str(entity_id),
                },
            ) from exc

    async def get_relationship_ids_between(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_a_id: UUID,
        entity_b_id: UUID,
    ) -> list[UUID]:
        """Get IDs of direct relationships between two entities (either direction)."""
        try:
            result = await self._db.execute(
                text("""
                    SELECT id FROM graph_relationships
                    WHERE project_id = :project_id
                      AND organization_id = :org_id
                      AND invalid_at IS NULL
                      AND ((source_id = :a AND target_id = :b)
                           OR (source_id = :b AND target_id = :a))
                    ORDER BY created_at DESC
                """),
                {
                    "project_id": str(project_id),
                    "org_id": str(org_id),
                    "a": str(entity_a_id),
                    "b": str(entity_b_id),
                },
            )
            # asyncpg returns UUID columns as uuid.UUID objects
            return [row.id for row in result.all()]
        except Exception as exc:
            logger.error(
                "pg_graph.get_relationship_ids_between_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_a_id": str(entity_a_id),
                    "entity_b_id": str(entity_b_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get relationship IDs between entities: {exc}",
                detail={
                    "org_id": str(org_id),
                    "entity_a_id": str(entity_a_id),
                    "entity_b_id": str(entity_b_id),
                },
            ) from exc

    # ── Group C2: Aggregate Queries (for observation service) ──────────────────

    async def get_total_entity_linked_episode_count(
        self,
        org_id: UUID,
        project_id: UUID,
    ) -> int:
        """Get total distinct episodes that have at least one linked entity.

        Queries ``graph_episode_entities`` via the backend ABC so that this
        works regardless of which graph backend is active.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.

        Returns:
            Number of distinct episodes with linked entities.
        """
        result = await self._db.execute(
            text("""
                SELECT COUNT(DISTINCT episode_id) AS total
                FROM graph_episode_entities
                WHERE project_id = :project_id
                  AND organization_id = :org_id
            """),
            {"project_id": str(project_id), "org_id": str(org_id)},
        )
        row = result.mappings().one_or_none()
        return row["total"] if row else 0

    async def resolve_entity_names(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_ids: list[UUID],
    ) -> dict[str, dict]:
        """Resolve entity IDs to their names and types.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_ids: List of entity UUIDs to resolve.

        Returns:
            Dict keyed by entity ID string with ``name`` and ``entity_type``.
        """
        if not entity_ids:
            return {}
        result = await self._db.execute(
            text("""
                SELECT id, name, entity_type
                FROM graph_entities
                WHERE id = ANY(:entity_ids)
                  AND organization_id = :org_id
                  AND project_id = :project_id
            """),
            {
                "entity_ids": [str(eid) for eid in entity_ids],
                "org_id": str(org_id),
                "project_id": str(project_id),
            },
        )
        rows = result.mappings().all()
        return {
            str(row["id"]): {"name": row["name"], "entity_type": row["entity_type"]}
            for row in rows
        }

    # ── Internal Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_entity(row) -> dict:
        """Convert a DB row to an entity dict matching ``GraphBackend`` spec."""
        return {
            "id": str(row.id),
            "name": row.name,
            "type": row.entity_type,
            "summary": row.summary if row.summary is not None else "",
            "attributes": (
                dict(row.attributes)
                if hasattr(row, "attributes") and row.attributes
                else {}
            ),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _row_to_relationship(row) -> dict:
        """Convert a DB row to a relationship dict."""
        return {
            "id": str(row.id),
            "source_id": str(row.source_id),
            "target_id": str(row.target_id),
            "type": row.relationship_type,
            "properties": row.properties if row.properties is not None else {},
            "fact": row.fact if row.fact is not None else "",
            "confidence": float(row.confidence) if row.confidence is not None else 1.0,
            "valid_from": row.valid_from.isoformat() if row.valid_from else None,
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _row_to_observation(row) -> dict:
        """Convert a DB row to an observation dict matching ``GraphBackend`` spec."""
        return {
            "id": str(row.id),
            "subject_entity_id": str(row.subject_entity_id),
            "related_entity_id": (
                str(row.related_entity_id) if row.related_entity_id else None
            ),
            "observation_type": row.observation_type,
            "content": row.content,
            "confidence": float(row.confidence) if row.confidence is not None else 0.0,
            "supporting_fact_ids": (
                [str(fid) for fid in row.supporting_fact_ids]
                if row.supporting_fact_ids else []
            ),
            "supporting_relationship_ids": (
                [str(rid) for rid in row.supporting_relationship_ids]
                if row.supporting_relationship_ids else []
            ),
            "valid_from": row.valid_from.isoformat() if row.valid_from else None,
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "observation_metadata": (
                dict(row.observation_metadata)
                if row.observation_metadata else {}
            ),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
