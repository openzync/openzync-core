# Graphiti Graph Schema — Node & Edge Definitions

## 1. Overview

| Field | Detail |
|-------|--------|
| **Coverage** | Graphiti node types, relationship types, property schemas, temporal query patterns, backend-specific considerations |
| **Dependencies** | `01-postgresql-schema.md` (FK references to user/session/episode IDs); Graphiti library (Apache 2.0); FalkorDB or Neo4j |
| **SRS Requirement IDs** | KG-01–KG-13, MT-01, MT-03, CTX-05, RET-03, NLP-01–NLP-04, NLP-15–NLP-17, OQ-01, OQ-02, OQ-05 |
| **Build Phase** | Phase 0 (Foundation — Graphiti setup), Phase 1 (Core — entity operations, temporal queries) |
| **Design Authority** | @architect (graph model decisions, backend abstraction) |

### 1.1 Architecture Decision

Graphiti is embedded as a Python library (not a sidecar). The application calls Graphiti's API directly from the service layer and worker tasks. Graphiti handles the connection to the underlying graph database (FalkorDB primary, Neo4j alternative).

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   Service Layer   │────▶│   Graphiti API    │────▶│  Graph Database   │
│ (OpenZep-core)   │     │  (getzep/graphiti)│     │  FalkorDB / Neo4j │
└──────────────────┘     └──────────────────┘     └──────────────────┘
```

### 1.2 Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Graph backend | FalkorDB (primary), Neo4j (alternative) | FalkorDB uses Redis wire protocol — shared infra, simpler ops; Neo4j for enterprise shops |
| org_id enforcement | **Application-level** in Graphiti client wrapper | Graphiti does not natively support multi-tenancy — we enforce it in the wrapper layer |
| RELATES_TO episodes | **Separate HAS_EPISODE relationships** (not array property) | Recommended: queryable, indexable, filterable (see §2.4 full tradeoff) |
| Temporal properties | `valid_from`, `valid_to`, `invalid_at` on RELATES_TO edges | Bi-temporal model matching the `facts` table in PostgreSQL |
| uuid uniqueness | **Application-enforced** via node property constraints | Both backends support uniqueness constraints, but syntax differs |

---

## 2. Data Model — Graph Schema

### 2.1 Node Types

#### EntityNode

Represents a real-world entity extracted from conversation (person, company, product, etc.).

```python
# packages/graphiti-client/models.py
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

@dataclass
class EntityNode:
    """A semantic entity in the knowledge graph.

    Maps to SRS KG-06 (Semantic layer): entities extracted from episodes.
    Every entity belongs to exactly one organization (org_id).
    """
    uuid: str = field(default_factory=lambda: str(uuid4()))
    name: str                        # Entity display name (e.g., "Alice", "Acme Corp")
    entity_type: str                 # Type: "person", "organization", "product", "location", "custom_..."
    summary: str = ""                # LLM-generated summary of known information about this entity
    created_at: str = ""             # ISO-8601 timestamp, set by Graphiti on creation
    org_id: str                      # Mandatory — tenant isolation key (MT-01)
    # Metadata
    source_episode_id: str | None = None  # FK to episodes table
    user_id: str | None = None            # FK to users table
    metadata: dict = field(default_factory=dict)
```

**FalkorDB node creation (Cypher-like GQL):**
```cypher
GRAPH.QUERY OpenZep "{org_id}" "
CREATE (:EntityNode {
    uuid: $uuid,
    name: $name,
    entity_type: $type,
    summary: $summary,
    created_at: $created_at,
    org_id: $org_id,
    source_episode_id: $source_episode_id,
    user_id: $user_id
})
"
```

**Neo4j node creation:**
```cypher
CREATE (e:EntityNode {
    uuid: $uuid,
    name: $name,
    entity_type: $type,
    summary: $summary,
    created_at: $created_at,
    org_id: $org_id,
    source_episode_id: $source_episode_id,
    user_id: $user_id
})
```

#### EpisodicNode

Represents a single conversation turn (message). Maps 1:1 with `episodes` table rows.

```python
@dataclass
class EpisodicNode:
    """A single conversation episode in the graph.

    Maps to SRS KG-05 (Episodic layer): raw conversation sessions.
    Links to the PostgreSQL episodes table via source_id.
    """
    uuid: str = field(default_factory=lambda: str(uuid4()))
    content: str                     # Message text (truncated for graph storage)
    source: str = "memgraph_api"     # Origin identifier
    source_id: str                   # FK to episodes.id in PostgreSQL
    created_at: str = ""             # ISO-8601 timestamp
    org_id: str                      # Mandatory — tenant isolation key
    user_id: str                     # FK to users table
    role: str = ""                   # "user", "assistant", "system", "tool"
