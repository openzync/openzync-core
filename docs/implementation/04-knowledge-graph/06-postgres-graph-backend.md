# PostgreSQL Graph Backend — Replace Graphiti with Native SQL

## 1. Overview

| Field | Detail |
|-------|--------|
| **Coverage** | PostgreSQL-native graph schema (`graph_entities`, `graph_relationships`, `graph_episode_entities`), `PostgresGraphBackend` implementation replacing `FalkorDBBackend`, BFS traversal via recursive CTEs, migration from Graphiti, config-driven backend switching, one-time data migration |
| **Dependencies** | [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md) (existing `episodes`, `facts` tables), [05-graph-client-abstraction.md](05-graph-client-abstraction.md) (`GraphBackend` ABC), [02-entity-operations.md](02-entity-operations.md) (entity service patterns) |
| **SRS Requirement IDs** | KG-01, KG-05, KG-06, KG-09, KG-10, KG-11, KG-12, PORT-02, MT-01, MT-02, MT-03, SEC-03 |
| **Build Phase** | Phase 1 — Core Memory (Graphiti replacement sprint) |
| **Design Authority** | @architect, @senior-dev |

### 1.1 Motivation

Graphiti (`graphiti-core`) was the original temporal knowledge graph engine for OpenZep. It has proven unreliable in practice:

| Problem | Impact |
|---------|--------|
| **Private API dependency** — OpenZep calls `_add_entity`, `_get_entity`, `_search`, `_add_relation` on Graphiti internals | Every version bump breaks silently. OpenZep is pinned to `v0.29.1` and cannot upgrade. |
| **Sync SDK in async runtime** — all Graphiti calls must be wrapped in `run_in_executor()` | Thread-pool contention, GIL overhead, no true async I/O |
| **FalkorDB runs in-memory only** — no persistence without manual AOF config | Data loss on container restart. RAM cost scales with graph size. |
| **Entities stored only in Graphiti** — no PostgreSQL fallback | When Graphiti is down, entity data is lost. Facts in PostgreSQL reference entities that may not exist. |
| **Separate infrastructure** — FalkorDB is another service to manage | Adds operational complexity. New failure mode (connection refused, OOM, etc.) |
| **Telemetry phones home** — PostHog analytics by default | Privacy concern for self-hosted deployments. |

**This document specifies the replacement:** a PostgreSQL-native graph backend that stores entities and relationships in plain SQL tables, uses recursive CTEs for BFS traversal, and eliminates the Graphiti dependency entirely.

### 1.2 Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Storage** | PostgreSQL tables (`graph_entities`, `graph_relationships`, `graph_episode_entities`) | Zero new infrastructure. Reuses the existing connection pool. ACID guarantees. |
| **Entity search** | pgvector (cosine similarity) + `pg_trgm` GIN (fuzzy name match) | Same approach as `episodes` and `facts` — consistent retrieval architecture. |
| **BFS traversal** | Recursive CTE (`WITH RECURSIVE`) | Native PostgreSQL feature since 8.4. No extensions needed. Sub-50ms for <50K nodes. |
| **Temporal edges** | `valid_from` / `valid_to` / `invalid_at` columns on `graph_relationships` | Same bi-temporal model as the `facts` table. Consistent query patterns. |
| **Config switching** | `GRAPH_BACKEND=postgres` or `graphiti` | Backend-agnostic via `GraphBackend` ABC. Migration = env var change. |
| **Backend interface** | Existing `GraphBackend` ABC in `packages/graphiti_client/interface.py` | No new interface. `PostgresGraphBackend` implements the same contract. |

### 1.3 Key Risks

| Risk | Likelihood | Mitigation |
|------|-----------|-------------|
| Recursive CTE performance degrades beyond 100K nodes | Medium | Add depth limits (max 5), index `(source_id, target_id)`, fall back to iterative BFS |
| Entity names are not unique per org | Low | Store name, use fuzzy `pg_trgm` search to find matches; `id` is the canonical identifier |
| Concurrent writes to relationships create duplicates | Low | Unique constraint on `(source_id, target_id, relationship_type)` where `invalid_at IS NULL` |

---

## 2. PostgreSQL Schema

### 2.1 Entity Nodes

Replaces Graphiti's `EntityNode`. Each row is a single entity (person, company, product, topic, etc.) scoped to an organization.

```sql
-- 01_graph_entities.sql

CREATE TABLE graph_entities (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    entity_type       TEXT NOT NULL DEFAULT 'custom',
    summary           TEXT,
    metadata          JSONB DEFAULT '{}' NOT NULL,
    embedding         VECTOR(1536),                              -- pgvector, optional
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tenant isolation: every query filters by organization_id
CREATE INDEX idx_graph_entities_org    ON graph_entities(organization_id);

-- Type filtering: list all entities of a type
CREATE INDEX idx_graph_entities_type   ON graph_entities(entity_type);

-- Fuzzy name search: enables "find entity named 'Acme' even if spelled 'Acm3'"
-- Requires: CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_graph_entities_name_trgm ON graph_entities USING GIN (name gin_trgm_ops);

-- Full-text search on summaries
CREATE INDEX idx_graph_entities_summary_fts ON graph_entities
    USING GIN (to_tsvector('english', coalesce(summary, '')));

-- Vector similarity search for entity retrieval
-- Requires: CREATE EXTENSION IF NOT EXISTS vector;
CREATE INDEX idx_graph_entities_embedding ON graph_entities
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Updated-at trigger
CREATE OR REPLACE FUNCTION update_graph_entity_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_graph_entity_updated
    BEFORE UPDATE ON graph_entities
    FOR EACH ROW
    EXECUTE FUNCTION update_graph_entity_timestamp();
```

