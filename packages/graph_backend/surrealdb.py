"""SurrealDB-native graph backend — native graph relations, BM25 full-text search.

Implements the ``GraphBackend`` ABC using SurrealDB:
- ``RELATE`` / arrow syntax (``->``, ``<-``, ``->?``, ``<->?``) for O(1) traversal
- Per-type edge tables via ``RELATE entity:$src -> {type} -> entity:$tgt``
- BM25 full-text search via ``DEFINE ANALYZER`` + ``@@`` operator
- ``LET + IF/THEN/ELSE`` pattern for atomic upserts

Edge tables are created **lazily** on the first ``RELATE`` for a given type.

Usage::

    from surrealdb import AsyncSurreal
    from packages.graph_backend import SurrealGraphBackend

    surreal = AsyncSurreal("ws://localhost:8000/rpc")
    await surreal.connect()
    await surreal.signin({"username": "root", "password": "root"})
    await surreal.use("openzep", "openzep")

    backend = SurrealGraphBackend(surreal=surreal)
    entity = await backend.create_entity(UUID(...), UUID(...), name="Acme", entity_type="company")
"""

from __future__ import annotations

import base64
import re
from collections import deque
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

import orjson
import structlog
from surrealdb import AsyncSurreal, RecordID

from core.exceptions import ExternalServiceError
from packages.graph_backend.interface import GraphBackend

logger = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_SAFE_EDGE_TYPE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
"""Regex that accepts only safe SurrealDB table-name characters."""

MAX_TRAVERSAL_DEPTH: int = 5
"""Hard cap on BFS depth to prevent unbounded queries."""

_DEFINE_SURQL: str = """
-- 1. Analyzer for entity full-text search (BM25 via @@ operator)
DEFINE ANALYZER openzep_entity
    TOKENIZERS blank,class
    FILTERS lowercase,ascii,snowball;

-- 2. Entity table
DEFINE TABLE entity SCHEMAFULL;
DEFINE FIELD organization_id ON entity TYPE string;
DEFINE FIELD project_id ON entity TYPE string;
DEFINE FIELD name ON entity TYPE string;
DEFINE FIELD entity_type ON entity TYPE string;
DEFINE FIELD summary ON entity TYPE string;
DEFINE FIELD attributes ON entity TYPE object;
DEFINE FIELD created_at ON entity TYPE datetime;
DEFINE FIELD updated_at ON entity TYPE datetime;

-- 3. Full-text BM25 indexes (required for @@ operator)
DEFINE INDEX entity_name_fts ON entity FIELDS name
    FULLTEXT ANALYZER openzep_entity BM25;
DEFINE INDEX entity_summary_fts ON entity FIELDS summary
    FULLTEXT ANALYZER openzep_entity BM25;

-- 4. Unique index for entity upsert by (org_id, project_id, name)
DEFINE INDEX entity_org_project_name ON entity
    FIELDS organization_id, project_id, name UNIQUE;
"""

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
        logger.warning("surreal_graph.invalid_cursor", extra={"cursor": cursor})
        return 0


def _encode_offset_cursor(offset: int) -> str:
    """Encode an integer offset as a base64 cursor string."""
    payload = orjson.dumps({"o": offset})
    return base64.b64encode(payload).decode("ascii")


# ── Backend Implementation ─────────────────────────────────────────────────