```

#### CommunityNode

Represents a cluster of related entities, summarized by LLM.

```python
@dataclass
class CommunityNode:
    """An entity cluster with LLM-generated summary.

    Maps to SRS KG-08 (Community layer): entity clusters for long-context compression.
    Created by the community summarisation worker (NLP-15, NLP-16).
    """
    uuid: str = field(default_factory=lambda: str(uuid4()))
    name: str                        # Generated cluster name
    summary: str                     # LLM-generated natural language summary
    entity_count: int = 0            # Number of member entities
    created_at: str = ""             # ISO-8601 timestamp
    org_id: str                      # Mandatory — tenant isolation key
```

### 2.2 Relationship Types

#### RELATES_TO

Connects two `EntityNode` instances with a typed fact relationship. This edge carries the temporal fact payload.

```python
@dataclass
class RelatesToEdge:
    """Typed relationship between two entities.

    This edge IS the temporal fact — it carries the fact triple data
    (predicate, fact text) and the bi-temporal validity window.
    """
    uuid: str = field(default_factory=lambda: str(uuid4()))
    fact: str                        # Natural language fact text (e.g., "Alice prefers Python")
    fact_embedding: list[float] | None = None  # Vector embedding of fact text
    predicate: str                   # Relationship predicate (e.g., "prefers", "works_at")
    valid_from: str | None = None    # ISO-8601: when this fact became valid (valid time)
    valid_to: str | None = None      # ISO-8601: when this fact ceased to be valid
    invalid_at: str | None = None    # ISO-8601: system timestamp of invalidation (transaction time)
    created_at: str = ""             # ISO-8601: when this edge was created
    confidence: float = 1.0          # Extraction confidence [0, 1]
    source_episode_id: str | None = None  # FK to the episode that produced this fact
```

**FalkorDB:**
```cypher
GRAPH.QUERY OpenZep "{org_id}" "
MATCH (a:EntityNode {uuid: $source_uuid, org_id: $org_id})
MATCH (b:EntityNode {uuid: $target_uuid, org_id: $org_id})
CREATE (a)-[:RELATES_TO {
    uuid: $uuid,
    fact: $fact,
    predicate: $predicate,
    valid_from: $valid_from,
    valid_to: $valid_to,
    invalid_at: $invalid_at,
    created_at: $created_at,
    confidence: $confidence
}]->(b)
"
```

#### HAS_EPISODE

Connects an `EntityNode` to an `EpisodicNode`. Records which episodes mentioned this entity.

```python
@dataclass
class HasEpisodeEdge:
    """Links an entity to the episode(s) where it was mentioned.
    """
    created_at: str = ""             # ISO-8601 timestamp
    episode_id: str                  # FK to EpisodicNode.uuid
```

**FalkorDB:**
```cypher
GRAPH.QUERY OpenZep "{org_id}" "
MATCH (e:EntityNode {uuid: $entity_uuid, org_id: $org_id})
MATCH (ep:EpisodicNode {uuid: $episode_uuid, org_id: $org_id})
CREATE (e)-[:HAS_EPISODE {
    created_at: $created_at,
    episode_id: $episode_uuid
}]->(ep)
"
```

#### MEMBER_OF

Connects an `EntityNode` to a `CommunityNode`. Records community membership.

```python
@dataclass
class MemberOfEdge:
    """Entity membership in a community cluster.
    """
    created_at: str = ""             # ISO-8601 timestamp