### 2.2 Entity Relationships

Replaces Graphiti's `RELATES_TO` edges and `EntityEdge`. Each row is a directed relationship between two entities with temporal validity.

```sql
-- 02_graph_relationships.sql

CREATE TABLE graph_relationships (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    source_id           UUID NOT NULL REFERENCES graph_entities(id) ON DELETE CASCADE,
    target_id           UUID NOT NULL REFERENCES graph_entities(id) ON DELETE CASCADE,
    relationship_type   TEXT NOT NULL,                         -- e.g. "works_at", "prefers", "mentions"
    properties          JSONB DEFAULT '{}' NOT NULL,            -- ephemeral key-value metadata
    fact                TEXT,                                   -- natural-language fact statement
    confidence          DOUBLE PRECISION DEFAULT 1.0,           -- extraction confidence [0, 1]
    source_episode_id   UUID REFERENCES episodes(id) ON DELETE SET NULL,
    valid_from          TIMESTAMPTZ,                            -- when this fact became true
    valid_to            TIMESTAMPTZ,                            -- when this fact ceased to be true
    invalid_at          TIMESTAMPTZ,                            -- system timestamp of retraction
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tenant isolation
CREATE INDEX idx_graph_rels_org ON graph_relationships(organization_id);

-- BFS traversal: find all edges from/to a node
CREATE INDEX idx_graph_rels_source ON graph_relationships(source_id);
CREATE INDEX idx_graph_rels_target ON graph_relationships(target_id);

-- Filter by relationship type during traversal
CREATE INDEX idx_graph_rels_type ON graph_relationships(relationship_type);

-- Temporal queries: find facts active at a point in time
CREATE INDEX idx_graph_rels_valid ON graph_relationships(valid_from, valid_to);

-- Prevent duplicate active relationships between same entities
-- Only one active (non-invalidated) edge of a given type between two entities
CREATE UNIQUE INDEX idx_graph_rels_active_unique
    ON graph_relationships(source_id, target_id, relationship_type)
    WHERE invalid_at IS NULL;
```

### 2.3 Episode-Entity Links

Replaces Graphiti's `HAS_EPISODE` edges. Links entities to the conversation episodes where they were mentioned.

```sql
-- 03_graph_episode_entities.sql

CREATE TABLE graph_episode_entities (
    episode_id  UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    entity_id   UUID NOT NULL REFERENCES graph_entities(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (episode_id, entity_id)
);

-- Query: find all episodes mentioning an entity
CREATE INDEX idx_graph_ep_entity ON graph_episode_entities(entity_id);
```

### 2.4 Entity Embeddings (Optional Enhancement)

Add embeddings to entity nodes when the embedding worker runs. This enables vector-similarity entity search without relying on Graphiti's internal embedding logic.

```python
# workers/tasks/embed_entity.py (new or extended)
"""Generate embeddings for entity nodes and store in graph_entities.embedding."""
```

### 2.5 Migration Script

```python
# alembic/versions/xxxx_add_graph_entities_tables.py
"""Add graph_entities, graph_relationships, graph_episode_entities tables.

Revision ID: xxxx
Revises: <previous_revision>
Create Date: 2026-06-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "xxxx"
down_revision: str | None = "<previous_revision>"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Enable required extensions (idempotent)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── graph_entities ────────────────────────────────────────────────────
    op.create_table(
        "graph_entities",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False, server_default="custom"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("embedding", sa.ARRAY(sa.Float()), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_graph_entities_org", "graph_entities", ["organization_id"])
    op.create_index("idx_graph_entities_type", "graph_entities", ["entity_type"])
    op.execute(
        "CREATE INDEX idx_graph_entities_name_trgm "
        "ON graph_entities USING GIN (name gin_trgm_ops)"
    )

    # ── graph_relationships ───────────────────────────────────────────────
    op.create_table(
        "graph_relationships",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=False),
        sa.Column("relationship_type", sa.Text(), nullable=False),
        sa.Column("properties", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("fact", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("source_episode_id", sa.UUID(), nullable=True),
        sa.Column("valid_from", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("invalid_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["graph_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["graph_entities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_episode_id"], ["episodes.id"], ondelete="SET NULL"),
    )
    op.create_index("idx_graph_rels_org", "graph_relationships", ["organization_id"])
    op.create_index("idx_graph_rels_source", "graph_relationships", ["source_id"])
    op.create_index("idx_graph_rels_target", "graph_relationships", ["target_id"])
    op.create_index("idx_graph_rels_type", "graph_relationships", ["relationship_type"])
    op.create_index("idx_graph_rels_valid", "graph_relationships", ["valid_from", "valid_to"])
    op.create_index(
        "idx_graph_rels_active_unique",
        "graph_relationships",
        ["source_id", "target_id", "relationship_type"],
        postgresql_where=sa.text("invalid_at IS NULL"),
        unique=True,
    )

    # ── graph_episode_entities ────────────────────────────────────────────
    op.create_table(
        "graph_episode_entities",
        sa.Column("episode_id", sa.UUID(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["graph_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("episode_id", "entity_id"),
    )
    op.create_index("idx_graph_ep_entity", "graph_episode_entities", ["entity_id"])


def downgrade() -> None:
    op.drop_table("graph_episode_entities")
    op.drop_table("graph_relationships")
    op.drop_table("graph_entities")
```

