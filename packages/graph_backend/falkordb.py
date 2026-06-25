"""FalkorDB-native graph backend — per-tenant graph keys, GraphBLAS traversal.

Implements the ``GraphBackend`` ABC using FalkorDB (a Redis-graph module):

- **Per-tenant graph keys** (``openzep_{org_id}_{project_id}``) guarantee
  tenant isolation at the database level — no ``WHERE org_id`` filters needed.
- **``MERGE``** for atomic entity and relationship upserts.
- **``CALL algo.bfs()``** for single-type/all-types BFS at GraphBLAS speed.
- **Cypher variable-length paths** (``:type1|type2*1..n``) for multi-type
  traversal — a single round-trip, no Python iteration.
- **``CALL db.idx.fulltext.queryNodes()``** for BM25/RediSearch full-text
  search.
- Offset-based pagination with base64 cursors.

Usage::

    from falkordb.asyncio import FalkorDB
    from packages.graph_backend import FalkorGraphBackend

    client = FalkorDB(host="localhost", port=6379)
    backend = FalkorGraphBackend(client=client)
    entity = await backend.create_entity(
        org_id, project_id, name="Acme", entity_type="company",
    )
    results = await backend.traverse(org_id, project_id, entity["id"], max_depth=2)
"""

from __future__ import annotations

import base64
import re
from collections import deque
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import orjson
import structlog
from falkordb.asyncio import FalkorDB

from core.exceptions import ExternalServiceError
from packages.graph_backend.interface import GraphBackend

logger = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_SAFE_EDGE_TYPE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
"""Regex that accepts only safe Cypher relationship-type characters."""

MAX_TRAVERSAL_DEPTH: int = 5
"""Hard cap on BFS depth to prevent unbounded traversals."""

# ── Schema bootstrap queries ───────────────────────────────────────────────

_DEFINE_QUERIES: list[str] = [
    # Range index for entity upsert (MERGE on name)
    "CREATE RANGE INDEX FOR (n:Entity) ON (n.name);",
    # Full-text BM25 index for entity name + summary search
    "CREATE FULLTEXT INDEX FOR (n:Entity) ON (n.name, n.summary) OPTIONS {language: 'english'};",
]

# ── Pagination helpers ────────────────────────────────────────────────────


def _decode_offset_cursor(cursor: str | None) -> int:
    """Decode a base64-encoded offset cursor.

    Cursor format: ``{"o": <offset>}`` serialised with ``orjson`` then
    base64-encoded.
    """
    if not cursor:
        return 0
    try:
        decoded = orjson.loads(base64.b64decode(cursor))
        return int(decoded.get("o", 0))
    except (orjson.JSONDecodeError, ValueError, TypeError):
        logger.warning("falkordb_graph.invalid_cursor", extra={"cursor": cursor})
        return 0


def _encode_offset_cursor(offset: int) -> str:
    """Encode an integer offset as a base64 cursor string."""
    payload = orjson.dumps({"o": offset})
    return base64.b64encode(payload).decode("ascii")


# ── Column indices for RETURN projections ──────────────────────────────────

_E_ID = 0
_E_NAME = 1
_E_TYPE = 2
_E_SUMMARY = 3
_E_ATTRS = 4
_E_CREATED = 5

_R_ID = 0
_R_SRC = 1
_R_TGT = 2
_R_REL_TYPE = 3
_R_PROPS = 4
_R_FACT = 5
_R_CONFIDENCE = 6
_R_VALID_FROM = 7
_R_VALID_TO = 8
_R_CREATED = 9


# ── Backend Implementation ─────────────────────────────────────────────────