```

### 2.3 Property Summary Matrix

| Property | EntityNode | EpisodicNode | CommunityNode | RELATES_TO | HAS_EPISODE | MEMBER_OF |
|----------|-----------|-------------|--------------|-----------|-------------|----------|
| `uuid` | ✅ (PK) | ✅ (PK) | ✅ (PK) | ✅ (PK) | — | — |
| `name` | ✅ | — | ✅ | — | — | — |
| `entity_type` | ✅ | — | — | — | — | — |
| `content` | — | ✅ | — | — | — | — |
| `summary` | ✅ | — | ✅ | — | — | — |
| `fact` | — | — | — | ✅ | — | — |
| `fact_embedding` | — | — | — | ✅ (nullable) | — | — |
| `predicate` | — | — | — | ✅ | — | — |
| `source` | — | ✅ | — | — | — | — |
| `source_id` | ✅ (nullable) | ✅ | — | — | — | — |
| `source_episode_id` | ✅ (nullable) | — | — | ✅ (nullable) | — | — |
| `user_id` | ✅ (nullable) | ✅ | — | — | — | — |
| `org_id` | ✅ | ✅ | ✅ | — | — | — |
| `role` | — | ✅ | — | — | — | — |
| `entity_count` | — | — | ✅ | — | — | — |
| `confidence` | — | — | — | ✅ | — | — |
| `valid_from` | — | — | — | ✅ (nullable) | — | — |
| `valid_to` | — | — | — | ✅ (nullable) | — | — |
| `invalid_at` | — | — | — | ✅ (nullable) | — | — |
| `created_at` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `metadata` | ✅ | — | — | — | — | — |

### 2.4 Design Tradeoff: RELATES_TO `episodes` Array vs Separate HAS_EPISODE

The SRS §7.2 shows `episodes` as a property on `RELATES_TO`. This is a design tradeoff worth documenting.

**Option A: `episodes` array property on RELATES_TO (SRS default)**

```cypher
(:EntityNode)-[:RELATES_TO {fact: "...", episodes: ["ep1", "ep2", "ep3"]}]->(:EntityNode)
```

| Pros | Cons |
|------|------|
| Single edge per entity pair — fewer total relationships | Cannot query individual episode-entity links |
| Simpler write path: one edge upsert | Array modification requires reading entire array first |
| Lower graph density | No index on array elements — full scan for "which entities in episode X?" |
| | Violates graph normalization principles |

**Option B: Separate HAS_EPISODE relationships (RECOMMENDED)**

```cypher
(:EntityNode)-[:RELATES_TO {fact: "...", uuid: "..."}]->(:EntityNode)
(:EntityNode)-[:HAS_EPISODE {created_at: "..."}]->(:EpisodicNode {uuid: "ep1"})
(:EntityNode)-[:HAS_EPISODE {created_at: "..."}]->(:EpisodicNode {uuid: "ep2"})
```

| Pros | Cons |
|------|------|
| Queryable: "find all entities mentioned in episode X" | More total edges (N entities × M episodes) |
| Filterable: "find facts mentioned in episodes within date range" | Slightly higher write overhead |
| Indexable: Neo4j indexes on HAS_EPISODE relationships | More complex traversal queries |
| Follows graph database best practices — edges are first-class | |
| Enables temporal filtering on when an entity was mentioned | |
| Supports efficient deletion: remove all HAS_EPISODE for a deleted episode | |

**Decision:** Use Option B (separate HAS_EPISODE relationships). The queryability advantage outweighs the edge count cost. For a user with 100 entities × 1000 episodes = 100,000 HAS_EPISODE edges — well within FalkorDB/Neo4j capacity.

### 2.5 Uniqueness Constraints

**FalkorDB** does not natively support property-level uniqueness constraints. Enforce uuid uniqueness at the application layer:

```python
# Before creating a node, check for existing uuid
async def _ensure_uuid_unique(self, graph: Graph, uuid: str, org_id: str) -> bool:
    result = await graph.query(
        f"GRAPH.QUERY OpenZep \"{org_id}\" "
        f"MATCH (n {{uuid: '{uuid}', org_id: '{org_id}'}}) RETURN count(n)"
    )
    return result[0][0] == 0  # True if no existing node