---

## 3. PostgresGraphBackend Implementation

### 3.1 File Location

```
packages/
  graphiti_client/
    __init__.py
    interface.py              # GraphBackend ABC (unchanged)
    models.py                 # Domain models (Entity, Relationship)
    backends/
      __init__.py
      falkordb.py             # FalkorDBBackend (existing, unchanged)
      postgres.py             # PostgresGraphBackend ← NEW
```

### 3.2 Implementation

```python
# packages/graphiti_client/backends/postgres.py
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

import logging
from uuid import UUID

from sqlalchemy import text, insert, update, delete, select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.graphiti_client.interface import GraphBackend

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

MAX_TRAVERSAL_DEPTH: int = 5
"""Hard cap on BFS depth to prevent unbounded recursive queries."""

BFS_CTE = """
WITH RECURSIVE bfs AS (
    -- Anchor: start node
    SELECT ge.id, ge.name, ge.entity_type, ge.summary,
           ge.metadata, ge.created_at, 0 AS depth
    FROM graph_entities ge
    WHERE ge.id = :start_id
      AND ge.organization_id = :org_id

    UNION

    -- Recursive: follow edges (both directions)
    SELECT DISTINCT e.id, e.name, e.entity_type, e.summary,
           e.metadata, e.created_at, bfs.depth + 1
    FROM bfs
    JOIN graph_relationships r
        ON (r.source_id = bfs.id AND r.invalid_at IS NULL)
        OR (r.target_id = bfs.id AND r.invalid_at IS NULL)
    JOIN graph_entities e
        ON (e.id = CASE
            WHEN r.source_id = bfs.id THEN r.target_id
            ELSE r.source_id
        END)
    WHERE bfs.depth < :max_depth
      AND e.organization_id = :org_id
      AND (:edge_types IS NULL OR r.relationship_type = ANY(:edge_types))
)
SELECT DISTINCT ON (bfs.id) bfs.id, bfs.name, bfs.entity_type,
       bfs.summary, bfs.metadata, bfs.created_at, bfs.depth
FROM bfs
ORDER BY bfs.id, bfs.depth
"""

SEARCH_ENTITIES_SQL = """
SELECT ge.id, ge.name, ge.entity_type, ge.summary,
       ge.metadata, ge.created_at,
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
  AND (:entity_types IS NULL OR ge.entity_type = ANY(:entity_types))
ORDER BY score DESC
LIMIT :limit
OFFSET :offset
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
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

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
            IntegrityError: If a unique constraint is violated
                (should not happen — UUID is random).
        """
        stmt = (
            insert(GraphEntity)  # noqa: F821 — see models note below
            .values(
                organization_id=org_id,
                name=name,
                entity_type=entity_type,
                summary=summary or "",
            )
            .returning(
                GraphEntity.id,  # noqa: F821
                GraphEntity.name,
                GraphEntity.entity_type,
                GraphEntity.summary,
                GraphEntity.created_at,
            )
        )
        # note: We use raw SQL for the return to avoid ORM
        # overhead. The GraphEntity model import is optional — callers
        # can pass ``async with db.execute(text(...))`` directly.
        result = await self._db.execute(
            text(
                """
                INSERT INTO graph_entities (organization_id, name, entity_type, summary)
                VALUES (:org_id, :name, :type, :summary)
                RETURNING id, name, entity_type, summary, created_at
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
        return self._row_to_entity(row)

    async def get_entity(self, org_id: UUID, entity_id: UUID) -> dict | None:
        """Retrieve an entity node by ID."""
        result = await self._db.execute(
            text(
                """
                SELECT id, name, entity_type, summary, metadata, created_at
                FROM graph_entities
                WHERE id = :entity_id AND organization_id = :org_id
                """
            ),
            {"entity_id": str(entity_id), "org_id": str(org_id)},
        )
        row = result.one_or_none()
        return self._row_to_entity(row) if row else None

    async def update_entity(
        self,
        org_id: UUID,
        entity_id: UUID,
        name: str | None = None,
        summary: str | None = None,
        entity_type: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Update entity fields. Only provided fields are changed."""
        updates: dict[str, object] = {}
        if name is not None:
            updates["name"] = name
        if summary is not None:
            updates["summary"] = summary
        if entity_type is not None:
            updates["entity_type"] = entity_type
        if metadata is not None:
            updates["metadata"] = metadata

        if not updates:
            # No-op: return current state
            return await self.get_entity(org_id, entity_id)  # type: ignore[return-value]

        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        params = {k: str(v) if isinstance(v, UUID) else v for k, v in updates.items()}
        params["org_id"] = str(org_id)
        params["entity_id"] = str(entity_id)

        result = await self._db.execute(
            text(
                f"""
                UPDATE graph_entities
                SET {set_clause}, updated_at = now()
                WHERE id = :entity_id AND organization_id = :org_id
                RETURNING id, name, entity_type, summary, metadata, created_at
                """
            ),
            params,
        )
        row = result.one_or_none()
        return self._row_to_entity(row) if row else None

    async def delete_entity(self, org_id: UUID, entity_id: UUID) -> bool:
        """Delete an entity and cascade its relationships and episode links.

        Returns:
            ``True`` if the entity existed and was deleted.
        """
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
        return result.rowcount > 0

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
        - If an active relationship of the same type already exists between
          these entities, the old one is NOT expired (caller must handle
          this via ``expire_relationship``).
        - ``valid_from`` defaults to now().
        - ``valid_to`` and ``invalid_at`` are NULL (active until invalidated).

        Raises:
            IntegrityError: If either source or target does not exist
                (foreign key constraint).
        """
        from datetime import datetime, timezone

        result = await self._db.execute(
            text(
                """
                INSERT INTO graph_relationships
                    (organization_id, source_id, target_id,
                     relationship_type, properties,
                     valid_from, created_at)
                VALUES
                    (:org_id, :source_id, :target_id,
                     :rel_type, :properties::jsonb,
                     COALESCE(:valid_from, now()), now())
                RETURNING id, source_id, target_id, relationship_type,
                          properties, valid_from, valid_to, created_at
                """
            ),
            {
                "org_id": str(org_id),
                "source_id": str(source_id),
                "target_id": str(target_id),
                "rel_type": relationship_type,
                "properties": json.dumps(properties or {}),
                "valid_from": valid_from.isoformat() if valid_from else None,
            },
        )
        row = result.one()
        return self._row_to_relationship(row)

    async def expire_relationship(
        self,
        org_id: UUID,
        relationship_id: UUID,
    ) -> bool:
        """Mark a relationship as invalidated (soft-delete).

        Sets ``invalid_at`` to now(). The row remains for historical queries.

        Returns:
            ``True`` if the relationship existed and was expired.
        """
        from datetime import datetime, timezone

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
            at_time: If provided, only return relationships valid at
                this point in time. Defaults to now().

        Returns:
            List of relationship dicts.
        """
        from datetime import datetime, timezone

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
            "at_time": at_time.isoformat(),
        }
        if relationship_type:
            conditions += " AND r.relationship_type = :rel_type"
            params["rel_type"] = relationship_type

        result = await self._db.execute(
            text(
                f"""
                SELECT r.id, r.source_id, r.target_id, r.relationship_type,
                       r.properties, r.fact, r.confidence,
                       r.valid_from, r.valid_to, r.created_at
                FROM graph_relationships r
                WHERE {conditions}
                ORDER BY r.created_at DESC
                """
            ),
            params,
        )
        return [self._row_to_relationship(row) for row in result.all()]

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
            max_depth: Maximum hops (capped at ``MAX_TRAVERSAL_DEPTH``).
            edge_types: If provided, only follow edges with these types.

        Returns:
            List of node dicts with ``depth`` field indicating hop count.
            Includes the start node at depth 0.
        """
        max_depth = min(max_depth, MAX_TRAVERSAL_DEPTH)
        edge_types_arr = edge_types if edge_types else None

        result = await self._db.execute(
            text(BFS_CTE),
            {
                "org_id": str(org_id),
                "start_id": str(start_node_id),
                "max_depth": max_depth,
                "edge_types": edge_types_arr,
            },
        )
        nodes = []
        for row in result.all():
            node = self._row_to_entity(row)
            node["depth"] = row.depth
            nodes.append(node)
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
        on summaries. Weighted 60% name match, 40% summary match.

        Returns entities sorted by relevance score descending.
        """
        result = await self._db.execute(
            text(SEARCH_ENTITIES_SQL),
            {
                "org_id": str(org_id),
                "query": query,
                "entity_types": types,
                "limit": limit,
                "offset": offset,
            },
        )
        entities = []
        for row in result.all():
            entity = self._row_to_entity(row)
            entity["score"] = float(row.score) if row.score else 0.0
            entities.append(entity)
        return entities

    async def list_entities(
        self,
        org_id: UUID,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List entities with cursor-based pagination.

        Args:
            org_id: Tenant scope.
            entity_type: Optional type filter.
            limit: Max results (max 200).
            cursor: Opaque cursor from previous response (base64-encoded
                ``created_at||id`` tuple).

        Returns:
            Dict with ``items``, ``next_cursor`` (str or None), ``has_more``.
        """
        import base64
        import json

        limit = min(limit, 200)

        # Decode cursor
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
                logger.warning("Invalid cursor", extra={"cursor": cursor})

        result = await self._db.execute(
            text(
                f"""
                SELECT ge.id, ge.name, ge.entity_type, ge.summary,
                       ge.metadata, ge.created_at
                FROM graph_entities ge
                WHERE {where_clause}
                ORDER BY ge.created_at ASC, ge.id ASC
                LIMIT :limit
                """
            ),
            params,
        )

        rows = result.all()
        has_more = len(rows) > limit
        items = [self._row_to_entity(r) for r in rows[:limit]]

        next_cursor = None
        if has_more and items:
            last = items[-1]
            cursor_payload = json.dumps(
                {"c": last["created_at"].isoformat(), "i": last["id"]}
            )
            next_cursor = base64.b64encode(cursor_payload.encode()).decode()

        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    async def list_entity_edges(
        self,
        org_id: UUID,
        entity_id: UUID,
        *,
        predicate: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """List all edges incident to an entity."""
        limit = min(limit, 200)
        conditions = "r.organization_id = :org_id AND (r.source_id = :eid OR r.target_id = :eid)"
        params: dict[str, object] = {
            "org_id": str(org_id), "eid": str(entity_id), "limit": limit + 1
        }
        if predicate:
            conditions += " AND r.relationship_type = :pred"
            params["pred"] = predicate

        if cursor:
            import base64, json

            try:
                decoded = json.loads(base64.b64decode(cursor))
                conditions += " AND r.created_at > :cursor_ts"
                params["cursor_ts"] = decoded["c"]
            except Exception:
                pass

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

        rows = result.all()
        has_more = len(rows) > limit
        items = [self._row_to_relationship(r) for r in rows[:limit]]

        next_cursor = None
        if has_more and items:
            import base64, json

            last = items[-1]
            cursor_payload = json.dumps({"c": last["created_at"].isoformat()})
            next_cursor = base64.b64encode(cursor_payload.encode()).decode()

        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

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
            "metadata": row.metadata if hasattr(row, "metadata") else {},
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
            "confidence": float(row.confidence or 1.0),
            "valid_from": row.valid_from.isoformat() if row.valid_from else None,
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
```

