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
import json
from datetime import datetime, timezone
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import ExternalServiceError
from packages.graphiti_client.interface import GraphBackend

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

    UNION

    -- Recursive: follow active edges (both directions)
    SELECT DISTINCT e.id, e.name, e.entity_type, e.summary,
           e.attributes, e.created_at, bfs.depth + 1
    FROM bfs
    JOIN graph_relationships r
        ON (r.source_id = bfs.id OR r.target_id = bfs.id)
        AND r.invalid_at IS NULL
    JOIN graph_entities e
        ON (e.id = CASE
            WHEN r.source_id = bfs.id THEN r.target_id
            ELSE r.source_id
        END)
    WHERE bfs.depth < :max_depth
      AND e.organization_id = :org_id
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
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict:
        """Create a new entity node.

        Raises:
            ExternalServiceError: If the insert fails.
        """
        try:
            result = await self._db.execute(
                text(
                    """
                    INSERT INTO graph_entities
                        (organization_id, name, entity_type, summary)
                    VALUES (:org_id, :name, :type, :summary)
                    RETURNING id, name, entity_type, summary, attributes, created_at
                    """
                ),
                {
                    "org_id": str(org_id),
                    "name": name,
                    "type": entity_type,
                    "summary": summary or "",
                },
            )
            row = result.one()
            entity = self._row_to_entity(row)
            logger.info(
                "pg_graph.entity_created",
                extra={
                    "org_id": str(org_id),
                    "entity_id": entity["id"],
                    "entity_type": entity_type,
                },
            )
            return entity
        except Exception as exc:
            logger.error(
                "pg_graph.create_entity_failed",
                extra={"org_id": str(org_id), "name": name, "error": str(exc)},
            )
            raise ExternalServiceError(
                message=f"Failed to create entity '{name}': {exc}",
                detail={"org_id": str(org_id), "name": name},
            ) from exc

    async def get_entity(self, org_id: UUID, entity_id: UUID) -> dict | None:
        """Retrieve an entity node by ID."""
        try:
            result = await self._db.execute(
                text(
                    """
                    SELECT id, name, entity_type, summary, attributes, created_at
                    FROM graph_entities
                    WHERE id = :entity_id AND organization_id = :org_id
                    """
                ),
                {"entity_id": str(entity_id), "org_id": str(org_id)},
            )
            row = result.one_or_none()
            return self._row_to_entity(row) if row else None
        except Exception as exc:
            logger.error(
                "pg_graph.get_entity_failed",
                extra={
                    "org_id": str(org_id),
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
        entity_id: UUID,
        name: str | None = None,
        summary: str | None = None,
        entity_type: str | None = None,
        attributes: dict | None = None,
    ) -> dict | None:
        """Update entity fields. Only provided fields are changed.

        Returns the updated entity dict, or ``None`` if not found.
        """
        updates: dict[str, object] = {}
        update_cols: list[str] = []

        if name is not None:
            updates["name"] = name
            update_cols.append("name = :name")
        if summary is not None:
            updates["summary"] = summary
            update_cols.append("summary = :summary")
        if entity_type is not None:
            updates["entity_type"] = entity_type
            update_cols.append("entity_type = :entity_type")
        if attributes is not None:
            updates["attributes_json"] = json.dumps(attributes)
            update_cols.append("attributes = CAST(:attributes_json AS jsonb)")

        if not update_cols:
            return await self.get_entity(org_id, entity_id)

        updates["org_id"] = str(org_id)
        updates["entity_id"] = str(entity_id)
        set_clause = ", ".join(update_cols)

        try:
            result = await self._db.execute(
                text(
                    f"""
                    UPDATE graph_entities
                    SET {set_clause}, updated_at = now()
                    WHERE id = :entity_id AND organization_id = :org_id
                    RETURNING id, name, entity_type, summary, attributes, created_at
                    """
                ),
                updates,
            )
            row = result.one_or_none()
            return self._row_to_entity(row) if row else None
        except Exception as exc:
            logger.error(
                "pg_graph.update_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "entity_id": str(entity_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to update entity {entity_id}: {exc}",
                detail={"org_id": str(org_id), "entity_id": str(entity_id)},
            ) from exc

    async def delete_entity(self, org_id: UUID, entity_id: UUID) -> bool:
        """Delete an entity — cascades to relationships and episode links.

        Returns:
            ``True`` if the entity existed and was deleted.
        """
        try:
            result = await self._db.execute(
                text(
                    """
                    DELETE FROM graph_entities
                    WHERE id = :entity_id AND organization_id = :org_id
                    RETURNING id
                    """
                ),
                {"entity_id": str(entity_id), "org_id": str(org_id)},
            )
            deleted = result.rowcount > 0
            if deleted:
                logger.info(
                    "pg_graph.entity_deleted",
                    extra={"org_id": str(org_id), "entity_id": str(entity_id)},
                )
            return deleted
        except Exception as exc:
            logger.error(
                "pg_graph.delete_entity_failed",
                extra={
                    "org_id": str(org_id),
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
        source_id: UUID,
        target_id: UUID,
        relationship_type: str,
        properties: dict | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> dict:
        """Create a directed relationship between two entities.

        Temporal semantics:
        - ``valid_from`` defaults to now().
        - ``valid_to`` and ``invalid_at`` are NULL (active until invalidated).

        Raises:
            ExternalServiceError: On FK or unique constraint violations.
        """
        try:
            # Use a savepoint so a failed relationship does NOT roll back
            # entity inserts that happened earlier in the same transaction.
            async with self._db.begin_nested():
                result = await self._db.execute(
                    text(
                        """
                        INSERT INTO graph_relationships
                            (organization_id, source_id, target_id,
                             relationship_type, properties, fact, confidence,
                             valid_from, valid_to, created_at)
                        VALUES
                            (:org_id, :source_id, :target_id,
                             :rel_type, CAST(:properties AS jsonb), :fact, :confidence,
                             COALESCE(:valid_from, now()), :valid_to, now())
                        RETURNING id, source_id, target_id, relationship_type,
                                  properties, fact, confidence,
                                  valid_from, valid_to, created_at
                        """
                    ),
                    {
                        "org_id": str(org_id),
                        "source_id": str(source_id),
                        "target_id": str(target_id),
                        "rel_type": relationship_type,
                        "properties": json.dumps(properties or {}),
                        "fact": "",
                        "confidence": 1.0,
                        "valid_from": valid_from.isoformat() if valid_from else None,
                        "valid_to": valid_to.isoformat() if valid_to else None,
                    },
                )
                row = result.one()
            relationship = self._row_to_relationship(row)
            logger.info(
                "pg_graph.relationship_created",
                extra={
                    "org_id": str(org_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "type": relationship_type,
                },
            )
            return relationship
        except Exception as exc:
            # Duplicate relationships (unique constraint) are expected when
            # multiple episodes extract the same fact. The savepoint ensures
            # entities from earlier operations are not affected.
            logger.warning(
                "pg_graph.create_relationship_duplicate",
                extra={
                    "org_id": str(org_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "relationship_type": relationship_type,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Duplicate relationship '{relationship_type}': {exc}",
                detail={
                    "org_id": str(org_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                },
            ) from exc

    async def expire_relationship(
        self,
        org_id: UUID,
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
                      AND invalid_at IS NULL
                    RETURNING id
                    """
                ),
                {"rel_id": str(relationship_id), "org_id": str(org_id)},
            )
            return result.rowcount > 0
        except Exception as exc:
            logger.error(
                "pg_graph.expire_relationship_failed",
                extra={
                    "org_id": str(org_id),
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
        entity_id: UUID,
        relationship_type: str | None = None,
        at_time: datetime | None = None,
    ) -> list[dict]:
        """Get all active relationships for an entity.

        Args:
            org_id: Tenant scope.
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
            AND (r.source_id = :entity_id OR r.target_id = :entity_id)
            AND r.invalid_at IS NULL
            AND (r.valid_from IS NULL OR r.valid_from <= :at_time)
            AND (r.valid_to IS NULL OR r.valid_to >= :at_time)
        """
        params: dict[str, object] = {
            "org_id": str(org_id),
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
            start = await self.get_entity(org_id, start_node_id)
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
            start = await self.get_entity(org_id, start_id)
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
            entity = await self.get_entity(org_id, UUID(current_id))
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
                          AND r.invalid_at IS NULL
                          AND (r.source_id = :eid OR r.target_id = :eid)
                          AND (:types_null OR r.relationship_type = ANY(:edge_types))
                        """
                    ),
                    {
                        "eid": current_id,
                        "org_id": str(org_id),
                        "types_null": not type_filter,
                        "edge_types": edge_types or [],
                    },
                )
                for row in result.all():
                    neighbour_id = str(row.neighbour_id)
                    if neighbour_id not in visited:
                        queue.append((neighbour_id, depth + 1))
            except Exception as exc:
                logger.warning(
                    "pg_graph.traverse_iterative.neighbour_fetch_failed",
                    extra={
                        "org_id": str(org_id),
                        "entity_id": current_id,
                        "error": str(exc),
                    },
                )

        return nodes

    # ── Search ───────────────────────────────────────────────────────────────

    async def search_entities(
        self,
        org_id: UUID,
        query: str,
        types: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Search entities by name or summary.

        Combines trigram similarity (fuzzy name match) with full-text search
        on summaries.  Weighted 60% name match, 40% summary match.

        Returns entities sorted by relevance score descending.
        """
        try:
            # Pass entity_types_null flag to handle None vs typed array
            result = await self._db.execute(
                text(SEARCH_ENTITIES_SQL),
                {
                    "org_id": str(org_id),
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
                entity["score"] = float(row.score) if hasattr(row, "score") and row.score is not None else 0.0
                entities.append(entity)
            return entities
        except Exception as exc:
            logger.error(
                "pg_graph.search_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "query": query,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Entity search failed: {exc}",
                detail={"org_id": str(org_id), "query": query},
            ) from exc

    # ── Entity Listing ────────────────────────────────────────────────────────

    async def list_entities(
        self,
        org_id: UUID,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List entities with cursor-based pagination.

        Cursor format: base64-encoded JSON ``{"c": "<created_at>", "i": "<id>"}``
        matching the pattern used by ``UserRepository`` and ``SessionRepository``.
        """
        limit = min(limit, 200)

        where_clause = "ge.organization_id = :org_id"
        params: dict[str, object] = {"org_id": str(org_id), "limit": limit + 1}

        if entity_type:
            where_clause += " AND ge.entity_type = :entity_type"
            params["entity_type"] = entity_type

        if cursor:
            try:
                decoded = json.loads(base64.b64decode(cursor))
                cursor_created_at = decoded["c"]
                cursor_id = decoded["i"]
                where_clause += (
                    " AND (ge.created_at, ge.id) > (:cursor_ts, :cursor_id::uuid)"
                )
                params["cursor_ts"] = cursor_created_at
                params["cursor_id"] = cursor_id
            except Exception:
                logger.warning("pg_graph.list_entities.invalid_cursor", extra={"cursor": cursor})

        try:
            query = LIST_ENTITIES_SQL.format(where_clause=where_clause)
            result = await self._db.execute(text(query), params)
            rows = result.all()
            has_more = len(rows) > limit
            items = [self._row_to_entity(r) for r in rows[:limit]]

            next_cursor = None
            if has_more and items:
                last = items[-1]
                cursor_payload = json.dumps(
                    {"c": last["created_at"], "i": last["id"]}
                )
                next_cursor = base64.b64encode(cursor_payload.encode()).decode()

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except Exception as exc:
            logger.error(
                "pg_graph.list_entities_failed",
                extra={"org_id": str(org_id), "entity_type": entity_type, "error": str(exc)},
            )
            raise ExternalServiceError(
                message=f"Failed to list entities: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

    async def list_entity_edges(
        self,
        org_id: UUID,
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
            AND (r.source_id = :eid OR r.target_id = :eid)
        """
        params: dict[str, object] = {
            "org_id": str(org_id),
            "eid": str(entity_id),
            "limit": limit + 1,
        }

        if predicate:
            conditions += " AND r.relationship_type = :pred"
            params["pred"] = predicate

        if cursor:
            try:
                decoded = json.loads(base64.b64decode(cursor))
                conditions += " AND (r.created_at, r.id) > (:cursor_ts, :cursor_id::uuid)"
                params["cursor_ts"] = decoded["c"]
                params["cursor_id"] = decoded["i"]
            except Exception:
                logger.warning("pg_graph.list_entity_edges.invalid_cursor", extra={"cursor": cursor})

        try:
            query = LIST_RELATIONSHIPS_SQL.format(where_clause=conditions)
            result = await self._db.execute(text(query), params)
            rows = result.all()
            has_more = len(rows) > limit
            items = [self._row_to_relationship(r) for r in rows[:limit]]

            next_cursor = None
            if has_more and items:
                last = items[-1]
                cursor_payload = json.dumps(
                    {"c": last["created_at"], "i": last["id"]}
                )
                next_cursor = base64.b64encode(cursor_payload.encode()).decode()

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except Exception as exc:
            logger.error(
                "pg_graph.list_entity_edges_failed",
                extra={
                    "org_id": str(org_id),
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
        entity_id: UUID,
    ) -> dict | None:
        """Retrieve an entity with all its incident edges."""
        entity = await self.get_entity(org_id, entity_id)
        if entity is None:
            return None
        edges = await self.get_relationships(org_id, entity_id)
        return {"node": entity, "edges": edges}

    # ── Health ────────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Verify the PostgreSQL connection is alive."""
        try:
            await self._db.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ── Internal Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_entity(row) -> dict:
        """Convert a DB row to an entity dict matching ``GraphBackend`` spec."""
        return {
            "id": str(row.id),
            "name": row.name,
            "type": row.entity_type,
            "summary": row.summary or "",
            "attributes": (
                dict(row.attributes) if hasattr(row, "attributes") and row.attributes else {}
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
            "properties": row.properties or {},
            "fact": row.fact or "",
            "confidence": float(row.confidence) if row.confidence is not None else 1.0,
            "valid_from": row.valid_from.isoformat() if row.valid_from else None,
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