```

**Neo4j** supports node property uniqueness via indexes:

```cypher
CREATE CONSTRAINT entity_uuid_unique IF NOT EXISTS
FOR (n:EntityNode) REQUIRE n.uuid IS UNIQUE;

CREATE CONSTRAINT episodic_uuid_unique IF NOT EXISTS
FOR (n:EpisodicNode) REQUIRE n.uuid IS UNIQUE;

CREATE CONSTRAINT community_uuid_unique IF NOT EXISTS
FOR (n:CommunityNode) REQUIRE n.uuid IS UNIQUE;
```

---

## 3. Service Layer

### 3.1 Graphiti Client Wrapper

```python
# packages/graphiti-client/client.py
from typing import Any
from uuid import UUID

class GraphitiClient:
    """Thin wrapper around the Graphiti library.

    Responsibilities:
    1. Initialize Graphiti with configured backend (FalkorDB / Neo4j)
    2. Enforce org_id on every operation (tenant isolation)
    3. Expose typed methods for node/edge CRUD
    4. Hide backend-specific query syntax from callers
    """

    def __init__(
        self,
        backend: str = "falkordb",       # "falkordb" or "neo4j"
        url: str = "redis://localhost:6380",
        llm_client: Any | None = None,   # OpenAI-compatible client
        embedding_client: Any | None = None,
    ) -> None:
        ...

    async def upsert_entity(
        self, org_id: str, entity: EntityNode
    ) -> str:
        """Create or update an EntityNode. Returns the node uuid.

        Idempotent: if uuid already exists, updates properties.
        Always filters by org_id to prevent cross-tenant access.

        Edge cases:
        - Empty name: raise ValueError("Entity name is required")
        - Duplicate name within org: update existing node (idempotent)
        - Existing uuid in different org: treated as different entity (org_id scoped)
        """
        ...

    async def upsert_episode(
        self, org_id: str, episode: EpisodicNode
    ) -> str:
        """Create or update an EpisodicNode. Returns the node uuid."""
        ...

    async def upsert_community(
        self, org_id: str, community: CommunityNode
    ) -> str:
        """Create or update a CommunityNode. Returns the node uuid."""
        ...

    async def create_relates_to(
        self, org_id: str, edge: RelatesToEdge,
        source_uuid: str, target_uuid: str
    ) -> str:
        """Create a RELATES_TO edge between two EntityNodes.

        Validates both entities exist and belong to org_id.
        Uses edge.uuid for idempotency — re-running with same uuid is a no-op.

        Temporal logic:
        - If valid_to is set and < now(), does NOT create the edge (fact expired)
        - If invalid_at is set, creates edge with invalid_at (fact was true, now retracted)
        """
        ...

    async def create_has_episode(
        self, org_id: str,
        entity_uuid: str, episode_uuid: str,
        created_at: str | None = None
    ) -> None:
        """Link an entity to an episode. Creates HAS_EPISODE edge."""
        ...

    async def create_member_of(
        self, org_id: str,
        entity_uuid: str, community_uuid: str
    ) -> None:
        """Add entity to a community. Creates MEMBER_OF edge."""
        ...

    async def get_entity(
        self, org_id: str, uuid: str
    ) -> EntityNode | None:
        """Get an entity by UUID. Returns None if not found or belongs to different org."""
        ...

    async def get_entity_edges(
        self, org_id: str, uuid: str,
        max_depth: int = 2
    ) -> list[dict]:
        """Get all edges for an entity, optionally BFS traversal up to max_depth.

        Used by context assembly (CTX-05) and graph query endpoints (KG-10).
        Returns list of {relationship_type, source, target, properties} dicts.
        """
        ...

    async def get_entity_by_name(
        self, org_id: str, name: str, entity_type: str | None = None
    ) -> EntityNode | None:
        """Find an entity by name within an org. Optionally filter by type.

        Names are NOT unique within an org — may return the most recent match.
        """
        ...

    async def list_entities(
        self, org_id: str,
        entity_type: str | None = None,
        limit: int = 50,
        offset: int = 0
    ) -> list[EntityNode]:
        """List entities for an org with optional type filter and pagination."""
        ...

    async def delete_entity(
        self, org_id: str, uuid: str
    ) -> bool:
        """Delete an entity and all its edges. Returns True if deleted, False if not found.

        Cascade behavior:
        - Deletes all RELATES_TO edges (incoming and outgoing)
        - Deletes all HAS_EPISODE edges
        - Deletes all MEMBER_OF edges
        - Does NOT delete EpisodicNode or CommunityNode
        """
        ...

    async def bfs_traverse(
        self, org_id: str, start_uuid: str,
        max_depth: int = 2,
        edge_types: list[str] | None = None
    ) -> list[dict]:
        """BFS traversal from a starting node.

        Used by context assembly (CTX-05, CTX-05) and graph queries.

        Returns list of path dicts:
        [{
            "nodes": [{"uuid": "...", "type": "EntityNode", "name": "..."}, ...],
            "edges": [{"type": "RELATES_TO", "predicate": "...", ...}, ...],
            "depth": 1
        }, ...]

        Performance guard:
        - max_depth defaults to 2 (configurable, hard cap at 5) — prevents
          unbounded traversals (OQ-05 mitigation)
        - edge_types filter limits traversal to relevant edges
        - Results are capped at 1000 nodes (configurable)
        """
        ...

    async def temporal_query(
        self, org_id: str, user_id: str,
        at_time: str | None = None,
        confidence_min: float = 0.0
    ) -> list[dict]:
        """Find all facts (RELATES_TO edges) valid at a given time for a user.

        This is the core temporal query (KG-07).

        If at_time is None, uses current time.
        Filters: valid_from <= at_time AND (valid_to IS NULL OR valid_to >= at_time)

        Returns list of {subject, predicate, fact, object, confidence,
                         valid_from, valid_to, source_episode_id}
        """
        ...