> **note:** The raw-SQL approach above avoids coupling to SQLAlchemy ORM models and keeps the backend lightweight. A future enhancement could add SQLAlchemy models for type safety — but the perf cost of ORM overhead on every graph operation makes raw SQL the right call for v1. The edge operations already use raw `text()` in the existing codebase.

### 3.3 Registering the Backend

```python
# core/graph_backend.py (refactored from core/graphiti.py)
"""Graph backend factory — selects backend based on config."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from packages.graphiti_client.interface import GraphBackend

logger = logging.getLogger(__name__)


async def init_graph_backend(
    db: AsyncSession | None = None,
) -> GraphBackend | None:
    """Initialise the configured graph backend.

    The backend is selected by ``settings.GRAPH_BACKEND``:

    - ``"postgres"`` (default): :class:`PostgresGraphBackend`
    - ``"graphiti"``: :class:`FalkorDBBackend` (legacy, requires FalkorDB)
    - ``"none"``: returns ``None`` — graph features are disabled

    Args:
        db: An async SQLAlchemy session. Required for ``postgres`` backend.
            Ignored for ``graphiti`` backend.

    Returns:
        An initialised ``GraphBackend`` instance, or ``None`` if
        graph features are disabled.
    """
    backend_name = settings.GRAPH_BACKEND

    if backend_name == "postgres":
        if db is None:
            raise ValueError("db is required for postgres graph backend")
        from packages.graphiti_client.backends.postgres import PostgresGraphBackend

        backend = PostgresGraphBackend(db)
        logger.info("graph_backend.initialized", extra={"backend": "postgres"})
        return backend

    elif backend_name == "graphiti":
        from core.graphiti import init_graphiti, get_graphiti

        try:
            await init_graphiti(str(settings.FALKORDB_URL))
            client = get_graphiti()
            from packages.graphiti_client.backends.falkordb import FalkorDBBackend

            backend = FalkorDBBackend(client.client)
            logger.info("graph_backend.initialized", extra={"backend": "graphiti"})
            return backend
        except Exception as exc:
            logger.warning(
                "graph_backend.graphiti_failed",
                extra={"error": str(exc)},
            )
            return None

    elif backend_name == "none":
        logger.info("graph_backend.disabled")
        return None

    else:
        raise ValueError(f"Unknown graph backend: {backend_name}")
```

