"""FalkorDB-native graph backend — per-tenant graph keys, GraphBLAS traversal.

Implements the ``GraphBackend`` ABC using FalkorDB (a Redis-graph module):

- **Per-tenant graph keys** (``openzync_{org_id}_{project_id}``) guarantee
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

from core.exceptions import (
    ExternalServiceError,
    GraphBackendUnavailableError,
    NotFoundError,
)
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
    # Range index for episode stub lookup
    "CREATE RANGE INDEX FOR (n:Episode) ON (n.id);",
    # Range index for session stub lookup
    "CREATE RANGE INDEX FOR (n:Session) ON (n.id);",
    # Range index for observation upsert (MERGE on subject_entity_id + observation_type)
    "CREATE RANGE INDEX FOR (n:Observation) ON (n.subject_entity_id, n.observation_type);",
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

_O_ID = 0
_O_SUBJECT_ID = 1
_O_TYPE = 2
_O_CONTENT = 3
_O_CONFIDENCE = 4
_O_RELATED_ID = 5
_O_METADATA = 6
_O_CREATED = 7


# ── Backend Implementation ─────────────────────────────────────────────────


class FalkorGraphBackend(GraphBackend):
    """FalkorDB-native graph backend.

    Each org+project pair gets its own isolated FalkorDB graph key
    (``openzync_{org_id}_{project_id}``).  This guarantees tenant isolation
    at the database level — ``algo.bfs()`` and ``queryNodes()`` never
    traverse into another tenant's data.

    Args:
        client: An optional connected ``FalkorDB`` async instance.  When
            ``None``, all methods raise ``GraphBackendUnavailableError``.
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
        key = f"openzync_{org_id}_{project_id}"
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

    async def _ensure_episode_node(
        self,
        graph: Any,
        episode_id: UUID,
        org_id: UUID,
        project_id: UUID,
    ) -> None:
        """Idempotently create a lightweight ``:Episode`` stub node.

        FalkorDB has no built-in episode concept — episodes live in
        PostgreSQL.  This stub provides a Cypher-traversable node that
        links entities to episodes via ``(:Episode)-[:MENTIONS]->(:Entity)``.

        The stub carries ``id``, ``organization_id``, ``project_id``, and
        ``created_at`` so it can be used as a traversal hop for session-
        scoped and co-occurrence queries.
        """
        try:
            await graph.query(
                """
                MERGE (ep:Episode {id: $episode_id})
                ON CREATE SET
                    ep.organization_id = $org_id,
                    ep.project_id = $project_id,
                    ep.created_at = $now
                """,
                {
                    "episode_id": str(episode_id),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "now": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            logger.error(
                "falkordb_graph.ensure_episode_failed",
                extra={
                    "episode_id": str(episode_id),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to ensure episode node {episode_id}: {exc}",
                detail={
                    "org_id": str(org_id),
                    "episode_id": str(episode_id),
                },
            ) from exc

    async def _ensure_session_node(
        self,
        graph: Any,
        session_id: UUID,
        org_id: UUID,
        project_id: UUID,
    ) -> None:
        """Idempotently create a lightweight ``:Session`` stub node.

        Same rationale as ``_ensure_episode_node`` — sessions live in
        PostgreSQL, but a stub node is needed so that
        ``get_entities_for_session`` can traverse
        ``(:Session)-[:HAS_EPISODE]->(:Episode)-[:MENTIONS]->(:Entity)``.
        """
        try:
            await graph.query(
                """
                MERGE (s:Session {id: $session_id})
                ON CREATE SET
                    s.organization_id = $org_id,
                    s.project_id = $project_id,
                    s.created_at = $now
                """,
                {
                    "session_id": str(session_id),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "now": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            logger.error(
                "falkordb_graph.ensure_session_failed",
                extra={
                    "session_id": str(session_id),
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to ensure session node {session_id}: {exc}",
                detail={
                    "org_id": str(org_id),
                    "session_id": str(session_id),
                },
            ) from exc

    @staticmethod
    def _row_to_observation(row: Sequence[Any]) -> dict:
        """Convert a result tuple to an observation dict.

        Expected column order: ``id, subject_entity_id, observation_type,
        content, confidence, related_entity_id, observation_metadata,
        created_at``.
        """
        return {
            "id": str(row[_O_ID]) if row[_O_ID] else "",
            "subject_entity_id": str(row[_O_SUBJECT_ID]) if row[_O_SUBJECT_ID] else "",
            "observation_type": str(row[_O_TYPE]) if row[_O_TYPE] else "",
            "content": str(row[_O_CONTENT]) if row[_O_CONTENT] else "",
            "confidence": (
                float(row[_O_CONFIDENCE])
                if len(row) > _O_CONFIDENCE and row[_O_CONFIDENCE] is not None
                else 0.0
            ),
            "related_entity_id": (
                str(row[_O_RELATED_ID]) if len(row) > _O_RELATED_ID and row[_O_RELATED_ID] else None
            ),
            "observation_metadata": (
                FalkorGraphBackend._parse_json_field(row[_O_METADATA])
                if len(row) > _O_METADATA
                else {}
            ),
            "created_at": (
                row[_O_CREATED].isoformat()
                if len(row) > _O_CREATED
                and hasattr(row[_O_CREATED], "isoformat")
                else str(row[_O_CREATED]) if len(row) > _O_CREATED and row[_O_CREATED] else None
            ),
        }

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

        key = f"openzync_{org_id}_{project_id}"
        if not self._schema_ensured.get(key):
            await self._ensure_schema(graph)
            self._schema_ensured[key] = True

        name_lower = name.lower().strip()
        summary_val = summary if summary is not None else ""
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
        *,
        name: str | None = None,
        entity_type: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Update entity fields.  Only provided fields are changed.

        Builds a dynamic ``SET`` clause with only the non-``None`` fields.
        Raises ``NotFoundError`` if the entity does not exist.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            entity_id: UUID of the entity to update.
            name: New name, or ``None`` to leave unchanged.
            entity_type: New type label, or ``None`` to leave unchanged.
            summary: New summary text, or ``None`` to leave unchanged.

        Returns:
            The updated entity dict including ``id``, ``name``, ``entity_type``,
            ``summary``, and ``updated_at`` keys.

        Raises:
            NotFoundError: If no entity with the given ID exists.
            ExternalServiceError: If the query fails.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            raise ExternalServiceError(
                message="FalkorDB not connected",
                detail={"reason": "client is None"},
            )

        set_parts: list[str] = []
        params: dict[str, object] = {"id": str(entity_id)}

        if name is not None:
            params["name"] = name.lower().strip()
            set_parts.append("n.name = $name")
        if entity_type is not None:
            params["entity_type"] = entity_type
            set_parts.append("n.entity_type = $entity_type")
        if summary is not None:
            params["summary"] = summary
            set_parts.append("n.summary = $summary")

        if not set_parts:
            existing = await self.get_entity(org_id, project_id, entity_id)
            if existing is None:
                raise NotFoundError(
                    message=f"Entity {entity_id} not found for update",
                    detail={"org_id": str(org_id), "entity_id": str(entity_id)},
                )
            return existing

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
                raise NotFoundError(
                    message=f"Entity {entity_id} not found for update",
                    detail={"org_id": str(org_id), "entity_id": str(entity_id)},
                )
            return self._row_to_entity(result.result_set[0])
        except NotFoundError:
            raise
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
            "properties": orjson.dumps(properties if properties is not None else {}).decode("utf-8"),
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

    # ── Group C2: Aggregate Queries (for observation service) ────────────────────

    async def get_total_entity_linked_episode_count(
        self,
        org_id: UUID,
        project_id: UUID,
    ) -> int:
        """Get total distinct episodes that have at least one linked entity.

        Traverses ``(:Episode)-[:MENTIONS]->(:Entity)`` and counts distinct
        episodes.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.

        Returns:
            Number of distinct episodes with linked entities.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            raise GraphBackendUnavailableError(
                "FalkorDB not connected — cannot count entity-linked episodes."
            )

        try:
            result = await graph.query(
                """
                MATCH (ep:Episode {organization_id: $org_id, project_id: $project_id})
                  -[:MENTIONS]->(en:Entity)
                RETURN count(DISTINCT ep) AS total
                """,
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            if result.result_set and result.result_set[0]:
                return result.result_set[0][0]
            return 0
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_total_entity_linked_episode_count_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message="Failed to count entity-linked episodes",
                detail={"org_id": str(org_id), "project_id": str(project_id)},
            ) from exc

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

        graph = self._get_graph(org_id, project_id)
        if graph is None:
            raise GraphBackendUnavailableError(
                "FalkorDB not connected — cannot resolve entity names."
            )

        str_ids = [str(eid) for eid in entity_ids]

        try:
            result = await graph.query(
                """
                MATCH (e:Entity)
                WHERE e.id IN $entity_ids
                  AND e.organization_id = $org_id
                  AND e.project_id = $project_id
                RETURN e.id AS id, e.name AS name, e.entity_type AS entity_type
                """,
                {
                    "entity_ids": str_ids,
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            output: dict[str, dict] = {}
            for row in result.result_set:
                eid_str = str(row[0]) if row[0] else ""
                if eid_str:
                    output[eid_str] = {
                        "name": str(row[1]) if row[1] else "",
                        "entity_type": str(row[2]) if row[2] else "",
                    }
            return output
        except Exception as exc:
            logger.error(
                "falkordb_graph.resolve_entity_names_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id_count": len(str_ids),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message="Failed to resolve entity names",
                detail={"org_id": str(org_id), "project_id": str(project_id)},
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
                logger.error(
                    "falkordb_graph.traverse.neighbour_fetch_failed",
                    extra={
                        "entity_id": current_id,
                        "depth": depth,
                    },
                    exc_info=True,
                )
                raise GraphBackendUnavailableError(
                    f"FalkorDB graph traversal neighbour fetch failed for entity {current_id}."
                ) from exc

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
                    "entity_types": types if types is not None else [],
                    "entity_types_null": types is None,
                },
            )

            # Slice in Python since FalkorDB SKIP/LIMIT with params can be
            # unreliable in some versions.
            rows = result.result_set if result.result_set is not None else []
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
                except Exception as exc:
                    logger.error(
                        "falkordb_graph.retrieve_graph.traverse_failed",
                        extra={"entity_id": entity_id_str, "query": query},
                        exc_info=True,
                    )
                    raise GraphBackendUnavailableError(
                        f"FalkorDB graph traversal failed for entity {entity_id_str} during retrieve_graph."
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
                "falkordb_graph.retrieve_graph_failed",
                extra={"query": query},
                exc_info=True,
            )
            raise GraphBackendUnavailableError(
                f"FalkorDB retrieve_graph failed for query '{query}'."
            ) from exc

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

    # ── Group A: Entity–Episode Linking ─────────────────────────────────────

    async def link_entity_to_episode(
        self,
        org_id: UUID,
        project_id: UUID,
        episode_id: UUID,
        entity_id: UUID,
    ) -> None:
        """Record that an entity appears in an episode via a stub ``:Episode`` node.

        Idempotent — uses ``MERGE`` for both the episode stub and the
        ``(:Episode)-[:MENTIONS]->(:Entity)`` edge.

        Raises:
            NotFoundError: If the entity does not exist in the graph.
            ExternalServiceError: If the query fails.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            raise ExternalServiceError(
                message="FalkorDB not connected",
                detail={"reason": "client is None"},
            )

        # Verify entity exists first
        entity = await self.get_entity(org_id, project_id, entity_id)
        if entity is None:
            raise NotFoundError(
                message=f"Entity {entity_id} not found for episode linking",
                detail={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": str(entity_id),
                },
            )

        await self._ensure_episode_node(graph, episode_id, org_id, project_id)

        try:
            await graph.query(
                """
                MATCH (ep:Episode {id: $episode_id})
                MATCH (en:Entity {id: $entity_id})
                MERGE (ep)-[:MENTIONS]->(en)
                """,
                {
                    "episode_id": str(episode_id),
                    "entity_id": str(entity_id),
                },
            )
            logger.info(
                "falkordb_graph.entity_linked_to_episode",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "episode_id": str(episode_id),
                    "entity_id": str(entity_id),
                },
            )
        except NotFoundError:
            raise
        except Exception as exc:
            logger.error(
                "falkordb_graph.link_entity_to_episode_failed",
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
        """Return all distinct graph entities linked to episodes in a session.

        Traverses ``(:Session)-[:HAS_EPISODE]->(:Episode)-[:MENTIONS]->(:Entity)``.
        Requires ``:Session`` and ``:HAS_EPISODE`` edges to be created
        externally (e.g. when episodes are assigned to sessions in the
        calling service).

        Returns:
            List of entity dicts with ``id``, ``name``, ``entity_type``,
            ``summary`` keys.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        try:
            result = await graph.query(
                """
                MATCH (s:Session {id: $session_id})
                  -[:HAS_EPISODE]->(ep:Episode)
                  -[:MENTIONS]->(en:Entity)
                RETURN DISTINCT
                    en.id, en.name, en.entity_type, en.summary,
                    en.attributes, en.created_at
                """,
                {"session_id": str(session_id)},
            )
            return [self._row_to_entity(row) for row in result.result_set]
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_entities_for_session_failed",
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
        """Find entity pairs that co-appear in episodes above a threshold.

        Uses the ``(:Episode)-[:MENTIONS]->(:Entity)`` pattern: two entities
        co-occur when they share a common episode.  Results are sorted by
        co-occurrence count descending.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            min_co_count: Minimum number of shared episodes. Defaults to 2.

        Returns:
            List of dicts with ``entity_a_id``, ``entity_a_name``,
            ``entity_b_id``, ``entity_b_name``, ``co_count``.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        try:
            result = await graph.query(
                """
                MATCH (a:Entity)<-[:MENTIONS]-(ep:Episode)-[:MENTIONS]->(b:Entity)
                WHERE a.id < b.id
                  AND a.organization_id = $org_id
                  AND a.project_id = $project_id
                  AND b.organization_id = $org_id
                  AND b.project_id = $project_id
                WITH a, b, count(ep) AS co_count
                WHERE co_count >= $min_co_count
                RETURN a.id AS entity_a_id, a.name AS entity_a_name,
                       b.id AS entity_b_id, b.name AS entity_b_name,
                       co_count
                ORDER BY co_count DESC
                """,
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "min_co_count": min_co_count,
                },
            )
            pairs = []
            for row in result.result_set:
                pairs.append({
                    "entity_a_id": str(row[0]) if row[0] else "",
                    "entity_a_name": str(row[1]) if row[1] else "",
                    "entity_b_id": str(row[2]) if row[2] else "",
                    "entity_b_name": str(row[3]) if row[3] else "",
                    "co_count": int(row[4]) if len(row) > 4 and row[4] is not None else 0,
                })
            return pairs
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_co_occurring_entity_pairs_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "min_co_count": min_co_count,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message="Failed to get co-occurring entity pairs",
                detail={"org_id": str(org_id)},
            ) from exc

    # ── Group B: Bulk / Merge Operations ────────────────────────────────────

    async def get_all_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        include_merged: bool = False,
    ) -> list[dict[str, Any]]:
        """Return ALL entities for a project (no pagination).

        WARNING: Intended for batch workers (merge dedup, community
        detection).  Do NOT expose via API — no limit means it can return
        millions of rows.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            include_merged: If ``True``, include soft-deleted (merged)
                entities.  Defaults to ``False``.

        Returns:
            A complete list of entity dicts for the project.  Each dict
            includes ``id``, ``name``, ``entity_type``, ``summary``,
            ``is_merged``, and ``created_at``.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        try:
            result = await graph.query(
                """
                MATCH (n:Entity {organization_id: $org_id, project_id: $project_id})
                WHERE $include_merged = true
                   OR n.is_merged IS NULL
                   OR n.is_merged = false
                RETURN n.id, n.name, n.entity_type, n.summary,
                       n.attributes, n.created_at, n.is_merged
                ORDER BY n.name
                """,
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "include_merged": include_merged,
                },
            )
            entities = []
            for row in result.result_set:
                entity = self._row_to_entity(row[:6])
                entity["is_merged"] = (
                    bool(row[6]) if len(row) > 6 and row[6] is not None else False
                )
                entities.append(entity)
            return entities
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_all_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message="Failed to get all entities",
                detail={"org_id": str(org_id)},
            ) from exc

    async def get_all_relationships(
        self,
        org_id: UUID,
        project_id: UUID,
    ) -> list[dict[str, Any]]:
        """Return ALL active relationships for a project (no pagination).

        Same warning as :meth:`get_all_entities` — batch use only.
        Only non-expired (``invalid_at IS NULL``) relationships are returned.

        Returns:
            A complete list of relationship dicts.  Each dict includes
            ``id``, ``source_id``, ``target_id``, ``relationship_type``,
            ``confidence``, and ``created_at``.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        try:
            result = await graph.query(
                """
                MATCH (s:Entity {organization_id: $org_id, project_id: $project_id})
                  -[r]->(t:Entity)
                WHERE (s.is_merged IS NULL OR s.is_merged = false)
                  AND r.invalid_at IS NULL
                RETURN r.id, r.source_id, r.target_id, type(r) AS rel_type,
                       r.properties, r.fact, r.confidence,
                       r.valid_from, r.valid_to, r.created_at
                """,
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            rels = []
            for row in result.result_set:
                rel = self._row_to_relationship(row)
                # Map internal 'type' key to ABC-expected 'relationship_type'
                rel["relationship_type"] = rel.pop("type", "")
                rels.append(rel)
            return rels
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_all_relationships_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message="Failed to get all relationships",
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
        """Search entities using BM25 full-text search for dedup detection.

        FalkorDB uses RediSearch-backed fulltext index (BM25 scoring).
        The ``fuzzy_threshold`` is applied as a minimum score filter.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            query: Search string to match against entity names/summaries.
            fuzzy_threshold: Minimum BM25 score threshold (0.0–1.0).  Note
                that FalkorDB/RediSearch BM25 scores are not normalised to
                0–1 — this threshold acts as a semantic filter; lower values
                are more permissive.
            limit: Maximum results to return.  Defaults to 50.

        Returns:
            List of entity dicts with an added ``score`` key (float).
            Sorted by descending score.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        try:
            result = await graph.query(
                """
                CALL db.idx.fulltext.queryNodes('Entity', $query) YIELD node, score
                WHERE score >= $threshold
                  AND node.organization_id = $org_id
                  AND node.project_id = $project_id
                RETURN node.id, node.name, node.entity_type, node.summary,
                       node.attributes, node.created_at, score
                ORDER BY score DESC
                LIMIT $limit
                """,
                {
                    "query": query,
                    "threshold": fuzzy_threshold,
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "limit": limit,
                },
            )
            entities = []
            for row in result.result_set:
                entity = self._row_to_entity(row[:6])
                entity["score"] = (
                    float(row[6]) if len(row) > 6 and row[6] is not None else 0.0
                )
                entities.append(entity)
            return entities
        except Exception as exc:
            logger.error(
                "falkordb_graph.bulk_search_entities_failed",
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
        """Merge duplicate entities: rewire edges to canonical, soft-delete merged.

        STRICT ATOMICITY CONTRACT: FalkorDB executes each rewiring step as
        an atomic ``graph.query()``.  However, because dynamic edge types
        prevent a single all-in-one Cypher statement, this method submits
        one query per distinct edge type plus a final soft-delete query.
        If a step fails mid-way, partial state may be visible — callers
        should implement retry logic (the operation is idempotent).

        Steps:
        1. Verify that all entities (canonical + merged) exist.
        2. Collect distinct relationship types connected to merged entities.
        3. For each type, rewire incoming edges → canonical.
        4. For each type, rewire outgoing edges → canonical.
        5. Soft-delete merged entities (``is_merged = true``).

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            canonical_id: UUID of the surviving entity.
            merged_ids: UUIDs of entities being absorbed.

        Returns:
            Dict with ``rewired_count`` (int), ``deleted_count`` (int, always
            0 for FalkorDB — MERGE deduplicates), ``merged_count`` (int).

        Raises:
            NotFoundError: If canonical or any merged entity does not exist.
            ExternalServiceError: If a query fails.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            raise ExternalServiceError(
                message="FalkorDB not connected",
                detail={"reason": "client is None"},
            )

        canonical_str = str(canonical_id)
        merged_strs = [str(mid) for mid in merged_ids]
        all_ids = [canonical_str] + merged_strs

        # Step 1: Verify all entities exist
        try:
            check_result = await graph.query(
                """
                UNWIND $ids AS eid
                MATCH (n:Entity {id: eid})
                RETURN collect(n.id) AS found
                """,
                {"ids": all_ids},
            )
            found_ids: list[str] = (
                list(check_result.result_set[0][0])
                if check_result.result_set
                else []
            )
            missing = [eid for eid in all_ids if eid not in found_ids]
            if missing:
                raise NotFoundError(
                    message=f"Entities not found: {missing}",
                    detail={
                        "org_id": str(org_id),
                        "canonical_id": canonical_str,
                        "missing_ids": missing,
                    },
                )
        except NotFoundError:
            raise
        except Exception as exc:
            logger.error(
                "falkordb_graph.merge_entities.verification_failed",
                extra={
                    "org_id": str(org_id),
                    "canonical_id": canonical_str,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Entity verification failed during merge: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

        # Step 2: Collect distinct edge types connected to merged entities
        try:
            type_result = await graph.query(
                """
                MATCH (m:Entity) WHERE m.id IN $merged_ids
                MATCH (m)-[r]-(connected:Entity)
                WHERE connected.id <> $canonical_id
                  AND r.invalid_at IS NULL
                RETURN collect(DISTINCT type(r)) AS rel_types
                """,
                {
                    "merged_ids": merged_strs,
                    "canonical_id": canonical_str,
                },
            )
            distinct_types: list[str] = (
                list(type_result.result_set[0][0])
                if type_result.result_set and type_result.result_set[0][0]
                else []
            )
        except Exception as exc:
            logger.error(
                "falkordb_graph.merge_entities.type_collection_failed",
                extra={
                    "org_id": str(org_id),
                    "canonical_id": canonical_str,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to collect relationship types during merge: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

        now_str = datetime.now(timezone.utc).isoformat()
        rewired_count = 0

        # Step 3 & 4: Rewire each edge type (incoming + outgoing)
        for rel_type in distinct_types:
            safe_type = self._sanitize_edge_type(rel_type)

            # Incoming rewiring — edges pointing TO merged entities
            try:
                inc_result = await graph.query(
                    f"""
                    MATCH (m:Entity) WHERE m.id IN $merged_ids
                    MATCH (s:Entity)-[r:{safe_type}]->(m)
                    WHERE s.id <> $canonical_id
                      AND r.invalid_at IS NULL
                    MATCH (canon:Entity {{id: $canonical_id}})
                    MERGE (s)-[nr:{safe_type}]->(canon)
                    ON CREATE SET
                        nr.id = r.id,
                        nr.source_id = r.source_id,
                        nr.target_id = $canonical_id,
                        nr.properties = r.properties,
                        nr.fact = r.fact,
                        nr.confidence = r.confidence,
                        nr.valid_from = r.valid_from,
                        nr.valid_to = r.valid_to,
                        nr.created_at = r.created_at,
                        nr.updated_at = $now,
                        nr.organization_id = r.organization_id,
                        nr.project_id = r.project_id,
                        nr.invalid_at = NULL
                    WITH r
                    DELETE r
                    """,
                    {
                        "merged_ids": merged_strs,
                        "canonical_id": canonical_str,
                        "now": now_str,
                    },
                )
                rewired_count += len(inc_result.result_set) if inc_result.result_set else 0
            except Exception as exc:
                logger.error(
                    "falkordb_graph.merge_entities.incoming_rewire_failed",
                    extra={
                        "rel_type": rel_type,
                        "canonical_id": canonical_str,
                        "error": str(exc),
                    },
                )
                raise ExternalServiceError(
                    message=f"Failed to rewire incoming '{rel_type}' edges during merge: {exc}",
                    detail={"org_id": str(org_id), "rel_type": rel_type},
                ) from exc

            # Outgoing rewiring — edges FROM merged entities
            try:
                out_result = await graph.query(
                    f"""
                    MATCH (m:Entity) WHERE m.id IN $merged_ids
                    MATCH (m)-[r:{safe_type}]->(t:Entity)
                    WHERE t.id <> $canonical_id
                      AND r.invalid_at IS NULL
                    MATCH (canon:Entity {{id: $canonical_id}})
                    MERGE (canon)-[nr:{safe_type}]->(t)
                    ON CREATE SET
                        nr.id = r.id,
                        nr.source_id = $canonical_id,
                        nr.target_id = r.target_id,
                        nr.properties = r.properties,
                        nr.fact = r.fact,
                        nr.confidence = r.confidence,
                        nr.valid_from = r.valid_from,
                        nr.valid_to = r.valid_to,
                        nr.created_at = r.created_at,
                        nr.updated_at = $now,
                        nr.organization_id = r.organization_id,
                        nr.project_id = r.project_id,
                        nr.invalid_at = NULL
                    WITH r
                    DELETE r
                    """,
                    {
                        "merged_ids": merged_strs,
                        "canonical_id": canonical_str,
                        "now": now_str,
                    },
                )
                rewired_count += len(out_result.result_set) if out_result.result_set else 0
            except Exception as exc:
                logger.error(
                    "falkordb_graph.merge_entities.outgoing_rewire_failed",
                    extra={
                        "rel_type": rel_type,
                        "canonical_id": canonical_str,
                        "error": str(exc),
                    },
                )
                raise ExternalServiceError(
                    message=f"Failed to rewire outgoing '{rel_type}' edges during merge: {exc}",
                    detail={"org_id": str(org_id), "rel_type": rel_type},
                ) from exc

        # Step 5: Soft-delete merged entities
        try:
            soft_delete_result = await graph.query(
                """
                MATCH (m:Entity) WHERE m.id IN $merged_ids
                SET m.is_merged = true, m.merged_into = $canonical_id
                RETURN count(m) AS merged_count
                """,
                {
                    "merged_ids": merged_strs,
                    "canonical_id": canonical_str,
                },
            )
            merged_count = (
                int(soft_delete_result.result_set[0][0])
                if soft_delete_result.result_set
                else 0
            )
        except Exception as exc:
            logger.error(
                "falkordb_graph.merge_entities.soft_delete_failed",
                extra={
                    "org_id": str(org_id),
                    "canonical_id": canonical_str,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to soft-delete merged entities: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

        logger.info(
            "falkordb_graph.entities_merged",
            extra={
                "org_id": str(org_id),
                "project_id": str(project_id),
                "canonical_id": canonical_str,
                "merged_ids": merged_strs,
                "rewired_count": rewired_count,
                "merged_count": merged_count,
            },
        )

        return {
            "rewired_count": rewired_count,
            "deleted_count": 0,
            "merged_count": merged_count,
        }

    async def create_relationship_bulk(
        self,
        org_id: UUID,
        project_id: UUID,
        relationships: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Batch-create multiple relationships in a single transaction.

        Each dict in ``relationships`` must have ``source_id``, ``target_id``,
        and ``relationship_type``.  Optional keys: ``confidence``,
        ``properties``, ``valid_from``, ``valid_to``.

        Uses one ``graph.query()`` call per relationship (since each may
        have a different edge type).  The caller should ensure the list is
        not excessively large.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            relationships: List of relationship descriptor dicts.

        Returns:
            List of created relationship dicts in the same order as the input.

        Raises:
            ValueError: If any input dict is missing required keys.
            ExternalServiceError: If any query fails.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            raise ExternalServiceError(
                message="FalkorDB not connected",
                detail={"reason": "client is None"},
            )

        results: list[dict[str, Any]] = []
        for i, rel in enumerate(relationships):
            source_id = rel.get("source_id")
            target_id = rel.get("target_id")
            rel_type = rel.get("relationship_type")

            if not source_id or not target_id or not rel_type:
                raise ValueError(
                    f"Relationship at index {i} is missing required key(s): "
                    "'source_id', 'target_id', 'relationship_type'. "
                    f"Got keys: {list(rel.keys())}"
                )

            created = await self.create_relationship(
                org_id=org_id,
                project_id=project_id,
                source_id=UUID(source_id) if isinstance(source_id, str) else source_id,
                target_id=UUID(target_id) if isinstance(target_id, str) else target_id,
                relationship_type=rel_type,
                properties=rel.get("properties"),
                confidence=rel.get("confidence"),
                valid_from=rel.get("valid_from"),
                valid_to=rel.get("valid_to"),
            )
            results.append(created)

        return results

    # ── Group C: Observations ───────────────────────────────────────────────

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

        Upsert uniqueness is determined by the triple
        ``(subject_entity_id, observation_type, related_entity_id)``.
        When ``related_entity_id`` is ``None``, a sentinel value
        (``00000000-0000-0000-0000-000000000000``) is used as the
        discriminator.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            subject_entity_id: The entity this observation is about.
            observation_type: Semantic type label.
            content: Human-readable description.
            confidence: Confidence score 0.0–1.0.
            related_entity_id: Optional secondary entity involved.
            supporting_fact_ids: Optional list of supporting fact UUIDs.
            supporting_relationship_ids: Optional list of supporting
                relationship UUIDs.
            valid_from: Optional temporal validity start.
            valid_to: Optional temporal validity end.
            observation_metadata: Optional arbitrary key-value metadata.

        Returns:
            The created or updated observation dict.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            raise ExternalServiceError(
                message="FalkorDB not connected",
                detail={"reason": "client is None"},
            )

        # Use sentinel for None related_entity_id so MERGE works as unique key
        related_discriminator = (
            str(related_entity_id)
            if related_entity_id is not None
            else "00000000-0000-0000-0000-000000000000"
        )

        obs_id = str(uuid4())
        now_str = datetime.now(timezone.utc).isoformat()

        supporting_fact_strs: list[str] = (
            [str(fid) for fid in supporting_fact_ids]
            if supporting_fact_ids
            else []
        )
        supporting_rel_strs: list[str] = (
            [str(rid) for rid in supporting_relationship_ids]
            if supporting_relationship_ids
            else []
        )

        try:
            result = await graph.query(
                """
                MERGE (o:Observation {
                    subject_entity_id: $subject_id,
                    observation_type: $type,
                    related_entity_id: $related_id
                })
                ON CREATE SET
                    o.id = $obs_id,
                    o.organization_id = $org_id,
                    o.project_id = $project_id,
                    o.content = $content,
                    o.confidence = $confidence,
                    o.supporting_fact_ids = $supporting_fact_ids,
                    o.supporting_relationship_ids = $supporting_rel_ids,
                    o.observation_metadata = $metadata,
                    o.valid_from = $valid_from,
                    o.valid_to = $valid_to,
                    o.created_at = $now,
                    o.updated_at = $now
                ON MATCH SET
                    o.content = $content,
                    o.confidence = $confidence,
                    o.supporting_fact_ids = $supporting_fact_ids,
                    o.supporting_relationship_ids = $supporting_rel_ids,
                    o.observation_metadata = $metadata,
                    o.valid_from = CASE WHEN $valid_from IS NOT NULL
                        THEN $valid_from ELSE o.valid_from END,
                    o.valid_to = CASE WHEN $valid_to IS NOT NULL
                        THEN $valid_to ELSE o.valid_to END,
                    o.updated_at = $now
                RETURN o.id, o.subject_entity_id, o.observation_type,
                       o.content, o.confidence, o.related_entity_id,
                       o.observation_metadata, o.created_at
                """,
                {
                    "subject_id": str(subject_entity_id),
                    "type": observation_type,
                    "related_id": related_discriminator,
                    "obs_id": obs_id,
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "content": content,
                    "confidence": confidence,
                    "supporting_fact_ids": orjson.dumps(supporting_fact_strs).decode("utf-8"),
                    "supporting_rel_ids": orjson.dumps(supporting_rel_strs).decode("utf-8"),
                    "metadata": orjson.dumps(
                        observation_metadata if observation_metadata is not None else {}
                    ).decode("utf-8"),
                    "valid_from": valid_from.isoformat() if valid_from else None,
                    "valid_to": valid_to.isoformat() if valid_to else None,
                    "now": now_str,
                },
            )
            row = result.result_set[0]
            observation = self._row_to_observation(row)

            logger.info(
                "falkordb_graph.observation_upserted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "observation_id": observation["id"],
                    "subject_entity_id": str(subject_entity_id),
                    "observation_type": observation_type,
                },
            )
            return observation
        except Exception as exc:
            logger.error(
                "falkordb_graph.upsert_observation_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "subject_entity_id": str(subject_entity_id),
                    "observation_type": observation_type,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to upsert observation for entity {subject_entity_id}: {exc}",
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
        """List observations with optional filters and offset pagination.

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            subject_entity_id: Optional filter — only observations about
                this entity.
            observation_type: Optional filter — only observations of
                this type.
            limit: Maximum results per page (max 200).
            cursor: Base64-encoded offset cursor.

        Returns:
            A dict with ``items``, ``next_cursor`` (str or None), and
            ``has_more`` (bool).
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return {"items": [], "next_cursor": None, "has_more": False}

        limit = min(limit, 200)
        offset = _decode_offset_cursor(cursor)

        params: dict[str, object] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
        }
        subject_filter = "$org_id = $org_id"
        type_filter = "$org_id = $org_id"

        if subject_entity_id is not None:
            subject_filter = "o.subject_entity_id = $subject_id"
            params["subject_id"] = str(subject_entity_id)
        if observation_type is not None:
            type_filter = "o.observation_type = $type"
            params["type"] = observation_type

        try:
            result = await graph.query(
                f"""
                MATCH (o:Observation)
                WHERE o.organization_id = $org_id
                  AND o.project_id = $project_id
                  AND {subject_filter}
                  AND {type_filter}
                RETURN o.id, o.subject_entity_id, o.observation_type,
                       o.content, o.confidence, o.related_entity_id,
                       o.observation_metadata, o.created_at
                ORDER BY o.created_at DESC
                LIMIT {limit + 1}
                """,
                params,
            )
            rows = result.result_set
            has_more = len(rows) > limit
            items = [self._row_to_observation(r) for r in rows[:limit]]

            next_cursor = None
            if has_more and items:
                next_cursor = _encode_offset_cursor(offset + len(items))

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_observations_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "subject_entity_id": (
                        str(subject_entity_id) if subject_entity_id else None
                    ),
                    "observation_type": observation_type,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message="Failed to get observations",
                detail={"org_id": str(org_id)},
            ) from exc

    async def get_entity_appearance_timestamps(
        self,
        org_id: UUID,
        project_id: UUID,
        entity_id: UUID,
    ) -> list[datetime]:
        """Get all timestamps when an entity appeared in episodes.

        Queries the graph via ``(:Episode)-[:MENTIONS]->(:Entity)``.
        Episode timestamps reflect when the stub node was first created
        (i.e. when the entity was first linked to that episode).

        Returns:
            Sorted list of episode timestamps (oldest first).
            Empty list if the entity has no linked episodes.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        try:
            result = await graph.query(
                """
                MATCH (ep:Episode)-[:MENTIONS]->(en:Entity {id: $entity_id})
                WHERE en.project_id = $project_id
                RETURN ep.created_at
                ORDER BY ep.created_at ASC
                """,
                {
                    "entity_id": str(entity_id),
                    "project_id": str(project_id),
                },
            )
            timestamps: list[datetime] = []
            for row in result.result_set:
                ts = row[0]
                if ts is not None:
                    if isinstance(ts, datetime):
                        timestamps.append(ts)
                    elif isinstance(ts, str):
                        try:
                            timestamps.append(datetime.fromisoformat(ts))
                        except (ValueError, TypeError):
                            timestamps.append(datetime.now(timezone.utc))
                    else:
                        timestamps.append(datetime.now(timezone.utc))
            return timestamps
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_entity_appearance_timestamps_failed",
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
        """Get IDs of direct relationships between two entities.

        Returns IDs for non-expired relationships in both directions.

        Returns:
            List of relationship UUIDs connecting the two entities.
            Empty list if no direct relationship exists.
        """
        graph = self._get_graph(org_id, project_id)
        if graph is None:
            return []

        try:
            result = await graph.query(
                """
                MATCH (a:Entity {id: $entity_a_id})-[r]-(b:Entity {id: $entity_b_id})
                WHERE r.invalid_at IS NULL
                RETURN r.id
                """,
                {
                    "entity_a_id": str(entity_a_id),
                    "entity_b_id": str(entity_b_id),
                },
            )
            rel_ids: list[UUID] = []
            for row in result.result_set:
                rid = row[0]
                if rid is not None:
                    try:
                        rel_ids.append(UUID(str(rid)))
                    except (ValueError, TypeError):
                        logger.warning(
                            "falkordb_graph.invalid_relationship_id",
                            extra={"relationship_id": rid},
                        )
            return rel_ids
        except Exception as exc:
            logger.error(
                "falkordb_graph.get_relationship_ids_between_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_a_id": str(entity_a_id),
                    "entity_b_id": str(entity_b_id),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get relationship IDs between {entity_a_id} and {entity_b_id}: {exc}",
                detail={
                    "org_id": str(org_id),
                    "entity_a_id": str(entity_a_id),
                    "entity_b_id": str(entity_b_id),
                },
            ) from exc