```

### 3.2 Business Logic Flow — Entity Extraction Worker

```
INPUT: episode_id, org_id, user_id

1. Fetch episode content from PostgreSQL (repositories.episodes.get_by_id)
2. Call LLM with entity extraction prompt (prompts/extract_entities_v1.jinja2)
3. Parse LLM response:
   - entities: [{"name": "...", "type": "...", "summary": ""}]
   - relationships: [{"source": "...", "target": "...", "predicate": "...", "fact": "..."}]
4. For each entity:
   a. Check existence: get_entity_by_name(org_id, name)
   b. If not exists: upsert_entity(org_id, EntityNode(...))
   c. If exists: update summary, add source_episode_id if missing
5. For each relationship:
   a. Resolve source and target to UUIDs (by name lookup)
   b. create_relates_to(org_id, RelatesToEdge(...), source_uuid, target_uuid)
6. For each entity:
   a. create_has_episode(org_id, entity_uuid, episode_graphiti_uuid)
7. Enqueue embed_entity task for each created/updated entity
8. Return list of created entity UUIDs
```

### 3.3 Error Handling Matrix

| Error | Graph State | Service Action |
|-------|------------|----------------|
| Entity creation fails (connection error) | No node created | Retry with exponential backoff (max 3) |
| Entity creation succeeds, relationship fails | Orphaned entity | Periodic cleanup job (or compensate in worker) |
| Edge deletion fails mid-cascade | Partial deletion | Retry entire deletion as atomic unit |
| BFS traversal timeout (> 5s) | Partial results | Return results found so far + warning header |
| org_id mismatch in query | Query returns empty | Return 403 (not 404 — don't reveal existence) |
| Duplicate uuid on create | Check fails | Retry with new uuid (generate again) |

---

## 4. Implementation Notes

### 4.1 Backend-Specific Schema Considerations

#### FalkorDB

| Aspect | Detail |
|--------|--------|
| **Connection** | Redis-protocol on port 6380 (default). Uses `redis-py` under Graphiti. |
| **Graph name** | Use org_id as Redis key namespace: `OpenZep:{org_id}`. Each org gets a logical graph. |
| **Indexes** | FalkorDB v1.4+ supports `GRAPH.INDEX ADD` for node properties. Index `org_id` and `uuid` on all node types. |
| **Limitations** | No native UNIQUE constraint. No composite indexes. No schema validation. |
| **Persistence** | Configure FalkorDB with append-only file (AOF) for durability. Default is in-memory only. |
| **Max nodes** | Unlimited (backed by RAM). Budget: 10GB RAM per 10M nodes. |

**FalkorDB index creation:**
```cypher
GRAPH.INDEX ADD :EntityNode(uuid)  -- Not supported in all versions; fall back to app-level check
GRAPH.INDEX ADD :EntityNode(org_id)
GRAPH.INDEX ADD :EpisodicNode(org_id)
GRAPH.INDEX ADD :CommunityNode(org_id)
```

#### Neo4j

| Aspect | Detail |
|--------|--------|
| **Connection** | Bolt protocol on port 7687 (default). Uses `neo4j` Python driver. |
| **Graph name** | Single database, nodes tagged with `org_id` property. Use `org_id` index for isolation. |
| **Indexes** | Full schema index support: uniqueness, composite, full-text. |
| **Constraints** | `CREATE CONSTRAINT ... FOR (n:EntityNode) REQUIRE n.uuid IS UNIQUE` |
| **Persistence** | Native — on-disk by default. |
| **Limitations** | Heavier deployment vs FalkorDB. Higher memory overhead per node. |

### 4.2 org_id Enforcement (MT-03, SEC-03)

Every node and every query MUST include `org_id`. The GraphitiClient wrapper enforces this:

```python
async def _assert_org_access(self, graph_uuid: str, org_id: str) -> None:
    """Verify a node belongs to the given org. Called before every read/write.

    This is the graph-level equivalent of the PostgreSQL tenant isolation filter.
    """
    # During development, log a warning if org_id is missing
    if not org_id:
        raise ValueError("org_id is required for all graph operations")