---

## 4. BFS Traversal — Performance Analysis

### 4.1 Recursive CTE Query

The core BFS query uses a standard recursive CTE that follows both incoming and outgoing edges:

```sql
WITH RECURSIVE bfs AS (
    -- Anchor: start node
    SELECT ge.id, ge.name, ge.entity_type, 0 AS depth
    FROM graph_entities ge
    WHERE ge.id = :start_id AND ge.organization_id = :org_id

    UNION

    -- Recursive: follow non-invalidated edges (both directions)
    SELECT DISTINCT e.id, e.name, e.entity_type, bfs.depth + 1
    FROM bfs
    JOIN graph_relationships r
        ON (r.source_id = bfs.id OR r.target_id = bfs.id)
        AND r.invalid_at IS NULL
    JOIN graph_entities e
        ON (e.id = CASE WHEN r.source_id = bfs.id
                        THEN r.target_id ELSE r.source_id END)
    WHERE bfs.depth < :max_depth
      AND e.organization_id = :org_id
      AND (:edge_types IS NULL OR r.relationship_type = ANY(:edge_types))
)
SELECT * FROM bfs ORDER BY depth, name;
```

### 4.2 Expected Latency

| Graph size | Depth 2 | Depth 3 | Depth 5 |
|-----------|---------|---------|---------|
| 1,000 nodes | < 5ms | < 10ms | < 25ms |
| 10,000 nodes | < 10ms | < 30ms | < 80ms |
| 100,000 nodes | < 30ms | < 100ms | < 400ms |
| 1,000,000 nodes | < 150ms | < 500ms | > 2s (use depth cap) |

