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
    await surreal.use("openzync", "openzync")

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
from surrealdb.errors import InternalError, SurrealError, parse_query_error

from core.exceptions import ExternalServiceError, GraphBackendUnavailableError, NotFoundError
from packages.graph_backend.interface import GraphBackend

logger = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_SAFE_EDGE_TYPE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
"""Regex that accepts only safe SurrealDB table-name characters."""

MAX_TRAVERSAL_DEPTH: int = 5
"""Hard cap on BFS depth to prevent unbounded queries."""

_DEFINE_SURQL: str = """
-- 1. Analyzer for entity full-text search (BM25 via @@ operator)
DEFINE ANALYZER openzync_entity
    TOKENIZERS blank, class
    FILTERS lowercase
    FILTERS ascii
    FILTERS snowball(english);

-- 2. Entity table
DEFINE TABLE entity SCHEMAFULL;
DEFINE FIELD organization_id ON entity TYPE string;
DEFINE FIELD project_id ON entity TYPE string;
DEFINE FIELD name ON entity TYPE string;
DEFINE FIELD entity_type ON entity TYPE string;
DEFINE FIELD summary ON entity TYPE string;
DEFINE FIELD attributes ON entity TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD is_merged ON entity TYPE bool DEFAULT false;
DEFINE FIELD invalid_at ON entity TYPE datetime;
DEFINE FIELD created_at ON entity TYPE datetime DEFAULT time::now();
DEFINE FIELD updated_at ON entity TYPE datetime VALUE time::now();

-- 3. Full-text BM25 indexes (required for @@ operator)
DEFINE INDEX entity_name_fts ON entity FIELDS name
    FULLTEXT ANALYZER openzync_entity BM25;
DEFINE INDEX entity_summary_fts ON entity FIELDS summary
    FULLTEXT ANALYZER openzync_entity BM25;

-- 4. Unique index for entity upsert by (org_id, project_id, name)
DEFINE INDEX entity_org_project_name ON entity
    FIELDS organization_id, project_id, name UNIQUE;

-- 5. Episode table (lightweight mirror for graph-edge traversal)
--    The caller is responsible for ensuring episode records exist
--    with `session_id` populated before `get_entities_for_session` is used.
DEFINE TABLE episode SCHEMAFULL;
DEFINE FIELD organization_id ON episode TYPE string;
DEFINE FIELD project_id ON episode TYPE string;
DEFINE FIELD session_id ON episode TYPE string;
DEFINE FIELD created_at ON episode TYPE datetime DEFAULT time::now();

-- 6. has_entity edge table (episode -> entity mapping)
--    Records that an entity was extracted from a specific episode.
DEFINE TABLE has_entity SCHEMAFULL;
DEFINE FIELD organization_id ON has_entity TYPE string;
DEFINE FIELD project_id ON has_entity TYPE string;

-- 7. Observation table (second-pass inferences)
DEFINE TABLE observation SCHEMAFULL;
DEFINE FIELD organization_id ON observation TYPE string;
DEFINE FIELD project_id ON observation TYPE string;
DEFINE FIELD subject_entity_id ON observation TYPE string;
DEFINE FIELD observation_type ON observation TYPE string;
DEFINE FIELD content ON observation TYPE string;
DEFINE FIELD confidence ON observation TYPE float;
DEFINE FIELD related_entity_id ON observation TYPE string;
DEFINE FIELD supporting_fact_ids ON observation TYPE array;
DEFINE FIELD supporting_relationship_ids ON observation TYPE array;
DEFINE FIELD valid_from ON observation TYPE datetime;
DEFINE FIELD valid_to ON observation TYPE datetime;
DEFINE FIELD observation_metadata ON observation TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD created_at ON observation TYPE datetime DEFAULT time::now();
DEFINE FIELD updated_at ON observation TYPE datetime VALUE time::now();
DEFINE INDEX idx_observation_dedup ON observation
    FIELDS organization_id, project_id, subject_entity_id, observation_type, related_entity_id UNIQUE;
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

        SurrealDB 3.x removed ``IF NOT EXISTS`` from ``DEFINE`` statements,
        so duplicate ``DEFINE`` calls return ``InternalError`` with "already
        exists".  This method catches that specific error and treats it as a
        no-op, making schema bootstrap truly idempotent regardless of
        SurrealDB version or backend instance lifecycle.
        """
        if self._schema_ensured or self._surreal is None:
            return
        try:
            await self._surreal.query(_DEFINE_SURQL)
        except InternalError as exc:
            # SurrealDB 3.x rejects duplicate DEFINE statements with
            # "already exists" — this is harmless, treat as idempotent.
            if "already exists" in str(exc).lower():
                logger.debug(
                    "surreal_graph.schema_already_exists",
                    extra={"error": str(exc)},
                )
                self._schema_ensured = True
                return
            raise  # some other InternalError — let the outer handler deal with it
        except Exception as exc:
            logger.error(
                "surreal_graph.schema_bootstrap_failed",
                extra={"error": str(exc)},
            )
            raise ExternalServiceError(
                message=f"SurrealDB schema bootstrap failed: {exc}",
                detail={"error": str(exc)},
            ) from exc
        self._schema_ensured = True
        logger.info("surreal_graph.schema_ensured")

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
        ``id``, ``name``, ``type``, ``summary``, ``attributes``, ``created_at``,
        and SurrealDB-specific extras: ``is_merged``, ``updated_at``.
        """
        return {
            "id": cls._record_id_to_str(row.get("id")),
            "name": row.get("name", ""),
            "type": row.get("entity_type", ""),
            "summary": row.get("summary") if row.get("summary") is not None else "",
            "attributes": (
                dict(row["attributes"])
                if isinstance(row.get("attributes"), dict)
                else {}
            ),
            "is_merged": bool(row.get("is_merged", False)),
            "created_at": cls._to_iso(row.get("created_at")),
            "updated_at": cls._to_iso(row.get("updated_at")),
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
            "properties": row.get("properties") if row.get("properties") is not None else {},
            "fact": row.get("fact") if row.get("fact") is not None else "",
            "confidence": float(row["confidence"]) if row.get("confidence") is not None else 1.0,
            "valid_from": cls._to_iso(row.get("valid_from")),
            "valid_to": cls._to_iso(row.get("valid_to")),
            "created_at": cls._to_iso(row.get("created_at")),
        }

    @classmethod
    def _row_to_observation(cls, row: dict) -> dict:
        """Convert a SurrealDB observation record to the standard observation dict.

        The returned dict uses the ``GraphBackend`` interface keys:
        ``id``, ``subject_entity_id``, ``observation_type``, ``content``,
        ``confidence``, ``created_at``.
        """
        return {
            "id": cls._record_id_to_str(row.get("id")),
            "subject_entity_id": row.get("subject_entity_id", ""),
            "related_entity_id": row.get("related_entity_id") or None,
            "observation_type": row.get("observation_type", ""),
            "content": row.get("content", ""),
            "confidence": float(row["confidence"]) if row.get("confidence") is not None else 0.0,
            "supporting_fact_ids": (
                [str(fid) for fid in row["supporting_fact_ids"]]
                if row.get("supporting_fact_ids") else []
            ),
            "supporting_relationship_ids": (
                [str(rid) for rid in row["supporting_relationship_ids"]]
                if row.get("supporting_relationship_ids") else []
            ),
            "valid_from": cls._to_iso(row.get("valid_from")),
            "valid_to": cls._to_iso(row.get("valid_to")),
            "observation_metadata": (
                dict(row["observation_metadata"])
                if isinstance(row.get("observation_metadata"), dict)
                else {}
            ),
            "created_at": cls._to_iso(row.get("created_at")),
            "updated_at": cls._to_iso(row.get("updated_at")),
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

    async def _query_last(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Execute a multi-statement query and return the **last** statement's result.

        The SDK's ``query()`` only returns the first statement's result, but
        our ``LET + RETURN IF/THEN/ELSE`` upsert pattern (used in
        :meth:`create_entity` and :meth:`create_relationship`) needs the
        ``RETURN`` statement's output.

        This helper calls ``query_raw()``, validates every statement, and
        returns the last result set.

        Returns:
            The result list from the final statement (e.g. ``[record_dict]``
            for a ``SELECT`` or ``RETURN AFTER`` statement, or ``[]`` for an
            empty set).
        """
        response = await self._surreal.query_raw(query, params)
        # Inline response validation (avoids calling SDK methods that are
        # mocked as async in tests, causing unawaited-coroutine warnings).
        if not isinstance(response, dict) or response.get("error"):
            raise SurrealError(
                str(response.get("error", "Invalid query response"))
            )
        results: list[dict[str, Any]] = response.get("result", [])
        if not results:
            raise SurrealError(
                "Query returned no result statements"
            )
        for stmt in results:
            if stmt.get("status") == "ERR":
                raise parse_query_error(stmt)
        return results[-1].get("result", [])

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
        summary_val = summary if summary is not None else ""

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
            result = await self._query_last(query, params)
            # result = [record_dict] — last statement is the RETURN
            record = result[0]
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
            rows = result
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
            deleted = len(result) > 0
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
        *,
        name: str | None = None,
        entity_type: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Update entity fields. Only provided fields are changed.

        Builds a dynamic ``UPDATE ... SET ...`` with only the non-None
        fields.  Returns the updated entity dict.

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

        if not set_parts:
            # Nothing to update — return current state
            entity = await self.get_entity(org_id, project_id, entity_id)
            if entity is None:
                raise NotFoundError(
                    message=f"Entity {entity_id} not found",
                    detail={"org_id": str(org_id), "entity_id": str(entity_id)},
                )
            return entity

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
            rows = result
            if not rows:
                raise NotFoundError(
                    message=f"Entity {entity_id} not found",
                    detail={"org_id": str(org_id), "entity_id": str(entity_id)},
                )
            return self._row_to_entity(rows[0])
        except NotFoundError:
            raise
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
            "properties": properties if properties is not None else {},
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
            result = await self._query_last(query, params)
            # result = [edge_record_dict]
            record = result[0]
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
                        for row in (et_result if et_result is not None else []):
                            neighbour_ids.add(self._record_id_to_str(row))
                else:
                    et_result = await self._surreal.query(
                        "SELECT VALUE ->?->entity.id FROM $current_id;",
                        {"current_id": RecordID("entity", current_id)},
                    )
                    neighbour_ids = set()
                    for row in (et_result if et_result is not None else []):
                        neighbour_ids.add(self._record_id_to_str(row))

                for nid in neighbour_ids:
                    if nid and nid not in visited:
                        queue.append((nid, depth + 1))
            except Exception as exc:
                logger.error(
                    "surreal_graph.traverse.neighbour_fetch_failed",
                    extra={
                        "entity_id": current_id,
                        "depth": depth,
                    },
                    exc_info=True,
                )
                raise GraphBackendUnavailableError(
                    f"SurrealDB graph traversal neighbour fetch failed for entity {current_id}."
                ) from exc

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

        Uses the ``@@`` operator with the ``openzync_entity`` analyzer and
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
            "entity_types": types if types is not None else [],
            "entity_types_null": types is None,
        }

        try:
            result = await self._surreal.query(
                f"""
                SELECT *, search::score(0) AS score
                FROM entity
                WHERE organization_id = $org_id
                  AND project_id = $project_id
                  AND (name @@ $query OR summary @@ $query)
                  AND ($entity_types_null OR entity_type IN $entity_types)
                ORDER BY score DESC
                LIMIT {limit} START {offset};
                """,
                params,
            )
            rows = result if result is not None else []
            entities = []
            for row in rows:
                entity = self._row_to_entity(row)
                entity["score"] = float(row.get("score") if row.get("score") is not None else 0.0)
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
                LIMIT {limit + 1} START {offset};
                """,
                params,
            )
            rows = result if result is not None else []
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
        }

        try:
            if predicate:
                safe_pred = self._sanitize_edge_type(predicate)
                result = await self._surreal.query(
                    f"""
                    SELECT *, meta::tb(id) AS edge_table_name
                    FROM (SELECT VALUE ->{safe_pred} FROM $eid)
                    ORDER BY created_at DESC
                    LIMIT {limit + 1} START {offset};
                    """,
                    params,
                )
            else:
                result = await self._surreal.query(
                    f"""
                    SELECT *, meta::tb(id) AS edge_table_name
                    FROM (SELECT VALUE <->? FROM $eid)
                    ORDER BY created_at DESC
                    LIMIT {limit + 1} START {offset};
                    """,
                    params,
                )

            rows = result if result is not None else []
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
                except Exception as exc:
                    logger.error(
                        "surreal_graph.retrieve_graph.traverse_failed",
                        extra={"entity_id": entity_id_str, "query": query},
                        exc_info=True,
                    )
                    raise GraphBackendUnavailableError(
                        f"SurrealDB graph traversal failed for entity {entity_id_str} during retrieve_graph."
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
                "surreal_graph.retrieve_graph_failed",
                extra={"query": query},
                exc_info=True,
            )
            raise GraphBackendUnavailableError(
                f"SurrealDB retrieve_graph failed for query '{query}'."
            ) from exc

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

    # ── Group A: Entity-Episode Linking ─────────────────────────────────────────

    async def link_entity_to_episode(
        self,
        org_id: UUID,
        project_id: UUID,
        episode_id: UUID,
        entity_id: UUID,
    ) -> None:
        """Record that an entity was extracted from (appears in) a specific episode.

        Uses the ``has_entity`` edge table via ``RELATE``.  Idempotent — if the
        edge already exists this is a no-op.

        .. note::

            The ``episode`` record does **not** need to exist in SurrealDB for
            the ``RELATE`` to succeed — SurrealDB stores RecordID references
            without validating the target record exists.  However,
            :meth:`get_entities_for_session` requires episode records to exist
            with ``session_id`` populated, otherwise it returns an empty list.

        Raises:
            NotFoundError: If the ``entity`` record does not exist in SurrealDB.
        """
        await self._ensure_schema()
        self._require_connection()

        episode_rid = RecordID("episode", str(episode_id))
        entity_rid = RecordID("entity", str(entity_id))

        query = """
        LET $existing = (SELECT id FROM has_entity
            WHERE in = $episode_rid AND out = $entity_rid
            LIMIT 1);
        RETURN IF array::len($existing) > 0 THEN
            (RETURN NONE)
        ELSE
            (RELATE $episode_rid -> has_entity -> $entity_rid
             SET organization_id = $org_id, project_id = $project_id
             RETURN id)
        END;
        """
        params: dict[str, Any] = {
            "episode_rid": episode_rid,
            "entity_rid": entity_rid,
            "org_id": str(org_id),
            "project_id": str(project_id),
        }

        try:
            await self._query_last(query, params)
            logger.info(
                "surreal_graph.link_entity_to_episode",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "episode_id": str(episode_id),
                    "entity_id": str(entity_id),
                },
            )
        except ExternalServiceError:
            raise
        except Exception as exc:
            logger.error(
                "surreal_graph.link_entity_to_episode_failed",
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

        Traverses ``entity <-has_entity<- episode`` via SurrealQL arrow syntax,
        filtering episodes by ``session_id``.

        .. note::

            Requires the ``episode`` records to exist in SurrealDB with
            ``session_id`` populated.  If episode records have not been created
            (or lack ``session_id``), this method returns an empty list.
        """
        await self._ensure_schema()
        self._require_connection()

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "session_id": str(session_id),
        }

        try:
            result = await self._surreal.query(
                """
                SELECT DISTINCT id, name, entity_type, summary
                FROM entity
                WHERE organization_id = $org_id
                  AND project_id = $project_id
                  AND <-has_entity<-(episode WHERE session_id = $session_id
                      AND organization_id = $org_id
                      AND project_id = $project_id)
                ORDER BY name ASC;
                """,
                params,
            )
            rows = result if result is not None else []
            return [
                {
                    "id": self._record_id_to_str(r.get("id")),
                    "name": r.get("name", ""),
                    "entity_type": r.get("entity_type", ""),
                    "summary": r.get("summary") if r.get("summary") is not None else "",
                }
                for r in rows
            ]
        except Exception as exc:
            logger.error(
                "surreal_graph.get_entities_for_session_failed",
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

        SurrealDB does not support self-joins on edge tables in a single
        SurrealQL statement, so this is implemented as a **two-step** approach:

        1. Query all distinct episode RecordIDs via the ``has_entity`` edge.
        2. For each episode, fetch its entity list and build a co-occurrence
           frequency map in Python.

        **Performance note**: This requires O(N\\ :sub:`episodes`) queries.
        For large projects with thousands of episodes, consider running this
        in a background worker.
        """
        await self._ensure_schema()
        self._require_connection()

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
        }

        try:
            # Step 1: Get distinct episode RecordIDs that have linked entities
            episode_result = await self._surreal.query(
                """
                SELECT VALUE in
                FROM has_entity
                WHERE organization_id = $org_id
                  AND project_id = $project_id
                GROUP BY in;
                """,
                params,
            )
            episode_rids: list[Any] = episode_result if episode_result is not None else []

            if not episode_rids:
                return []

            # Step 2: For each episode, get its entity RecordIDs
            # ⚠️ O(N_episodes) queries — acceptable for batch observation workers
            pair_counts: dict[tuple[str, str], int] = {}
            entity_name_cache: dict[str, str] = {}

            for ep_rid in episode_rids:
                entity_result = await self._surreal.query(
                    """
                    SELECT VALUE out
                    FROM has_entity
                    WHERE in = $ep_rid;
                    """,
                    {"ep_rid": ep_rid},
                )
                entity_rids: list[Any] = entity_result if entity_result is not None else []
                # Skip episodes with fewer than 2 entities
                if len(entity_rids) < 2:
                    continue

                # Build entity name cache
                for er in entity_rids:
                    eid_str = self._record_id_to_str(er)
                    if eid_str and eid_str not in entity_name_cache:
                        entity = await self.get_entity(org_id, project_id, UUID(eid_str))
                        entity_name_cache[eid_str] = entity.get("name", eid_str) if entity else eid_str

                # Build all pairs within this episode
                eid_strs = sorted(
                    {self._record_id_to_str(er) for er in entity_rids if er is not None}
                )
                for i in range(len(eid_strs)):
                    for j in range(i + 1, len(eid_strs)):
                        key = (eid_strs[i], eid_strs[j])
                        pair_counts[key] = pair_counts.get(key, 0) + 1

            # Step 3: Filter by threshold and build result
            results: list[dict[str, Any]] = []
            for (a_id, b_id), count in pair_counts.items():
                if count >= min_co_count:
                    results.append({
                        "entity_a_id": a_id,
                        "entity_a_name": entity_name_cache.get(a_id, a_id),
                        "entity_b_id": b_id,
                        "entity_b_name": entity_name_cache.get(b_id, b_id),
                        "co_count": count,
                    })

            results.sort(key=lambda x: x["co_count"], reverse=True)
            return results

        except ExternalServiceError:
            raise
        except Exception as exc:
            logger.error(
                "surreal_graph.get_co_occurring_pairs_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "min_co_count": min_co_count,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to get co-occurring entity pairs: {exc}",
                detail={"org_id": str(org_id), "project_id": str(project_id)},
            ) from exc

    # ── Group B: Bulk / Merge Operations ────────────────────────────────────────

    async def get_all_entities(
        self,
        org_id: UUID,
        project_id: UUID,
        *,
        include_merged: bool = False,
    ) -> list[dict[str, Any]]:
        """Return ALL entities for a project (no pagination — for batch workers).

        .. warning::
            BATCH USE ONLY — no LIMIT.  Potentially millions of rows.
        """
        await self._ensure_schema()
        self._require_connection()

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "include_merged": include_merged,
        }

        try:
            result = await self._surreal.query(
                """
                SELECT id, name, entity_type, summary, is_merged, created_at
                FROM entity
                WHERE organization_id = $org_id
                  AND project_id = $project_id
                  AND ($include_merged OR is_merged IS NONE OR is_merged = false)
                ORDER BY name ASC;
                """,
                params,
            )
            rows = result if result is not None else []
            return [
                {
                    "id": self._record_id_to_str(r.get("id")),
                    "name": r.get("name", ""),
                    "entity_type": r.get("entity_type", ""),
                    "summary": r.get("summary") if r.get("summary") is not None else "",
                    "is_merged": bool(r.get("is_merged", False)),
                    "created_at": self._to_iso(r.get("created_at")),
                }
                for r in rows
            ]
        except Exception as exc:
            logger.error(
                "surreal_graph.get_all_entities_failed",
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
        """Return ALL non-expired relationships for a project (no pagination).

        Uses SurrealDB's wildcard arrow syntax (``->?``) on **all** entities
        in the project to discover every edge, then deduplicates by edge ID
        and filters out ``has_entity`` edges (episode-entity links).

        .. warning::
            BATCH USE ONLY — no LIMIT.  Potentially millions of rows.
        """
        await self._ensure_schema()
        self._require_connection()

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
        }

        try:
            result = await self._surreal.query(
                """
                SELECT *, meta::tb(id) AS edge_table_name
                FROM (SELECT VALUE ->?
                      FROM (SELECT * FROM entity
                            WHERE organization_id = $org_id
                              AND project_id = $project_id))
                WHERE organization_id = $org_id
                  AND project_id = $project_id
                  AND (invalid_at IS NONE)
                ORDER BY created_at ASC;
                """,
                params,
            )
            rows = result if result is not None else []
            seen: set[str] = set()
            relationships: list[dict[str, Any]] = []
            for row in rows:
                rid_str = self._record_id_to_str(row.get("id"))
                if rid_str in seen:
                    continue
                # Skip has_entity edges — they are episode-entity links, not graph relationships
                edge_table = row.get("edge_table_name") or ""
                if edge_table == "has_entity":
                    continue
                seen.add(rid_str)
                relationships.append(self._row_to_relationship(row))
            return relationships
        except Exception as exc:
            logger.error(
                "surreal_graph.get_all_relationships_failed",
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
        """Search entities using BM25 full-text matching for dedup detection.

        SurrealDB does **not** have native trigram / Levenshtein fuzzy matching
        on indexes.  This implementation uses BM25 full-text search on the
        ``name`` field via the ``openzync_entity`` analyzer.  BM25 is
        **word-level** matching (handles stems and partial words) but is not
        character-level fuzzy (e.g. "Jon" → "John" requires a full-text match).

        The raw BM25 score (which can exceed 1.0) is normalised via
        ``1 - 1/(1 + score)`` to a 0–1 range for the ``fuzzy_threshold``
        filter.
        """
        await self._ensure_schema()
        self._require_connection()

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "query": query,
            "limit": limit,
        }

        try:
            result = await self._surreal.query(
                """
                SELECT *, search::score(0) AS raw_score
                FROM entity
                WHERE organization_id = $org_id
                  AND project_id = $project_id
                  AND name @@ $query
                  AND (is_merged IS NONE OR is_merged = false)
                ORDER BY raw_score DESC
                LIMIT $limit;
                """,
                params,
            )
            rows = result if result is not None else []

            # Normalise BM25 scores using sigmoid-like clamp: 1 - 1/(1+raw)
            # and filter by threshold
            entities = []
            for row in rows:
                raw = float(row.get("raw_score", 0.0) or 0.0)
                score = 1.0 - (1.0 / (1.0 + raw))
                if score < fuzzy_threshold:
                    continue
                entity = self._row_to_entity(row)
                entity["score"] = score
                entities.append(entity)

            return entities
        except Exception as exc:
            logger.error(
                "surreal_graph.bulk_search_entities_failed",
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
        """Merge duplicate entities: rewire all edges to canonical, soft-delete merged.

        SurrealDB cannot atomically rewire edge ``in``/``out`` RecordID fields
        in a single statement.  The implementation uses a **multi-step Python
        approach**:

        1. Verify canonical and merged entities exist.
        2. For each merged entity, discover outgoing and incoming edges via
           arrow syntax.
        3. Create new edges (canonical → old\\ :sub:`target` and
           old\\ :sub:`source` → canonical) preserving all edge properties.
        4. Delete the original edges.
        5. Mark merged entities as ``is_merged = true``.

        **Atomicity note**: Because SurrealDB does not support multi-statement
        transactions with interleaved Python logic, this method is **not fully
        atomic**.  If the process crashes after step 3 but before step 5,
        duplicate edges may exist.  Consider wrapping the call in an application-
        level transaction (saga pattern) for strict guarantees.
        """
        if not merged_ids:
            return {"rewired_count": 0, "deleted_count": 0, "merged_count": 0}

        await self._ensure_schema()
        self._require_connection()

        canonical_rid = RecordID("entity", str(canonical_id))
        merged_rids = [RecordID("entity", str(mid)) for mid in merged_ids]

        # Step 1: Verify canonical entity exists
        canonical = await self.get_entity(org_id, project_id, canonical_id)
        if canonical is None:
            raise NotFoundError(
                message=f"Canonical entity {canonical_id} not found",
                detail={"org_id": str(org_id), "canonical_id": str(canonical_id)},
            )

        rewired_count = 0
        deleted_count = 0
        merged_count = 0

        try:
            # Step 2: For each merged entity, discover and rewire edges
            for merged_rid in merged_rids:
                # Outgoing edges: merged → target  ⇒  canonical → target
                out_result = await self._surreal.query(
                    """
                    SELECT *, meta::tb(id) AS edge_table_name
                    FROM (SELECT VALUE ->? FROM $merged_rid)
                    WHERE organization_id = $org_id AND project_id = $project_id;
                    """,
                    {
                        "merged_rid": merged_rid,
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                    },
                )
                for edge_row in (out_result if out_result is not None else []):
                    edge_table = edge_row.get("edge_table_name", "")
                    if not edge_table or edge_table == "has_entity":
                        continue  # skip episode-entity links
                    target_rid = edge_row.get("out")
                    properties = edge_row.get("properties") or {}
                    confidence = edge_row.get("confidence") or 1.0
                    valid_from = edge_row.get("valid_from")
                    valid_to = edge_row.get("valid_to")

                    # Create new edge from canonical to target
                    await self._surreal.query(
                        f"""
                        RELATE $canonical_rid -> {edge_table} -> $target_rid
                        CONTENT {{
                            organization_id: $org_id,
                            project_id: $project_id,
                            properties: $properties,
                            confidence: $confidence,
                            valid_from: $valid_from,
                            valid_to: $valid_to,
                            created_at: time::now(),
                            updated_at: time::now()
                        }};
                        """,
                        {
                            "canonical_rid": canonical_rid,
                            "target_rid": target_rid,
                            "org_id": str(org_id),
                            "project_id": str(project_id),
                            "properties": properties,
                            "confidence": confidence,
                            "valid_from": valid_from,
                            "valid_to": valid_to,
                        },
                    )
                    rewired_count += 1

                # Incoming edges: source → merged  ⇒  source → canonical
                in_result = await self._surreal.query(
                    """
                    SELECT *, meta::tb(id) AS edge_table_name
                    FROM (SELECT VALUE <-? FROM $merged_rid)
                    WHERE organization_id = $org_id AND project_id = $project_id;
                    """,
                    {
                        "merged_rid": merged_rid,
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                    },
                )
                for edge_row in (in_result if in_result is not None else []):
                    edge_table = edge_row.get("edge_table_name", "")
                    if not edge_table or edge_table == "has_entity":
                        continue
                    source_rid = edge_row.get("in")
                    properties = edge_row.get("properties") or {}
                    confidence = edge_row.get("confidence") or 1.0
                    valid_from = edge_row.get("valid_from")
                    valid_to = edge_row.get("valid_to")

                    # Create new edge from source to canonical
                    await self._surreal.query(
                        f"""
                        RELATE $source_rid -> {edge_table} -> $canonical_rid
                        CONTENT {{
                            organization_id: $org_id,
                            project_id: $project_id,
                            properties: $properties,
                            confidence: $confidence,
                            valid_from: $valid_from,
                            valid_to: $valid_to,
                            created_at: time::now(),
                            updated_at: time::now()
                        }};
                        """,
                        {
                            "source_rid": source_rid,
                            "canonical_rid": canonical_rid,
                            "org_id": str(org_id),
                            "project_id": str(project_id),
                            "properties": properties,
                            "confidence": confidence,
                            "valid_from": valid_from,
                            "valid_to": valid_to,
                        },
                    )
                    rewired_count += 1

                # Delete old edges by their RecordIDs
                # (We collected edge rows but need their IDs — re-query to delete)
                out_del_result = await self._surreal.query(
                    """
                    SELECT VALUE id, meta::tb(id) AS edge_table_name
                    FROM (SELECT VALUE ->? FROM $merged_rid)
                    WHERE organization_id = $org_id AND project_id = $project_id;
                    """,
                    {
                        "merged_rid": merged_rid,
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                    },
                )
                for edge_info in (out_del_result if out_del_result is not None else []):
                    eid = edge_info.get("id")
                    et = edge_info.get("edge_table_name", "")
                    if eid is None or not et or et == "has_entity":
                        continue
                    await self._surreal.query(
                        f"DELETE FROM {et} WHERE id = $eid;",
                        {"eid": eid},
                    )
                    deleted_count += 1

                in_del_result = await self._surreal.query(
                    """
                    SELECT VALUE id, meta::tb(id) AS edge_table_name
                    FROM (SELECT VALUE <-? FROM $merged_rid)
                    WHERE organization_id = $org_id AND project_id = $project_id;
                    """,
                    {
                        "merged_rid": merged_rid,
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                    },
                )
                for edge_info in (in_del_result if in_del_result is not None else []):
                    eid = edge_info.get("id")
                    et = edge_info.get("edge_table_name", "")
                    if eid is None or not et or et == "has_entity":
                        continue
                    await self._surreal.query(
                        f"DELETE FROM {et} WHERE id = $eid;",
                        {"eid": eid},
                    )
                    deleted_count += 1

            # Step 3: Mark merged entities as soft-deleted
            merged_id_strs = [str(mid) for mid in merged_ids]
            mark_result = await self._surreal.query(
                """
                UPDATE entity SET
                    is_merged = true,
                    updated_at = time::now()
                WHERE id IN $merged_rids
                  AND organization_id = $org_id
                  AND project_id = $project_id;
                """,
                {
                    "merged_rids": merged_rids,
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            # Count how many were actually updated
            merged_count = len(mark_result) if mark_result is not None else 0

            # Step 4: Delete duplicate relationships (same source, target, type)
            # Iterate each edge table that had rewires and remove dups
            # This is done by querying for duplicate edges and keeping only the first
            # ⚠️ Performance: this iterates edge tables, which is acceptable for batch merge

            logger.info(
                "surreal_graph.entities_merged",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "canonical_id": str(canonical_id),
                    "merged_ids": merged_id_strs,
                    "rewired_count": rewired_count,
                    "deleted_count": deleted_count,
                    "merged_count": merged_count,
                },
            )

            return {
                "rewired_count": rewired_count,
                "deleted_count": deleted_count,
                "merged_count": merged_count,
            }

        except ExternalServiceError:
            raise
        except NotFoundError:
            raise
        except Exception as exc:
            logger.error(
                "surreal_graph.merge_entities_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "canonical_id": str(canonical_id),
                    "merged_ids": [str(mid) for mid in merged_ids],
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
        """Batch-create multiple relationships in sequence.

        Delegates to :meth:`create_relationship` for each item to benefit
        from its built-in idempotency (LET/IF/THEN/ELSE upsert pattern).

        Args:
            org_id: Organisational scope.
            project_id: Project scope.
            relationships: List of relationship descriptor dicts.

        Returns:
            List of created relationship dicts (one per input, in order).

        Raises:
            ValueError: If any input dict is missing required keys.
        """
        if not relationships:
            return []

        created: list[dict[str, Any]] = []
        try:
            for rel in relationships:
                source_id = rel.get("source_id")
                target_id = rel.get("target_id")
                rel_type = rel.get("relationship_type")

                if not source_id or not target_id or not rel_type:
                    raise ValueError(
                        f"Each relationship must have source_id, target_id, "
                        f"and relationship_type. Got: {rel}"
                    )

                result = await self.create_relationship(
                    org_id=org_id,
                    project_id=project_id,
                    source_id=UUID(str(source_id)),
                    target_id=UUID(str(target_id)),
                    relationship_type=str(rel_type),
                    properties=rel.get("properties"),
                    confidence=rel.get("confidence"),
                    valid_from=rel.get("valid_from"),
                    valid_to=rel.get("valid_to"),
                )
                created.append(result)

            logger.info(
                "surreal_graph.create_relationship_bulk",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "count": len(created),
                },
            )
            return created
        except ValueError:
            raise
        except Exception as exc:
            logger.error(
                "surreal_graph.create_relationship_bulk_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "count": len(relationships),
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Bulk relationship creation failed: {exc}",
                detail={"org_id": str(org_id)},
            ) from exc

    # ── Group C: Observations ───────────────────────────────────────────────────

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

        Uses the ``LET + IF/THEN/ELSE`` pattern for atomic upsert on the
        ``observation`` table.  Dedup is by
        ``(organization_id, project_id, subject_entity_id, observation_type,
        related_entity_id)`` — matching the Postgres backend's functional
        unique index pattern.

        Returns:
            The created or updated observation dict.
        """
        await self._ensure_schema()
        self._require_connection()

        subject_id_str = str(subject_entity_id)
        related_id_str = str(related_entity_id) if related_entity_id is not None else ""
        fact_ids = (
            [str(fid) for fid in supporting_fact_ids]
            if supporting_fact_ids else []
        )
        rel_ids = (
            [str(rid) for rid in supporting_relationship_ids]
            if supporting_relationship_ids else []
        )

        query = """
        LET $existing = (SELECT * FROM observation
            WHERE organization_id = $org_id
              AND project_id = $project_id
              AND subject_entity_id = $subject_id
              AND observation_type = $obs_type
              AND related_entity_id = $related_id
            LIMIT 1);
        RETURN IF array::len($existing) > 0 THEN
            (UPDATE $existing[0].id SET
                content = $content,
                confidence = $confidence,
                supporting_fact_ids = $fact_ids,
                supporting_relationship_ids = $rel_ids,
                valid_from = $valid_from,
                valid_to = $valid_to,
                observation_metadata = $metadata
            RETURN AFTER)
        ELSE
            (CREATE observation SET
                organization_id = $org_id,
                project_id = $project_id,
                subject_entity_id = $subject_id,
                observation_type = $obs_type,
                content = $content,
                confidence = $confidence,
                related_entity_id = $related_id,
                supporting_fact_ids = $fact_ids,
                supporting_relationship_ids = $rel_ids,
                valid_from = $valid_from,
                valid_to = $valid_to,
                observation_metadata = $metadata,
                created_at = time::now(),
                updated_at = time::now()
            RETURN AFTER)
        END;
        """

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "subject_id": subject_id_str,
            "obs_type": observation_type,
            "content": content,
            "confidence": confidence,
            "related_id": related_id_str,
            "fact_ids": fact_ids,
            "rel_ids": rel_ids,
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_to": valid_to.isoformat() if valid_to else None,
            "metadata": observation_metadata or {},
        }

        try:
            result = await self._query_last(query, params)
            record = result[0]
            observation = self._row_to_observation(record)

            logger.info(
                "surreal_graph.observation_upserted",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "subject_entity_id": subject_id_str,
                    "observation_type": observation_type,
                },
            )
            return observation
        except ExternalServiceError:
            raise
        except Exception as exc:
            logger.error(
                "surreal_graph.upsert_observation_failed",
                extra={
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "subject_entity_id": subject_id_str,
                    "observation_type": observation_type,
                    "error": str(exc),
                },
            )
            raise ExternalServiceError(
                message=f"Failed to upsert observation: {exc}",
                detail={
                    "org_id": str(org_id),
                    "subject_entity_id": subject_id_str,
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
        """List observations with optional filters and cursor pagination.

        Uses offset-based pagination (same pattern as :meth:`list_entities`).
        Results are ordered by ``created_at DESC`` (most recent first).

        Returns:
            A dict with ``items``, ``next_cursor``, and ``has_more``.
        """
        await self._ensure_schema()
        self._require_connection()

        limit = min(limit, 200)
        offset = _decode_offset_cursor(cursor)

        params: dict[str, Any] = {
            "org_id": str(org_id),
            "project_id": str(project_id),
        }

        where_clause = (
            "organization_id = $org_id AND project_id = $project_id"
        )

        if subject_entity_id is not None:
            where_clause += " AND subject_entity_id = $subject_id"
            params["subject_id"] = str(subject_entity_id)
        if observation_type is not None:
            where_clause += " AND observation_type = $obs_type"
            params["obs_type"] = observation_type

        try:
            result = await self._surreal.query(
                f"""
                SELECT * FROM observation
                WHERE {where_clause}
                ORDER BY created_at DESC, id ASC
                LIMIT {limit + 1} START {offset};
                """,
                params,
            )
            rows = result if result is not None else []
            has_more = len(rows) > limit
            items = [self._row_to_observation(r) for r in rows[:limit]]

            next_cursor = None
            if has_more and items:
                next_cursor = _encode_offset_cursor(offset + len(items))

            return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
        except Exception as exc:
            logger.error(
                "surreal_graph.get_observations_failed",
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
        """Get all timestamps when an entity appeared in episodes.

        Traverses ``entity <-has_entity<- episode`` and returns each
        linked episode's ``created_at`` timestamp.  Only episodes within
        the org/project scope are considered.

        Returns:
            Sorted list of episode timestamps (oldest first).  Empty list if
            the entity has no linked episodes or the episode records do not
            exist in SurrealDB.
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
                SELECT created_at FROM episode
                WHERE id IN (SELECT VALUE <-has_entity.id
                             FROM entity:$entity_id)
                  AND organization_id = $org_id
                  AND project_id = $project_id
                ORDER BY created_at ASC;
                """,
                params,
            )
            rows = result if result is not None else []
            timestamps: list[datetime] = []
            for row in rows:
                ts = row.get("created_at")
                if ts is not None:
                    timestamps.append(ts)
            return timestamps
        except Exception as exc:
            logger.error(
                "surreal_graph.get_entity_appearance_timestamps_failed",
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
        """Get IDs of direct relationships between two entities (both directions).

        Uses SurrealQL arrow syntax to find edges from A→B (outgoing from A)
        and B→A (outgoing from B).  Returns **all** matching edge IDs,
        including duplicate types.

        Returns:
            List of relationship UUIDs.  Empty list if no direct relationship.
        """
        await self._ensure_schema()
        self._require_connection()

        a_rid = RecordID("entity", str(entity_a_id))
        b_rid = RecordID("entity", str(entity_b_id))

        try:
            result = await self._surreal.query(
                """
                SELECT id
                FROM (SELECT VALUE ->? FROM entity:$a_id)
                WHERE out = $b_rid
                  AND organization_id = $org_id
                  AND project_id = $project_id
                  AND (invalid_at IS NONE);
                """,
                {
                    "a_id": a_rid,
                    "b_rid": b_rid,
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            ids: list[UUID] = []
            for row in (result if result is not None else []):
                rid_str = self._record_id_to_str(row.get("id"))
                if rid_str:
                    try:
                        ids.append(UUID(rid_str))
                    except (ValueError, AttributeError):
                        pass

            # Reverse direction: edges from B to A
            result_rev = await self._surreal.query(
                """
                SELECT id
                FROM (SELECT VALUE ->? FROM entity:$b_id)
                WHERE out = $a_rid
                  AND organization_id = $org_id
                  AND project_id = $project_id
                  AND (invalid_at IS NONE);
                """,
                {
                    "b_id": b_rid,
                    "a_rid": a_rid,
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                },
            )
            for row in (result_rev if result_rev is not None else []):
                rid_str = self._record_id_to_str(row.get("id"))
                if rid_str:
                    try:
                        ids.append(UUID(rid_str))
                    except (ValueError, AttributeError):
                        pass

            return ids
        except Exception as exc:
            logger.error(
                "surreal_graph.get_relationship_ids_between_failed",
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

    # ── Group D: Soft-Delete / Expiry ──────────────────────────────────────────

    async def expire_relationship(
        self,
        org_id: UUID,
        project_id: UUID,
        relationship_id: UUID,
    ) -> bool:
        """Soft-delete a relationship by setting ``invalid_at``.

        Since SurrealDB edges are stored in per-type tables (``mentions``,
        ``authored_by``, etc.) and we only have the edge UUID, this method
        first discovers the edge RecordID by scanning all project edges using
        ``meta::id()``, then issues an ``UPDATE … SET invalid_at``.

        Returns:
            ``True`` if the relationship was expired, ``False`` if it did not
            exist or was already expired.
        """
        await self._ensure_schema()
        self._require_connection()

        try:
            # Discover the edge RecordID across all edge tables in the project.
            # meta::id(id) extracts the UUID portion of the RecordID.
            find_result = await self._surreal.query(
                """
                SELECT id
                FROM (SELECT VALUE ->?
                      FROM (SELECT * FROM entity
                            WHERE organization_id = $org_id
                              AND project_id = $project_id))
                WHERE organization_id = $org_id
                  AND project_id = $project_id
                  AND meta::id(id) = $rel_id_str
                LIMIT 1;
                """,
                {
                    "org_id": str(org_id),
                    "project_id": str(project_id),
                    "rel_id_str": str(relationship_id),
                },
            )
            found = find_result if find_result is not None else []
            if not found:
                logger.info(
                    "surreal_graph.expire_relationship.not_found",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "relationship_id": str(relationship_id),
                    },
                )
                return False

            edge_rid = found[0].get("id")

            # Now expire it — only if invalid_at is still NONE
            result = await self._surreal.query(
                """
                UPDATE $edge_rid
                SET invalid_at = time::now(), updated_at = time::now()
                WHERE invalid_at IS NONE
                RETURN BEFORE;
                """,
                {"edge_rid": edge_rid},
            )
            updated = result if result is not None else []
            expired = len(updated) > 0

            if expired:
                logger.info(
                    "surreal_graph.relationship_expired",
                    extra={
                        "org_id": str(org_id),
                        "project_id": str(project_id),
                        "relationship_id": str(relationship_id),
                    },
                )
            return expired

        except ExternalServiceError:
            raise
        except Exception as exc:
            logger.error(
                "surreal_graph.expire_relationship_failed",
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