```

**Graph namespace strategy (FalkorDB):**
```python
def _graph_name(self, org_id: str) -> str:
    """Return the FalkorDB graph name for this org.

    Each org gets a separate graph key in Redis.
    This provides physical isolation alongside logical isolation.
    """
    return f"OpenZep:{org_id}"
```

### 4.3 Temporal Graph Operations

**Adding a new fact (edge):**
```python
async def add_fact(
    self, org_id: str,
    fact: RelatesToEdge,
    source_entity_uuid: str,
    target_entity_uuid: str,
) -> str:
    """Add a temporal fact as a RELATES_TO edge.

    If an existing RELATES_TO edge exists between the same entities
    with the same predicate, the old edge's valid_to is set to now()
    (marking it as no longer valid), and a new edge is created.

    This implements bi-temporal semantics:
    - valid_from: when this fact became true in the real world (valid time)
    - created_at: when this fact was recorded in the system (transaction time)
    - invalid_at: when this fact was retracted by the system
    """
    # 1. Find existing active edge with same predicate
    existing = await self._find_active_edge(
        org_id, source_entity_uuid, target_entity_uuid, fact.predicate
    )
    # 2. If exists, expire it
    if existing:
        await self._expire_edge(org_id, existing.uuid)
    # 3. Create new edge
    return await self.create_relates_to(org_id, fact, source_entity_uuid, target_entity_uuid)
```

**Querying facts valid at time T (temporal query):**
```
FalkorDB lacks date-aware filtering in GQL — temporal filtering must be done
in application code after fetching edges, OR by storing timestamps as numeric
epochs for comparison.

Alternative: Use the `facts` PostgreSQL table for temporal queries and the
graph for entity traversal. This hybrid approach is preferred for production.