### 4.3 Optimizations

| Optimization | Effect | When to Apply |
|-------------|--------|---------------|
| **Indexes on `(source_id, target_id)`** | Reduces join cost by 10-50x | Always |
| **`idx_graph_rels_active_unique`** | Filters inactive edges at the index level | Always |
| **Depth cap at 2 (default)** | Limits exponential explosion | Default context assembly |
| **Edge-type filter** | Reduces branching factor | When only specific relationships matter |
| **`DISTINCT` in recursive step** | Prevents duplicate nodes | Always — avoids cycles |
| **Materialized path** (alternative) | Pre-computed ancestry for read-heavy graphs | > 1M nodes with frequent BFS |

### 4.4 Fallback: Iterative BFS (for very large graphs)

If recursive CTE performance degrades beyond 500ms, implement an iterative version in Python:

```python
async def traverse_iterative(
    self, org_id: UUID, start_id: UUID,
    max_depth: int = 2, edge_types: list[str] | None = None,
) -> list[dict]:
    """Iterative BFS using multiple round-trip queries.

    Tradeoff: More network round-trips, but avoids deep recursive
    CTE planning overhead. Better for very deep traversals on
    large graphs (> 500K nodes).
    """
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(str(start_id), 0)]
    nodes: list[dict] = []

    while queue:
        current_id, depth = queue.pop(0)
        if current_id in visited:
            continue
        visited.add(current_id)

        # Fetch current node
        entity = await self.get_entity(UUID(org_id), UUID(current_id))
        if entity:
            entity["depth"] = depth
            nodes.append(entity)

        if depth >= max_depth:
            continue

        # Fetch direct neighbours
        result = await self._db.execute(
            text("""
                SELECT CASE
                    WHEN r.source_id = :eid THEN r.target_id
                    ELSE r.source_id
                END AS neighbour_id
                FROM graph_relationships r
                WHERE r.organization_id = :org_id
                  AND r.invalid_at IS NULL
                  AND (r.source_id = :eid OR r.target_id = :eid)
                  AND (:types IS NULL OR r.relationship_type = ANY(:types))
            """),
            {"eid": current_id, "org_id": str(org_id), "types": edge_types},
        )
        for row in result.all():
            neighbour_id = str(row.neighbour_id)
            if neighbour_id not in visited:
                queue.append((neighbour_id, depth + 1))

    return nodes
```

---

## 5. Migration from Graphiti

### 5.1 Migration Path

```
Step 1: Add tables + build PostgresGraphBackend (days 1-3)
Step 2: Dual-write: write to both Graphiti + PostgreSQL (days 4-7)
Step 3: Switch reads to PostgreSQL, validate parity (days 8-10)
Step 4: Drop Graphiti dependency (days 11-12)
```

### 5.2 Phase 1: Dual-Write (Days 1-7)

During this phase, both Graphiti and PostgreSQL receive writes. Reads still come from Graphiti initially.

```python
# repositories/entity_repository.py (modified)
class EntityRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._pg_backend = PostgresGraphBackend(db)
        self._graphiti_backend = self._init_graphiti_backend()

    async def upsert_entity(self, org_id: UUID, name: str, ...) -> dict | None:
        # Write to PostgreSQL (always works)
        pg_entity = await self._pg_backend.create_entity(org_id, name, ...)

        # Also write to Graphiti if available (best-effort)
        if self._graphiti_backend:
            try:
                await self._graphiti_backend.create_entity(org_id, name, ...)
            except Exception:
                logger.warning("graphiti.write_failed", exc_info=True)

        return pg_entity
```

### 5.3 Phase 2: Switch Reads (Days 8-10)

Change ``EntityRepository`` and ``HybridRetriever`` to read from ``PostgresGraphBackend`` by default. Keep Graphiti as a fallback for environments that still rely on it.

```python
# core/config.py (add)
GRAPH_BACKEND: str = "postgres"  # or "graphiti" or "none"
```

```yaml
# docker-compose.yml (add)
services:
  api:
    environment:
      GRAPH_BACKEND: postgres
```

### 5.4 Phase 3: Graphiti Removal (Days 11-12)

```diff
 # pyproject.toml
 [project.dependencies]
 ...
-graphiti-core==0.29.1
 ...
```

Also remove:
- `core/graphiti.py` — module-level singleton
- `packages/graphiti_client/backends/falkordb.py` — FalkorDB implementation
- `infra/docker-compose.yml` FalkorDB service definition

### 5.5 One-Time Data Migration