class SurrealGraphBackend(GraphBackend):
    """SurrealDB-native graph backend.

    Uses native SurrealDB graph relations (``RELATE`` / arrow syntax) for
    O(1) traversal and ``@@`` with BM25 for full-text search.

    Args:
        surreal: An optional connected ``AsyncSurreal`` instance.  When
            ``None``, all methods raise ``ExternalServiceError`` with
            message ``"SurrealDB not connected"``.
        max_traversal_depth: Maximum BFS depth (default 2, max 5).
    """

    def __init__(
        self,
        surreal: AsyncSurreal | None = None,
        max_traversal_depth: int = 2,
    ) -> None:
        self._surreal = surreal
        self._max_depth = min(max_traversal_depth, MAX_TRAVERSAL_DEPTH)
        self._schema_ensured = False

    # ── Schema Bootstrap ─────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_edge_type(name: str) -> str:
        """Validate and return a SurrealDB-safe edge type name.

        Edge type names become SurrealDB table names via ``RELATE``.
        Only alphanumeric characters and underscores are allowed.

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

    async def _ensure_schema(self) -> None:
        """Idempotently create the SurrealDB schema on first use.

        Runs ``DEFINE ANALYZER``, ``DEFINE TABLE``, and ``DEFINE INDEX``
        statements once per backend instance.  Guarded by ``_schema_ensured``.
        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._schema_ensured or self._surreal is None:
            return
        try:
            await self._surreal.query(_DEFINE_SURQL)
            self._schema_ensured = True
            logger.info("surreal_graph.schema_ensured")
        except Exception as exc:
            logger.error(
                "surreal_graph.schema_bootstrap_failed",
                extra={"error": str(exc)},
            )
            raise ExternalServiceError(
                message=f"SurrealDB schema bootstrap failed: {exc}",
                detail={"error": str(exc)},
            ) from exc

    # ── Internal Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _record_id_to_str(rid: Any) -> str:
        """Safely extract the string ID from a SurrealDB ``RecordID``.

        Handles raw ``RecordID`` objects, plain strings, and ``None``.
        """
        if rid is None:
            return ""
        if hasattr(rid, "table_name") and hasattr(rid, "id"):
            return str(rid.id)
        return str(rid)

    @staticmethod
    def _to_iso(dt_val: Any) -> str | None:
        """Convert a datetime value to ISO-8601 string, or return ``None``."""
        if dt_val is None:
            return None
        if hasattr(dt_val, "isoformat"):
            return dt_val.isoformat()
        return str(dt_val)

    @classmethod
    def _row_to_entity(cls, row: dict) -> dict:
        """Convert a SurrealDB entity record to the standard entity dict.

        The returned dict uses the ``GraphBackend`` interface keys:
        ``id``, ``name``, ``type``, ``summary``, ``attributes``, ``created_at``.
        """
        return {
            "id": cls._record_id_to_str(row.get("id")),
            "name": row.get("name", ""),
            "type": row.get("entity_type", ""),
            "summary": row.get("summary") or "",
            "attributes": (
                dict(row["attributes"])
                if isinstance(row.get("attributes"), dict)
                else {}
            ),
            "created_at": cls._to_iso(row.get("created_at")),
        }

    @classmethod
    def _row_to_relationship(cls, row: dict) -> dict:
        """Convert a SurrealDB edge record to the standard relationship dict.

        The returned dict uses the ``GraphBackend`` interface keys:
        ``id``, ``source_id``, ``target_id``, ``type``, ``properties``,
        ``fact``, ``confidence``, ``valid_from``, ``valid_to``, ``created_at``.

        The ``type`` field is resolved in priority order:
        1. Explicit ``relationship_type`` field (set by queries that JOIN).
        2. ``edge_table_name`` (set via ``meta::tb(id)`` in list queries).
        3. The edge ``RecordID``'s ``table_name`` attribute (available on
           raw ``RELATE`` returns).
        """
        rid = row.get("id")
        edge_type = (
            row.get("relationship_type")
            or row.get("edge_table_name")
            or (rid.table_name if hasattr(rid, "table_name") else "")
        )
        return {
            "id": cls._record_id_to_str(rid),
            "source_id": cls._record_id_to_str(row.get("in")),
            "target_id": cls._record_id_to_str(row.get("out")),
            "type": edge_type,
            "properties": row.get("properties") or {},
            "fact": row.get("fact") or "",
            "confidence": float(row["confidence"]) if row.get("confidence") is not None else 1.0,
            "valid_from": cls._to_iso(row.get("valid_from")),
            "valid_to": cls._to_iso(row.get("valid_to")),
            "created_at": cls._to_iso(row.get("created_at")),
        }

    @classmethod
    def _rows_to_entities(cls, rows: list[dict]) -> list[dict]:
        """Convert a list of SurrealDB entity records to entity dicts."""
        return [cls._row_to_entity(r) for r in rows]

    # ── Guard helper ─────────────────────────────────────────────────────────

    def _require_connection(self) -> None:
        """Raise if SurrealDB is not connected."""
        if self._surreal is None:
            raise ExternalServiceError(
                message="SurrealDB not connected",
                detail={"reason": "surreal is None"},
            )

    # ── Entity CRUD ─────────────────────────────────────────────────────────

    async def create_entity(
        self,
        org_id: UUID,
        project_id: UUID,
        name: str,
        entity_type: str,
        summary: str | None = None,
    ) -> dict:
        """Create or update an entity node in the graph.

        Uses the ``LET + IF/THEN/ELSE`` pattern for an atomic upsert:
        - Looks up an existing entity by ``(organization_id, project_id, name)``
        - If found: updates ``entity_type`` (with type-upgrade guard) and
          ``summary`` (only if a non-empty value is provided).
        - If not found: creates a new entity record.

        The ``entity_type`` guard prevents downgrading a specific type to
        ``"Custom"`` — if the existing type is ``"Custom"`` and the new type
        is specific, the type is upgraded.  A specific type is never replaced
        with another specific type or downgraded to ``"Custom"``.

        Returns:
            The created or updated entity dict.

        Raises:
            ExternalServiceError: If the SurrealQL upsert fails.
        """
        await self._ensure_schema()
        self._require_connection()

        name_lower = name.lower().strip()
        summary_val = summary or ""

        query = """
        LET $existing = (SELECT id, entity_type, summary FROM entity
            WHERE organization_id = $org_id
              AND project_id = $project_id
              AND name = $name
            LIMIT 1);
        RETURN IF array::len($existing) > 0 THEN
            (UPDATE entity SET
                entity_type = IF $existing[0].entity_type = 'Custom'
                    AND $type != 'Custom'
                    THEN $type
                    ELSE $existing[0].entity_type
                END,
                summary = IF $summary != '' THEN $summary ELSE $existing[0].summary END,
                updated_at = time::now()
            WHERE id = $existing[0].id
            RETURN AFTER)
        ELSE
            (CREATE entity SET
                organization_id = $org_id,
                project_id = $project_id,
                name = $name,
                entity_type = $type,
                summary = $summary,
                attributes = {},
                created_at = time::now(),
                updated_at = time::now()
            RETURN AFTER)
        END;
        """

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "name": name_lower,
            "type": entity_type,
            "summary": summary_val,
        }

        try:
            result = await self._surreal.query(query, params)
            # result = [[], [record_dict]] — last statement is the RETURN
            record = result[-1][0]
            entity = self._row_to_entity(record)

            logger.info(
                "surreal_graph.entity_upserted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "entity_id": entity["id"],
                    "entity_type": entity_type,
                    "name": name_lower,
                },
            )
            return entity
        except ExternalServiceError:
            raise
        except Exception as exc:
            logger.error(
                "surreal_graph.create_entity_failed",
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
        """Retrieve an entity node by its ID, scoped to org and project.

        Returns:
            The entity dict, or ``None`` if not found.
        """
        await self._ensure_schema()
        self._require_connection()

        params: dict[str, Any] = {
            "id": RecordID("entity", str(entity_id)),
            "org_id": str(org_id),
            "project_id": str(project_id),
        }

        try:
            result = await self._surreal.query(
                """
                SELECT * FROM entity
                WHERE id = $id
                  AND organization_id = $org_id
                  AND project_id = $project_id
                LIMIT 1;
                """,
                params,
            )
            rows = result[0]
            if not rows:
                return None
            return self._row_to_entity(rows[0])
        except Exception as exc:
            logger.error(
                "surreal_graph.get_entity_failed",
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

        Deletes the entity record and cascades to all incident edges
        (via SurrealDB's ``DELETE`` with ``RETURNING``).

        Returns:
            ``True`` if the entity was deleted, ``False`` if it did not exist.
        """
        await self._ensure_schema()
        self._require_connection()

        params: dict[str, Any] = {
            "entity_id": RecordID("entity", str(entity_id)),
            "org_id": str(org_id),
            "project_id": str(project_id),
        }

        try:
            result = await self._surreal.query(
                """
                DELETE entity
                WHERE id = $entity_id
                  AND organization_id = $org_id
                  AND project_id = $project_id
                RETURNING id;
                """,
                params,
            )
            deleted = len(result[0]) > 0
            if deleted:
                logger.info(
                    "surreal_graph.entity_deleted",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "entity_id": str(entity_id),
                    },
                )
            return deleted
        except Exception as exc:
            logger.error(
                "surreal_graph.delete_entity_failed",
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
        """Update entity fields. Only provided fields are changed.

        Builds a dynamic ``UPDATE ... SET ...`` with only the non-None
        fields.  Returns the updated entity dict, or ``None`` if the entity
        does not exist.
        """
        await self._ensure_schema()
        self._require_connection()

        # Build dynamic SET clause
        set_parts: list[str] = []
        params: dict[str, Any] = {
            "entity_id": RecordID("entity", str(entity_id)),
            "org_id": str(org_id),
            "project_id": str(project_id),
        }

        if name is not None:
            params["name"] = name.lower().strip()
            set_parts.append("name = $name")
        if summary is not None:
            params["summary"] = summary
            set_parts.append("summary = $summary")
        if entity_type is not None:
            params["entity_type"] = entity_type
            set_parts.append("entity_type = $entity_type")
        if attributes is not None:
            params["attributes"] = attributes
            set_parts.append("attributes = $attributes")

        if not set_parts:
            # Nothing to update — return current state
            return await self.get_entity(org_id, project_id, entity_id)

        set_clause = ", ".join(set_parts)

        try:
            result = await self._surreal.query(
                f"""
                UPDATE entity SET
                    {set_clause},
                    updated_at = time::now()
                WHERE id = $entity_id
                  AND organization_id = $org_id
                  AND project_id = $project_id
                RETURN AFTER;
                """,
                params,
            )
            rows = result[0]
            if not rows:
                return None
            return self._row_to_entity(rows[0])
        except Exception as exc:
            logger.error(
                "surreal_graph.update_entity_failed",
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

    # ── Relationship CRUD ───────────────────────────────────────────────────

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

        Uses the ``LET + IF/THEN/ELSE`` pattern for an atomic upsert on the
        edge table named after ``relationship_type`` (validated via
        :meth:`_sanitize_edge_type`).  If the edge already exists (same
        ``in`` and ``out``), its properties, confidence (max), and temporal
        fields are updated.  Otherwise a new edge is ``RELATE``\\ d.

        Returns:
            The created or updated relationship dict.

        Raises:
            ValueError: If ``relationship_type`` contains unsafe characters.
            ExternalServiceError: If the SurrealQL upsert fails.
        """
        await self._ensure_schema()
        self._require_connection()

        safe_type = self._sanitize_edge_type(relationship_type)
        source_rid = RecordID("entity", str(source_id))
        target_rid = RecordID("entity", str(target_id))

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "source_id": source_rid,
            "target_id": target_rid,
            "properties": properties or {},
            "confidence": confidence if confidence is not None else 1.0,
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_to": valid_to.isoformat() if valid_to else None,
        }

        query = f"""
        LET $existing = (SELECT * FROM {safe_type}
            WHERE in = $source_id AND out = $target_id
            LIMIT 1);
        RETURN IF array::len($existing) > 0 THEN
            (UPDATE $existing[0].id SET
                properties = $properties,
                confidence = math::max(confidence, $confidence),
                valid_from = IF $valid_from IS NOT NONE
                    THEN $valid_from ELSE $existing[0].valid_from END,
                valid_to = IF $valid_to IS NOT NONE
                    THEN $valid_to ELSE $existing[0].valid_to END,
                updated_at = time::now()
            RETURN AFTER)
        ELSE
            (RELATE $source_id -> {safe_type} -> $target_id
            CONTENT {{
                organization_id: $org_id,
                project_id: $project_id,
                properties: $properties,
                fact: "",
                confidence: $confidence,
                valid_from: $valid_from,
                valid_to: $valid_to,
                created_at: time::now(),
                updated_at: time::now()
            }})
        END;
        """

        try:
            result = await self._surreal.query(query, params)
            # result = [[], [edge_record_dict]]
            record = result[-1][0]
            relationship = self._row_to_relationship(record)

            logger.info(
                "surreal_graph.relationship_upserted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "source_id": str(source_id),
                    "target_id": str(target_id),
                    "type": relationship_type,
                },
            )
            return relationship
        except ExternalServiceError:
            raise
        except ValueError:
            raise
        except Exception as exc:
            logger.error(
                "surreal_graph.create_relationship_failed",
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

    # ── Traversal ───────────────────────────────────────────────────────────

    async def traverse(
        self,
        org_id: UUID,
        project_id: UUID,
        start_node_id: UUID,
        max_depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> list[dict]:
        """Traverse the graph outward from a starting node.

        Uses an iterative BFS (Python) rather than a recursive CTE to avoid
        deep recursion issues.  At each hop, native SurrealDB arrow syntax
        (``->{type}->entity``) is used for O(1) neighbour discovery.

        Args:
            edge_types: ``None`` = all edge types; empty list = no edges
                (returns just the start node); specific list = filter by type.

        Returns:
            List of node dicts with a ``depth`` key (0 = start node).
        """
        await self._ensure_schema()
        self._require_connection()

        # Distinguish None (all types) from [] (no types)
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

            entity = await self.get_entity(org_id, project_id, UUID(current_id))
            if entity:
                entity["depth"] = depth
                nodes.append(entity)

            if depth >= max_depth:
                continue

            # Discover neighbours via native SurrealQL arrow syntax
            try:
                if edge_types is not None:
                    neighbour_ids: set[str] = set()
                    for et in edge_types:
                        safe_et = self._sanitize_edge_type(et)
                        et_result = await self._surreal.query(
                            f"SELECT VALUE ->{safe_et}->entity.id FROM $current_id;",
                            {"current_id": RecordID("entity", current_id)},
                        )
                        for row in (et_result[0] or []):
                            neighbour_ids.add(self._record_id_to_str(row))
                else:
                    et_result = await self._surreal.query(
                        "SELECT VALUE ->?->entity.id FROM $current_id;",
                        {"current_id": RecordID("entity", current_id)},
                    )
                    neighbour_ids = set()
                    for row in (et_result[0] or []):
                        neighbour_ids.add(self._record_id_to_str(row))

                for nid in neighbour_ids:
                    if nid and nid not in visited:
                        queue.append((nid, depth + 1))
            except Exception as exc:
                logger.warning(
                    "surreal_graph.traverse.neighbour_fetch_failed",
                    extra={
                        "entity_id": current_id,
                        "depth": depth,
                        "error": str(exc),
                    },
                )

        return nodes

    # ── Search ──────────────────────────────────────────────────────────────

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

        Uses the ``@@`` operator with the ``openzep_entity`` analyzer and
        ``search::score(0)`` for BM25 ranking.  Results are ordered by
        relevance descending.

        Returns:
            A list of matching entity dicts with a ``score`` key.
        """
        await self._ensure_schema()
        self._require_connection()

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "query": query,
            "entity_types": types or [],
            "entity_types_null": types is None,
            "limit": limit,
            "offset": offset,
        }

        try:
            result = await self._surreal.query(
                """
                SELECT *, search::score(0) AS score
                FROM entity
                WHERE organization_id = $org_id
                  AND project_id = $project_id
                  AND (name @@ $query OR summary @@ $query)
                  AND ($entity_types_null OR entity_type IN $entity_types)
                ORDER BY score DESC
                LIMIT $limit OFFSET $offset;
                """,
                params,
            )
            rows = result[0] or []
            entities = []
            for row in rows:
                entity = self._row_to_entity(row)
                entity["score"] = float(row.get("score") or 0.0)
                entities.append(entity)
            return entities
        except Exception as exc:
            logger.error(
                "surreal_graph.search_entities_failed",
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

    # ── Entity Listing ──────────────────────────────────────────────────────

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

        Uses ``LIMIT + 1`` to detect whether more results exist, and returns
        an opaque base64-encoded cursor for the next page.

        Returns:
            A dict with ``items``, ``next_cursor`` (str or None), and
            ``has_more`` (bool).
        """
        await self._ensure_schema()
        self._require_connection()

        limit = min(limit, 200)
        offset = _decode_offset_cursor(cursor)

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "limit": limit + 1,
            "offset": offset,
        }

        where_clause = (
            "organization_id = $org_id AND project_id = $project_id"
        )

        if entity_type:
            where_clause += " AND entity_type = $entity_type"
            params["entity_type"] = entity_type

        try:
            result = await self._surreal.query(
                f"""
                SELECT * FROM entity
                WHERE {where_clause}
                ORDER BY created_at ASC, id ASC
                LIMIT $limit START $offset;
                """,
                params,
            )
            rows = result[0] or []
            has_more = len(rows) > limit
            items = self._rows_to_entities(rows[:limit])

            next_cursor = None
            if has_more and items:
                next_cursor = _encode_offset_cursor(offset + len(items))

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except Exception as exc:
            logger.error(
                "surreal_graph.list_entities_failed",
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
        """List all edges incident to a specific entity node.

        With ``predicate``: queries a specific edge table by name.
        Without ``predicate``: uses the ``<->?`` wildcard to discover all
        incident edges (bidirectional).  ``meta::tb(id)`` extracts the
        edge table name for the ``type`` field.

        Returns:
            A dict with ``items`` (list of edge dicts), ``next_cursor``,
            and ``has_more``.
        """
        await self._ensure_schema()
        self._require_connection()

        limit = min(limit, 200)
        offset = _decode_offset_cursor(cursor)

        params: dict[str, Any] = {
            "eid": RecordID("entity", str(entity_id)),
            "limit": limit + 1,
            "offset": offset,
        }

        try:
            if predicate:
                safe_pred = self._sanitize_edge_type(predicate)
                result = await self._surreal.query(
                    f"""
                    SELECT *, meta::tb(id) AS edge_table_name
                    FROM (SELECT VALUE ->{safe_pred} FROM $eid)
                    ORDER BY created_at DESC
                    LIMIT $limit START $offset;
                    """,
                    params,
                )
            else:
                result = await self._surreal.query(
                    """
                    SELECT *, meta::tb(id) AS edge_table_name
                    FROM (SELECT VALUE <->? FROM $eid)
                    ORDER BY created_at DESC
                    LIMIT $limit START $offset;
                    """,
                    params,
                )

            rows = result[0] or []
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
                "surreal_graph.list_entity_edges_failed",
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

        Combines entity BM25 text search with iterative BFS traversal.
        Steps:
          1. Search entities whose name or summary matches the query.
          2. For each matched entity, BFS-traverse to depth ``max_depth``.
          3. Deduplicate by entity id, shape results with distance key.
          4. Sort by distance ascending and limit.

        Returns:
            Entity dicts with ``id``, ``name``, ``type``, ``summary``, and
            ``distance`` keys.  Distance 0 = directly matched, 1+ = reached
            via traversal.
        """
        await self._ensure_schema()
        self._require_connection()

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
                        "surreal_graph.retrieve_graph.traverse_failed",
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
                "surreal_graph.retrieve_graph_failed",
                extra={"query": query},
                exc_info=True,
            )
            return []

    # ── Health ──────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Verify SurrealDB is reachable and responsive."""
        if self._surreal is None:
            return False
        try:
            await self._surreal.query("SELECT 1;")
            return True
        except Exception:
            return False