See: temporal_query() in GraphitiClient — it reads from PostgreSQL `facts`
table and uses the graph for entity traversal only.
```

### 4.4 Performance Notes

| Query Pattern | Expected Latency | Optimization |
|--------------|-----------------|-------------|
| Single entity lookup by UUID | < 5ms | org_id + uuid index |
| BFS traversal depth 2, 100 nodes | < 50ms | Limit branching factor; cap at 1000 nodes |
| BFS traversal depth 5, 1000 nodes | < 500ms | Add edge-type filter; reduce depth |
| Entity list (type filter, offset 50) | < 20ms | org_id + entity_type index |
| Community membership lookup | < 10ms | org_id + MEMBER_OF count |

### 4.5 Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GRAPH_BACKEND` | `falkordb` | `falkordb` or `neo4j` |
| `FALKORDB_URL` | `redis://localhost:6380` | FalkorDB connection string |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | — | Neo4j password |
| `GRAPH_BFS_MAX_DEPTH` | `2` | Maximum BFS traversal depth (hard cap: 5) |
| `GRAPH_BFS_MAX_NODES` | `1000` | Maximum nodes to return from BFS |
| `GRAPH_UUID_RETRIES` | `3` | Number of uuid collision retries |

### 4.6 Operational Notes

- **FalkorDB memory budget:** FalkorDB is in-memory. Estimate: 500 bytes per node + 300 bytes per edge. For 100K nodes + 500K edges ≈ 200MB. Monitor with `INFO MEMORY`.
- **Neo4j heap:** Neo4j JVM heap should be 4-8GB for production. Configure via `NEO4J_dbms_memory_heap_max__size`.
- **Backup:** FalkorDB supports `SAVE` (snapshot) and BGSAVE. Schedule periodic saves. Neo4j has native `neo4j-admin dump`.
- **Connection pooling:** FalkorDB uses Redis connection pool (max 10 connections). Neo4j uses Bolt connection pool (max 10 sessions).

---

## 5. Testing Guidance

### 5.1 Unit Tests

| Test | What to Cover |
|------|--------------|
| EntityNode creation | Default uuid generation, all fields set correctly |
| org_id validation | Missing org_id raises ValueError |
| Edge type validation | RELATES_TO, HAS_EPISODE, MEMBER_OF type strings validated |
| Temporal window logic | valid_from/valid_to comparison, NULL handling |
| uuid uniqueness check | Same uuid rejected (mock graph query) |
| BFS depth cap | max_depth > 5 raises ValueError |

### 5.2 Integration Tests (real FalkorDB / Neo4j via testcontainers)

| Test | What to Cover |
|------|--------------|
| Create entity node | Node exists with all properties |
| Create RELATES_TO edge | Edge exists with temporal properties |
| Tenant isolation | org_id="org_a" cannot query org_id="org_b" nodes |
| Entity node deletion | Cascade removes all edges |
| BFS traversal depth 1 | Returns direct neighbors only |
| BFS traversal depth 2 | Returns neighbors of neighbors |
| Temporal query at time T | Returns only edges valid at T |
| Duplicate node with same uuid | Correctly rejected at app layer |
| Large graph traversal | 1000 nodes depth 2 completes under 200ms |
| HOTPATH: entity by name | Multiple entities with same name — returns correct one |

### 5.3 Edge Cases

1. **Cyclic graphs:** BFS traversal must track visited nodes (UUID set) to prevent infinite loops. Test with A→B→C→A cycle.
2. **Self-referencing entity:** Entity with RELATES_TO edge to itself. Ensure traversal handles it.
3. **Concurrent edge creation:** Two workers creating the same RELATES_TO edge between same entities. The uuid-based idempotency check prevents duplicates — verify with concurrent test.
4. **Entity with no edges:** BFS traversal from an isolated entity returns only the node itself.
5. **Temporal edge with valid_to in the past:** Should be excluded from "current facts" queries but preserved for historical queries.
6. **Empty org (no nodes):** List entities returns empty list, BFS from nonexistent uuid returns empty.
7. **Very long entity name:** > 1000 chars. Should be truncated at the application layer before storage.
8. **Special characters in UUIDs:** Ensure all UUIDs are validated as valid UUID format before graph operations.

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*

**SRS traceability:** This document implements graph data model requirements from SRS §7.2 (Graphiti Graph Schema), all knowledge graph requirements (KG-01–KG-13), multi-tenancy graph isolation (MT-01, MT-03), and temporal query patterns (KG-07). The RECOMMENDED decision for HAS_EPISODE over episodes array is documented with full tradeoff analysis.