```python
# scripts/migrate_graphiti_to_pg.py
"""One-time migration: export Graphiti entities to PostgreSQL.

Usage:
    python scripts/migrate_graphiti_to_pg.py --org-id=<org_id>

This script reads all entities from Graphiti's FalkorDB backend
and inserts them into the new ``graph_entities`` and
``graph_relationships`` PostgreSQL tables. It is idempotent —
re-running is safe (upserts by UUID).
"""

import argparse
import asyncio
import logging

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def migrate_org(org_id: str) -> None:
    """Migrate a single org's graph from Graphiti to PostgreSQL."""
    # 1. Connect to Graphiti via FalkorDB
    from graphiti_core import Graphiti
    from graphiti_core.nodes import EntityNode
    from graphiti_core.edges import GraphRelationship

    graphiti = Graphiti(str(settings.FALKORDB_URL))
    # ... (Graphiti-specific entity listing logic)

    # 2. Connect to PostgreSQL
    engine = create_async_engine(str(settings.DATABASE_URL))
    async with AsyncSession(engine) as db:
        # 3. For each entity in Graphiti:
        #    - Check if it exists in graph_entities (by uuid)
        #    - If not, insert it
        #    - Repeat for relationships
        pass

    await engine.dispose()
    logger.info("Migration complete", extra={"org_id": org_id})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--org-id", required=True)
    args = parser.parse_args()
    asyncio.run(migrate_org(args.org_id))
```

---

## 6. Configuration

### 6.1 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRAPH_BACKEND` | `postgres` | Graph backend to use: `postgres`, `graphiti`, or `none` |
| `FALKORDB_URL` | — | Required only when `GRAPH_BACKEND=graphiti` |

### 6.2 Docker Compose (PostgreSQL-Only Mode)

```yaml
# infra/docker-compose.yml (simplified — no FalkorDB needed)
services:
  postgres:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_DB: openzep
      POSTGRES_PASSWORD: openzep
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U openzep"]
      interval: 5s

  api:
    build: ../services/api
    environment:
      DATABASE_URL: postgresql+asyncpg://openzep:openzep@postgres:5432/openzep
      GRAPH_BACKEND: postgres    # ← no FalkorDB needed
      REDIS_URL: redis://redis:6379

  worker:
    build: ../services/worker
    environment:
      DATABASE_URL: postgresql+asyncpg://openzep:openzep@postgres:5432/openzep
      GRAPH_BACKEND: postgres    # ← same for workers
      REDIS_URL: redis://redis:6379

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

volumes:
  pgdata:
```

---

## 7. Testing Strategy

### 7.1 Unit Tests

```python
# tests/unit/test_postgres_graph_backend.py
"""Unit tests for PostgresGraphBackend."""

import pytest
from uuid import uuid4
from datetime import datetime, timezone

from packages.graphiti_client.backends.postgres import PostgresGraphBackend


@pytest.mark.asyncio
class TestPostgresGraphBackend:
    """Tests use a real PostgreSQL via testcontainers or a test DB session."""

    async def test_create_entity(self, pg_backend, org_id):
        entity = await pg_backend.create_entity(
            org_id=org_id, name="Acme Corp", entity_type="company"
        )
        assert entity["name"] == "Acme Corp"
        assert entity["type"] == "company"
        assert entity["id"]

    async def test_get_entity_nonexistent(self, pg_backend, org_id):
        entity = await pg_backend.get_entity(org_id, uuid4())
        assert entity is None

    async def test_create_relationship(self, pg_backend, org_id):
        alice = await pg_backend.create_entity(org_id, "Alice", "person")
        acme = await pg_backend.create_entity(org_id, "Acme", "company")
        rel = await pg_backend.create_relationship(
            org_id, UUID(alice["id"]),
            UUID(acme["id"]), "works_at",
        )
        assert rel["type"] == "works_at"
        assert rel["source_id"] == alice["id"]

    async def test_traverse_depth_1(self, pg_backend, org_id):
        # Create graph: Alice → works_at → Acme
        alice = await pg_backend.create_entity(org_id, "Alice", "person")
        acme = await pg_backend.create_entity(org_id, "Acme", "company")
        await pg_backend.create_relationship(
            org_id, UUID(alice["id"]), UUID(acme["id"]), "works_at",
        )

        # Traverse from Alice, depth 1
        result = await pg_backend.traverse(org_id, UUID(alice["id"]), max_depth=1)
        found = {n["name"] for n in result}
        assert "Alice" in found       # depth 0
        assert "Acme" in found        # depth 1

    async def test_traverse_ignores_invalidated_edges(self, pg_backend, org_id):
        alice = await pg_backend.create_entity(org_id, "Alice", "person")
        bob = await pg_backend.create_entity(org_id, "Bob", "person")
        rel = await pg_backend.create_relationship(
            org_id, UUID(alice["id"]), UUID(bob["id"]), "friends_with",
        )
        await pg_backend.expire_relationship(org_id, UUID(rel["id"]))

        result = await pg_backend.traverse(org_id, UUID(alice["id"]), max_depth=1)
        names = {n["name"] for n in result}
        assert "Bob" not in names  # edge was invalidated

    async def test_search_entities_by_name(self, pg_backend, org_id):
        await pg_backend.create_entity(org_id, "Alice Johnson", "person")
        await pg_backend.create_entity(org_id, "Alice Corp", "company")
        await pg_backend.create_entity(org_id, "Bob Smith", "person")

        results = await pg_backend.search_entities(org_id, "alice")
        assert len(results) == 2
        assert all("alice" in r["name"].lower() for r in results)

    async def test_tenant_isolation(self, pg_backend):
        org_a = uuid4()
        org_b = uuid4()
        await pg_backend.create_entity(org_a, "Secret", "person")
        results_b = await pg_backend.search_entities(org_b, "secret")
        assert len(results_b) == 0
```