class FalkorGraphBackend(GraphBackend):
    """FalkorDB-native graph backend.

    Each org+project pair gets its own isolated FalkorDB graph key
    (``openzep_{org_id}_{project_id}``).  This guarantees tenant isolation
    at the database level — ``algo.bfs()`` and ``queryNodes()`` never
    traverse into another tenant's data.

    Args:
        client: An optional connected ``FalkorDB`` async instance.  When
            ``None``, all methods degrade gracefully (empty results).
        max_traversal_depth: Maximum BFS depth (default 2, max 5).
    """

    def __init__(
        self,
        client: FalkorDB | None = None,
        max_traversal_depth: int = 2,
    ) -> None:
        self._client = client
        self._max_depth = min(max_traversal_depth, MAX_TRAVERSAL_DEPTH)
        # Per-graph-key flag to run index bootstrap only once per tenant.
        self._schema_ensured: dict[str, bool] = {}

    # ── Internal Helpers ──────────────────────────────────────────────────

    def _get_graph(self, org_id: UUID, project_id: UUID):
        """Resolve the per-tenant graph by org+project.

        ``select_graph()`` is a local operation that instantiates a Python
        ``AsyncGraph`` wrapper — no network round-trip.
        """
        if self._client is None:
            return None
        key = f"openzep_{org_id}_{project_id}"
        return self._client.select_graph(key)

    async def _ensure_schema(self, graph) -> None:
        """Idempotently create FalkorDB indexes on a tenant graph.

        FalkorDB returns an "already exists" message for duplicate index
        creation rather than raising an error, so this is safe to call
        multiple times.
        """
        try:
            for q in _DEFINE_QUERIES:
                await graph.query(q)
        except Exception as exc:
            err_str = str(exc).lower()
            if "already exists" in err_str:
                logger.info(
                    "falkordb_graph.schema_already_exists",
                    extra={"graph_key": graph.name},
                )
                return
            logger.warning(
                "falkordb_graph.schema_bootstrap_failed",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "graph_key": graph.name,
                },
            )

    @staticmethod
    def _sanitize_edge_type(name: str) -> str:
        """Validate and return a FalkorDB-safe edge type name.

        Edge type names become relationship types in Cypher patterns
        (``[r:TYPE]``).  Only alphanumeric characters and underscores are
        allowed.

        Args:
            name: The edge type name to validate.

        Returns:
            The same name if valid.

        Raises:
            ValueError: If the name contains unsafe characters.
        """
        if not _SAFE_EDGE_TYPE_RE.match(name):
            raise ValueError(
                f"Unsafe edge type name: {name!r}. "
                "Only [a-zA-Z0-9_] characters are allowed."
            )
        return name

    @staticmethod
    def _parse_json_field(raw: Any) -> dict:
        """Parse a JSON field that may be a string, dict, or None."""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw:
            try:
                return orjson.loads(raw)
            except (orjson.JSONDecodeError, TypeError):
                pass
        return {}

    # ── Row → dict converters ─────────────────────────────────────────────

    @staticmethod
    def _row_to_entity(row: Sequence[Any]) -> dict:
        """Convert a result tuple to an entity dict.

        Expected column order: ``id, name, entity_type, summary,
        attributes, created_at``.
        """
        return {
            "id": str(row[_E_ID]) if row[_E_ID] else "",
            "name": str(row[_E_NAME]) if row[_E_NAME] else "",
            "type": str(row[_E_TYPE]) if row[_E_TYPE] else "",
            "summary": str(row[_E_SUMMARY]) if row[_E_SUMMARY] else "",
            "attributes": (
                FalkorGraphBackend._parse_json_field(row[_E_ATTRS])
                if len(row) > _E_ATTRS
                else {}
            ),
            "created_at": (
                row[_E_CREATED].isoformat()
                if len(row) > _E_CREATED
                and hasattr(row[_E_CREATED], "isoformat")
                else str(row[_E_CREATED]) if row[_E_CREATED] else None
            ),
        }

    @staticmethod
    def _row_to_relationship(row: Sequence[Any]) -> dict:
        """Convert a result tuple to a relationship dict.

        Expected column order: ``id, source_id, target_id, type,
        properties, fact, confidence, valid_from, valid_to, created_at``.
        """
        return {
            "id": str(row[_R_ID]) if row[_R_ID] else "",
            "source_id": str(row[_R_SRC]) if row[_R_SRC] else "",
            "target_id": str(row[_R_TGT]) if row[_R_TGT] else "",
            "type": str(row[_R_REL_TYPE]) if row[_R_REL_TYPE] else "",
            "properties": (
                FalkorGraphBackend._parse_json_field(row[_R_PROPS])
                if len(row) > _R_PROPS
                else {}
            ),
            "fact": str(row[_R_FACT]) if len(row) > _R_FACT and row[_R_FACT] else "",
            "confidence": (
                float(row[_R_CONFIDENCE])
                if len(row) > _R_CONFIDENCE and row[_R_CONFIDENCE] is not None
                else 1.0
            ),
            "valid_from": (
                row[_R_VALID_FROM].isoformat()
                if len(row) > _R_VALID_FROM
                and hasattr(row[_R_VALID_FROM], "isoformat")
                else str(row[_R_VALID_FROM]) if len(row) > _R_VALID_FROM and row[_R_VALID_FROM] else None
            ),
            "valid_to": (
                row[_R_VALID_TO].isoformat()
                if len(row) > _R_VALID_TO
                and hasattr(row[_R_VALID_TO], "isoformat")
                else str(row[_R_VALID_TO]) if len(row) > _R_VALID_TO and row[_R_VALID_TO] else None
            ),
            "created_at": (
                row[_R_CREATED].isoformat()
                if len(row) > _R_CREATED
                and hasattr(row[_R_CREATED], "isoformat")
                else str(row[_R_CREATED]) if len(row) > _R_CREATED and row[_R_CREATED] else None
            ),
        }

    # ── Entity CRUD ─────────────────────────────────────────────────────────

    async def create_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict:
        """Create or update an entity node (upsert by name per tenant).

        Uses ``MERGE`` on the ``name`` property within the per-tenant graph
        (so names are unique within a tenant).  ``ON MATCH`` upgrades the
        entity type if the existing type is ``"Custom"`` and updates the
        summary if a non-empty value is provided — matching the behaviour of
        the Postgres and SurrealDB backends.

        Returns:
            The created or updated entity dict.

        Raises:
            ExternalServiceError: If the query fails.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            raise ExternalServiceError(
                message="FalkorDB not connected",
                detail={"reason": "client is None"},
            )

        key = f"openzep_{org_id}_{project_id}"
        if not self._schema_ensured.get(key):
            await self._ensure_schema(graph)
            self._schema_ensured[key] = True

        name_lower = name.lower().strip()
        summary_val = summary or ""
        entity_id = str(uuid4())
        now_str = datetime.now(timezone.utc).isoformat()

        try:
            result = await graph.query(
                """
                MERGE (n:Entity {name: $name})
                ON CREATE SET
                    n.id = $id,
                    n.organization_id = $org_id,
                    n.project_id = $project_id,
                    n.entity_type = $type,
                    n.summary = $summary,
                    n.attributes = $attributes,
                    n.created_at = $now,
                    n.updated_at = $now
                ON MATCH SET
                    n.entity_type = CASE
                        WHEN n.entity_type = 'Custom' AND $type != 'Custom'
                        THEN $type ELSE n.entity_type
                    END,
                    n.summary = CASE WHEN $summary != '' THEN $summary ELSE n.summary END,
                    n.updated_at = $now
                RETURN n.id, n.name, n.entity_type, n.summary, n.attributes, n.created_at
                """,
                {
                    "name": name_lower,
                    "id": entity_id,
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "type": entity_type,
                    "summary": summary_val,
                    "attributes": orjson.dumps({}).decode("utf-8"),
                    "now": now_str,
                },
            )
            row = result.result_set[0]
            entity = self._row_to_entity(row)

            logger.info(
                "falkordb_graph.entity_upserted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": entity["id"],
                    "entity_type": entity_type,
                    "name": name_lower,
                },
            )
            return entity
        except Exception as exc:
            logger.error(
                "falkordb_graph.create_entity_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "name": name_lower,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to create entity '{name_lower}': {exc}",
                detail={"org_id": str(org_id), "name": name_lower},
            ) from exc

    async def get_entity(
        self, org_id: UUID, project_id: UUID, entity_id: UUID
    ) -> dict | None:
        """Retrieve an entity node by its ID.

        Args:
            org_id: Organisational scope (used to derive the tenant graph).
            project_id: Project scope (used to derive the tenant graph).
            entity_id: The UUID of the entity to fetch.

        Returns:
            The entity dict, or ``None`` if not found.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return None

        try:
            result = await graph.query(
                """
                MATCH (n:Entity {id: $id})
                RETURN n.id, n.name, n.entity_type, n.summary, n.attributes, n.created_at
                """,
                {"id": str(entity_id)},
            )
            if not result.result_set:
                return None
            return self._row_to_entity(result.result_set[0])
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_entity_failed",
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

    async def delete_entity(
        self, org_id: UUID, project_id: UUID, entity_id: UUID
    ) -> bool:
        """Remove an entity node from the graph.

        ``DETACH DELETE`` automatically removes all incident edges (cascade).

        Returns:
            ``True`` if the entity was deleted, ``False`` if it did not exist.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return False

        try:
            result = await graph.query(
                """
                MATCH (n:Entity {id: $id})
                DETACH DELETE n
                RETURN count(n) AS deleted
                """,
                {"id": str(entity_id)},
            )
            deleted_count = result.result_set[0][0] if result.result_set else 0
            deleted = deleted_count > 0
            if deleted:
                logger.info(
                    "falkordb_graph.entity_deleted",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "entity_id": str(entity_id),
                    },
                )
            return deleted
        except Exception as exc:
            logger.error(
                "falkordb_graph.delete_entity_failed",
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

    async def update_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
        name: str | None = None,
        summary: str | None = None,
        entity_type: str | None = None,
        attributes: dict | None = None,
    ) -> dict | None:
        """Update entity fields.  Only provided fields are changed.

        Builds a dynamic ``SET`` clause with only the non-``None`` fields.
        Returns the updated entity dict, or ``None`` if not found.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return None

        set_parts: list[str] = []
        params: dict[str, object] = {"id": str(entity_id)}

        if name is not None:
            params["name"] = name.lower().strip()
            set_parts.append("n.name = $name")
        if summary is not None:
            params["summary"] = summary
            set_parts.append("n.summary = $summary")
        if entity_type is not None:
            params["entity_type"] = entity_type
            set_parts.append("n.entity_type = $entity_type")
        if attributes is not None:
            params["attributes"] = orjson.dumps(attributes).decode("utf-8")
            set_parts.append("n.attributes = $attributes")

        if not set_parts:
            return await self.get_entity(org_id, project_id, entity_id)

        set_clause = ", ".join(set_parts)
        params["now"] = datetime.now(timezone.utc).isoformat()

        try:
            result = await graph.query(
                f"""
                MATCH (n:Entity {{id: $id}})
                SET {set_clause}, n.updated_at = $now
                RETURN n.id, n.name, n.entity_type, n.summary, n.attributes, n.created_at
                """,
                params,
            )
            if not result.result_set:
                return None
            return self._row_to_entity(result.result_set[0])
        except Exception as exc:
            logger.error(
                "falkordb_graph.update_entity_failed",
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

    # ── Relationship CRUD ──────────────────────────────────────────────────

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

        Uses ``MERGE`` on the pattern ``(s)-[r:TYPE]->(t)`` within the
        per-tenant graph.  ``ON MATCH`` updates properties, confidence
        (takes the max), and temporal validity fields — matching the upsert
        behaviour of the Postgres and SurrealDB backends.

        A UUID is generated for each relationship and stored as ``r.id`` so
        the backend can reference specific relationships.  ``source_id`` and
        ``target_id`` are stored as explicit properties (FalkorDB's internal
        integer node IDs are not usable for our UUID-based references).

        Args:
            org_id: Tenant scope (used to derive the graph key).
            project_id: Project scope (used to derive the graph key).
            source_id: UUID of the source entity.
            target_id: UUID of the target entity.
            relationship_type: Edge label (sanitised to safe chars).
            properties: Optional key-value metadata.
            confidence: Optional confidence score (default 1.0).
            valid_from: Optional temporal validity start.
            valid_to: Optional temporal validity end.

        Returns:
            The created or updated relationship dict.

        Raises:
            ValueError: If the relationship type contains unsafe characters.
            ExternalServiceError: If the query fails.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            raise ExternalServiceError(
                message="FalkorDB not connected",
                detail={"reason": "client is None"},
            )

        safe_type = self._sanitize_edge_type(relationship_type)
        rel_id = str(uuid4())
        now_str = datetime.now(timezone.utc).isoformat()

        params: dict[str, object] = {
            "source_id": str(source_id),
            "target_id": str(target_id),
            "rel_id": rel_id,
            "org_id": str(org_id),
            "project_id": str(project_id),
            "properties": orjson.dumps(properties or {}).decode("utf-8"),
            "confidence": confidence if confidence is not None else 1.0,
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_to": valid_to.isoformat() if valid_to else None,
            "now": now_str,
        }

        try:
            result = await graph.query(
                f"""
                MERGE (s:Entity {{id: $source_id}})-[r:{safe_type}]->(t:Entity {{id: $target_id}})
                ON CREATE SET
                    r.id = $rel_id,
                    r.organization_id = $org_id,
                    r.project_id = $project_id,
                    r.source_id = $source_id,
                    r.target_id = $target_id,
                    r.properties = $properties,
                    r.fact = '',
                    r.confidence = $confidence,
                    r.valid_from = $valid_from,
                    r.valid_to = $valid_to,
                    r.created_at = $now,
                    r.updated_at = $now
                ON MATCH SET
                    r.properties = $properties,
                    r.confidence = CASE WHEN $confidence > r.confidence
                        THEN $confidence ELSE r.confidence END,
                    r.valid_from = CASE WHEN $valid_from IS NOT NULL
                        THEN $valid_from ELSE r.valid_from END,
                    r.valid_to = CASE WHEN $valid_to IS NOT NULL
                        THEN $valid_to ELSE r.valid_to END,
                    r.updated_at = $now
                RETURN r.id, r.source_id, r.target_id, type(r) AS rel_type,
                       r.properties, r.fact, r.confidence,
                       r.valid_from, r.valid_to, r.created_at
                """,
                params,
            )
            row = result.result_set[0]
            relationship = self._row_to_relationship(row)

            logger.info(
                "falkordb_graph.relationship_upserted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "type": relationship_type,
                    "rel_id": rel_id,
                },
            )
            return relationship
        except ValueError:
            raise
        except Exception as exc:
            logger.error(
                "falkordb_graph.create_relationship_failed",
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

    async def get_relationships(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
        relationship_type: str | None = None,
        at_time: datetime | None = None,
    ) -> list[dict]:
        """Get all active relationships for an entity (bidirectional).

        Uses explicit ``r.source_id`` / ``r.target_id`` properties rather
        than ``startNode(r)`` / ``endNode(r)`` because FalkorDB's internal
        node IDs are integers, not our UUID-based references.

        Args:
            org_id: Tenant scope.
            project_id: Project scope.
            entity_id: The entity whose relationships to fetch.
            relationship_type: Optional filter by type.
            at_time: Only return relationships valid at this time
                (defaults to now).

        Returns:
            List of relationship dicts (limited to 201).
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        at_time = at_time or datetime.now(timezone.utc)
        params: dict[str, object] = {
            "eid": str(entity_id),
            "at_time": at_time.isoformat(),
        }

        type_clause = ""
        if relationship_type:
            safe_type = self._sanitize_edge_type(relationship_type)
            type_clause = f"AND type(r) = '{safe_type}'"

        try:
            result = await graph.query(
                f"""
                MATCH (n:Entity {{id: $eid}})-[r]-(:Entity)
                WHERE r.invalid_at IS NULL
                  {type_clause}
                  AND (r.valid_from IS NULL OR r.valid_from <= $at_time)
                  AND (r.valid_to IS NULL OR r.valid_to >= $at_time)
                RETURN r.id, r.source_id, r.target_id, type(r) AS rel_type,
                       r.properties, r.fact, r.confidence,
                       r.valid_from, r.valid_to, r.created_at
                ORDER BY r.created_at DESC
                LIMIT 201
                """,
                params,
            )
            return [self._row_to_relationship(row) for row in result.result_set]
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_relationships_failed",
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

    async def expire_relationship(
        self,
        org_id: UUID,
        project_id: UUID,
        relationship_id: UUID,
    ) -> bool:
        """Mark a relationship as invalidated (soft-delete).

        Sets ``invalid_at`` to the current timestamp.  Returns ``True`` if
        the relationship was found and expired.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return False

        now_str = datetime.now(timezone.utc).isoformat()
        try:
            result = await graph.query(
                """
                MATCH ()-[r]->()
                WHERE r.id = $rel_id
                  AND r.invalid_at IS NULL
                SET r.invalid_at = $now
                RETURN count(r) AS updated
                """,
                {"rel_id": str(relationship_id), "now": now_str},
            )
            return result.result_set[0][0] > 0 if result.result_set else False
        except Exception as exc:
            logger.error(
                "falkordb_graph.expire_relationship_failed",
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

    # ── Traversal (iterative BFS with exact depth tracking) ────────────────

    async def traverse(
        self,
        org_id: UUID,
        project_id: UUID,
        start_node_id: UUID,
        max_depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> list[dict]:
        """Traverse the graph outward from a starting node.

        Uses an iterative BFS in Python so every returned node has an exact
        ``depth`` field.  At each hop, neighbours are discovered with a
        single Cypher query (exploiting per-tenant graph isolation for
        speed).

        Traversal strategy:
        - ``edge_types is None``: all relationship types followed (``[r]``).
        - Single-element list: native ``algo.bfs()`` via GraphBLAS.
        - Multi-element list: Cypher variable-length path
          (``:type1|type2*1..n``).
        - Empty list: returns just the start node.

        Args:
            org_id: Tenant scope (derives the isolated graph key).
            project_id: Project scope (derives the isolated graph key).
            start_node_id: UUID of the node to start from.
            max_depth: Maximum hops (capped at MAX_TRAVERSAL_DEPTH).
            edge_types: ``None`` = all types; ``[]`` = no edges
                (returns just start node); specific list = filter by type.

        Returns:
            List of node dicts with ``depth`` key (0 = start node).
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        # Empty edge_types — return just the start node
        if edge_types is not None and len(edge_types) == 0:
            start = await self.get_entity(org_id, project_id, start_node_id)
            if start is None:
                return []
            start["depth"] = 0
            return [start]

        max_depth = min(max_depth, self._max_depth)
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque()
        queue.append((str(start_node_id), 0))
        nodes: list[dict] = []

        while queue:
            current_id, depth = queue.popleft()
            if current_id in visited:
                continue
            visited.add(current_id)

            # Fetch the current node and tag it with its BFS depth
            entity = await self.get_entity(org_id, project_id, UUID(current_id))
            if entity:
                entity["depth"] = depth
                nodes.append(entity)

            if depth >= max_depth:
                continue

            # Discover neighbours in a single round-trip
            try:
                if edge_types is not None and len(edge_types) == 1:
                    # Single type -> algo.bfs for C-level GraphBLAS speed
                    safe_type = self._sanitize_edge_type(edge_types[0])
                    result = await graph.query(
                        f"""
                        MATCH (n:Entity {{id: $eid}})
                        CALL algo.bfs(n, 1, '{safe_type}') YIELD nodes
                        UNWIND nodes AS node
                        RETURN DISTINCT node.id
                        """,
                        {"eid": current_id},
                    )
                elif edge_types is not None and len(edge_types) > 1:
                    # Multi-type -> Cypher variable-length path (single hop)
                    safe_types = "|".join(
                        self._sanitize_edge_type(et) for et in edge_types
                    )
                    result = await graph.query(
                        f"""
                        MATCH (n:Entity {{id: $eid}})-[r:{safe_types}]-(neighbour:Entity)
                        RETURN DISTINCT neighbour.id
                        """,
                        {"eid": current_id},
                    )
                else:
                    # All types -> wildcard
                    result = await graph.query(
                        """
                        MATCH (n:Entity {id: $eid})-[r]-(neighbour:Entity)
                        RETURN DISTINCT neighbour.id
                        """,
                        {"eid": current_id},
                    )

                for row in result.result_set:
                    neighbour_id = str(row[0])
                    if neighbour_id and neighbour_id not in visited:
                        queue.append((neighbour_id, depth + 1))
            except ValueError:
                raise
            except Exception as exc:
                logger.warning(
                    "falkordb_graph.traverse.neighbour_fetch_failed",
                    extra={
                        "entity_id": current_id,
                        "depth": depth,
                        "error": str(exc),
                    },
                )

        return nodes

    # ── Search ─────────────────────────────────────────────────────────────

    async def search_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        query: str,
        types: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Search entity nodes by name or summary using BM25 full-text search.

        Uses FalkorDB's RediSearch-backed full-text index with
        ``CALL db.idx.fulltext.queryNodes()``.  Results are ordered by
        relevance score descending.

        Args:
            org_id: Tenant scope (derives the isolated graph key).
            project_id: Project scope (derives the isolated graph key).
            query: Free-text search string.
            types: Optional filter — only return entities with these
                ``entity_type`` values.
            limit: Maximum results to return.
            offset: Number of results to skip.

        Returns:
            A list of matching entity dicts with a ``score`` key.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        try:
            result = await graph.query(
                """
                CALL db.idx.fulltext.queryNodes('Entity', $query) YIELD node, score
                WHERE $entity_types_null OR node.entity_type IN $entity_types
                RETURN node.id, node.name, node.entity_type, node.summary,
                       node.attributes, node.created_at, score
                ORDER BY score DESC
                """,
                {
                    "query": query,
                    "entity_types": types or [],
                    "entity_types_null": types is None,
                },
            )

            # Slice in Python since FalkorDB SKIP/LIMIT with params can be
            # unreliable in some versions.
            rows = result.result_set or []
            sliced = rows[offset : offset + limit]
            entities = []
            for row in sliced:
                entity = self._row_to_entity(row[:6])
                entity["score"] = (
                    float(row[6]) if len(row) > 6 and row[6] is not None else 0.0
                )
                entities.append(entity)
            return entities
        except Exception as exc:
            logger.error(
                "falkordb_graph.search_entities_failed",
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

    # ── Entity Listing ─────────────────────────────────────────────────────

    async def list_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List entity nodes with optional type filter and offset pagination.

        Returns:
            A dict with ``items`` (list of entity dicts), ``next_cursor``
            (str or ``None``), and ``has_more`` (bool).
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return {"items": [], "next_cursor": None, "has_more": False}

        limit = min(limit, 200)
        offset = _decode_offset_cursor(cursor)

        params: dict[str, object] = {}
        where_clause = "1 = 1"
        if entity_type:
            where_clause = "n.entity_type = $entity_type"
            params["entity_type"] = entity_type

        try:
            result = await graph.query(
                f"""
                MATCH (n:Entity)
                WHERE {where_clause}
                RETURN n.id, n.name, n.entity_type, n.summary, n.attributes, n.created_at
                ORDER BY n.created_at ASC, n.id ASC
                LIMIT {limit + 1}
                """,
                params,
            )
            rows = result.result_set
            has_more = len(rows) > limit
            items = [self._row_to_entity(r) for r in rows[:limit]]

            next_cursor = None
            if has_more and items:
                next_cursor = _encode_offset_cursor(offset + len(items))

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except Exception as exc:
            logger.error(
                "falkordb_graph.list_entities_failed",
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
        """List all edges incident to an entity with offset pagination.

        Returns:
            A dict with ``items`` (list of edge dicts), ``next_cursor``,
            and ``has_more``.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return {"items": [], "next_cursor": None, "has_more": False}

        limit = min(limit, 200)
        offset = _decode_offset_cursor(cursor)

        params: dict[str, object] = {"eid": str(entity_id)}
        type_filter = ""
        if predicate:
            safe_pred = self._sanitize_edge_type(predicate)
            type_filter = f"AND type(r) = '{safe_pred}'"

        try:
            result = await graph.query(
                f"""
                MATCH (n:Entity {{id: $eid}})-[r]-(:Entity)
                WHERE r.invalid_at IS NULL {type_filter}
                RETURN r.id, r.source_id, r.target_id, type(r) AS rel_type,
                       r.properties, r.fact, r.confidence,
                       r.valid_from, r.valid_to, r.created_at
                ORDER BY r.created_at DESC
                LIMIT {limit + 1}
                """,
                params,
            )
            rows = result.result_set
            has_more = len(rows) > limit
            items = [self._row_to_relationship(r) for r in rows[:limit]]

            next_cursor = None
            if has_more and items:
                next_cursor = _encode_offset_cursor(offset + len(items))

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except ValueError:
            raise
        except Exception as exc:
            logger.error(
                "falkordb_graph.list_entity_edges_failed",
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
        """Retrieve a single entity node with all its incident edges.

        Returns:
            A dict with ``node`` (entity dict) and ``edges`` (list of edge
            dicts), or ``None`` if the entity does not exist.
        """
        entity = await self.get_entity(org_id, project_id, entity_id)
        if entity is None:
            return None
        edges_result = await self.list_entity_edges(org_id, project_id, entity_id)
        return {"node": entity, "edges": edges_result["items"]}

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

        Combines entity BM25 full-text search with BFS traversal.  Steps:
        1. Search entities matching the query.
        2. For each matched entity, BFS-traverse to depth ``max_depth``.
        3. Deduplicate by entity id.
        4. Sort by distance ascending and limit.

        Returns:
            Entity dicts with ``id``, ``name``, ``type``, ``summary``, and
            ``distance`` keys.  Distance 0 = directly matched.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

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
                    eid = UUID(entity_id_str)
                except (ValueError, TypeError):
                    continue

                try:
                    related = await self.traverse(
                        org_id=org_id,
                        project_id=project_id,
                        start_node_id=eid,
                        max_depth=max_depth,
                    )
                except Exception:
                    logger.warning(
                        "falkordb_graph.retrieve_graph.traverse_failed",
                        extra={"entity_id": entity_id_str, "query": query},
                    )
                    continue

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

        except Exception:
            logger.warning(
                "falkordb_graph.retrieve_graph_failed",
                extra={"query": query},
                exc_info=True,
            )
            return []

    # ── Health ─────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Verify the FalkorDB connection is alive."""
        if self._client is None:
            return False
        try:
            graph = self._client.select_graph("_health_check")
            await graph.query("RETURN 1")
            return True
        except Exception:
            return False