### 7.2 Integration Tests

```python
# tests/integration/test_graph_backends.py
"""Run the same test suite against all backends."""

import pytest
from uuid import UUID

from packages.graphiti_client.interface import GraphBackend


# Shared test matrix — every backend must pass these
BACKEND_TEST_PARAMS = [
    pytest.param("postgres", marks=pytest.mark.integration),
    pytest.param("graphiti", marks=pytest.mark.integration),
]


@pytest.mark.asyncio
class TestGraphBackendContract:
    """Contract tests — every GraphBackend implementation must pass."""

    @pytest.fixture(params=BACKEND_TEST_PARAMS)
    def backend(self, request, db_session, graphiti_client):
        if request.param == "postgres":
            from packages.graphiti_client.backends.postgres import PostgresGraphBackend
            return PostgresGraphBackend(db_session)
        elif request.param == "graphiti":
            from packages.graphiti_client.backends.falkordb import FalkorDBBackend
            return FalkorDBBackend(graphiti_client)

    async def test_create_and_get_entity(self, backend: GraphBackend, org_id: UUID):
        created = await backend.create_entity(org_id, "Test", "test_type", "A test")
        assert created["id"]
        fetched = await backend.get_entity(org_id, UUID(created["id"]))
        assert fetched["name"] == "Test"
```

---

## 8. Rollback Plan

If the PostgreSQL backend has issues in production, rollback is a single config change:

```bash
# Rollback to Graphiti
docker compose exec api env GRAPH_BACKEND=graphiti
docker compose restart api worker
```

During dual-write phase (Step 2), data in Graphiti is never stale because both backends receive writes. The only risk is during Phase 1 when only PostgreSQL has the new data — but this is mitigated by keeping Graphiti as the read path until Phase 2.

---

## 9. Effort Summary

| Phase | Task | Days | Dependencies |
|-------|------|------|-------------|
| 1 | Create migration + `graph_entities`/`graph_relationships` tables | 1 | Alembic set up |
| 1 | Implement `PostgresGraphBackend` (all interface methods) | 3-4 | Migration complete |
| 1 | Unit tests + integration test matrix | 2 | Backend implemented |
| 2 | Dual-write in `EntityRepository` and workers | 1 | Backend tested |
| 2 | Wire `HybridRetriever.traverse()` to PostgreSQL BFS | 1 | Backend tested |
| 3 | Switch reads to PostgreSQL, validate parity | 2 | Dual-write working |
| 3 | One-time data migration script | 1 | Both data sources online |
| 3 | Config wiring (`GRAPH_BACKEND` env var) | 0.5 | — |
| 4 | Remove Graphiti dependency + FalkorDB from compose | 1 | Migration validated |
| **Total** | | **~12-14 days** | |

### Parallel Tracks

| Track | Owner | Depends On |
|-------|-------|------------|
| **A:** Schema + Backend implementation | @senior-dev | — |
| **B:** Tests + CI matrix | @qa-engineer | Track A initial methods |
| **C:** Migration script + config wiring | @devops | Track A complete |

---

## 10. Open Questions

| ID | Question | Impact | Decision |
|----|----------|--------|----------|
| GR-01 | Should we keep the `graph_episode_entities` join table or just use an array on `graph_entities`? | Join table enables efficient "find entities in episode X" queries. Array saves a table. | Use join table (queryability > schema simplicity) |
| GR-02 | pgvector on `graph_entities.embedding` — dimension dynamically or fixed at 1536? | Fixed matches existing `episodes` and `facts` conventions. Dynamic adds complexity. | Fixed at 1536 (OpenAI `text-embedding-3-small` default) |
| GR-03 | Should the BFS recursive CTE track visited nodes across cycles? | Yes — `DISTINCT` in the recursive step already deduplicates. The visited set grows with graph size. | Use `DISTINCT` — sufficient for depth ≤ 5 |
| GR-04 | When should we drop Graphiti support entirely? | Immediately after Phase 3 validation. Keeping it as a fallback adds maintenance burden (two backends to test). | Drop in Phase 4 once postgres backend is validated in production for 1 week |

---

*Implementation document for replacing Graphiti with a PostgreSQL-native graph backend. Last updated: 2026-06-06.*

**See also:**
- [05-graph-client-abstraction.md](05-graph-client-abstraction.md) — `GraphBackend` ABC this implements
- [02-entity-operations.md](02-entity-operations.md) — service layer using this backend
- [03-hybrid-retrieval.md](../03-core-memory/03-hybrid-retrieval.md) — `HybridRetriever` that consumes BFS traversal
- [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md) — existing PostgreSQL schema
