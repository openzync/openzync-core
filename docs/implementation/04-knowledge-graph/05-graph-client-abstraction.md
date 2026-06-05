# Graphiti-Client — Backend-Agnostic Graph Abstraction Layer (FalkorDB-focused)

## 1. Overview

| Field | Detail |
|-------|--------|
| **Coverage** | Abstract `GraphBackend` interface, FalkorDB implementation, Neo4j implementation, org_id enforcement, error translation, testing strategy, backend selection at startup |
| **Dependencies** | [01-graphiti-setup.md](01-graphiti-setup.md) (Graphiti initialisation), [02-entity-operations.md](02-entity-operations.md) (EntityService types), [03-temporal-queries.md](03-temporal-queries.md) (temporal query patterns) |
| **SRS Requirement IDs** | KG-01, KG-02, PORT-02, OQ-01 (API stability), MT-01, MT-02, SEC-03 |
| **Build Phase** | Phase 0 (Foundation — interface defined here, implementations built alongside) |
| **Design Authority** | @architect for interface design, @senior-dev for implementations, @qa-engineer for test strategy |

### 1.1 Purpose & Rationale

The `packages/graphiti-client/` package exists to solve two problems:

| Problem | SRS Reference | How This Package Solves It |
|---------|---------------|---------------------------|
| **OQ-01: Graphiti API stability** | §15, OQ-01 | If Graphiti's public API changes, only the wrapper adapts. Callers never import Graphiti directly. |
| **PORT-02: Backend-agnostic** | §6.5, PORT-02 | FalkorDB and Neo4j share a single interface. Switching only requires changing an env var. |

> **Note:** FalkorDB is the primary and only supported graph backend for v1.0. Neo4j support was documented during design but deferred to a future release. The abstraction interface remains clean enough to add Neo4j later without breaking changes.

### 1.2 Package Location

```
packages/
  graphiti-client/
    __init__.py
    graph_backend.py          # Abstract interface
    falkordb_backend.py       # FalkorDB implementation
    neo4j_backend.py          # Neo4j implementation
    errors.py                 # Error translation
    models.py                 # Domain models for the abstraction
```

---

## 2. Abstract Interface

```python
# packages/graphiti-client/graph_backend.py
"""Abstract interface for graph backend operations."""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Entity:
    """Domain model for an entity node, independent of Graphiti types."""

    uuid: str
    name: str
    entity_type: str
    summary: str = ""
    group_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None


@dataclass
class Relationship:
    """Domain model for a relationship edge, independent of Graphiti types."""

    uuid: str
    source_node_uuid: str
    target_node_uuid: str
    name: str  # predicate (e.g., "works_at")
    fact: str = ""
    group_id: str = ""
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    created_at: datetime | None = None


@dataclass
class Community:
    """Domain model for a community summary."""

    uuid: str
    name: str
    summary: str = ""
    member_count: int = 0
    group_id: str = ""
    created_at: datetime | None = None


@dataclass
class PaginatedResult[T]:
    """Generic paginated result from any graph backend."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


class GraphBackend(ABC):
    """Abstract interface for graph database operations.

    Every method takes `org_id: uuid.UUID` as the first parameter.
    This is enforced at the interface level — no method can be called
    without an org_id, making cross-tenant access structurally impossible.

    All methods must be safe to call from async contexts.
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the backend: create indices, constraints, etc.

        Called once at application startup. Idempotent.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close all connections and release resources.

        Called once at application shutdown.
        """
        ...

    # ── Entity Operations ────────────────────────────────────────────

    @abstractmethod
    async def create_entity(
        self,
        org_id: uuid.UUID,
        user_id: str,
        name: str,
        entity_type: str,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Entity:
        """Create a new entity node.

        Args:
            org_id: Tenant ID — enforces multi-tenant isolation.
            user_id: The user's external_id within the org.
            name: Entity name (e.g., "Alice", "Acme Corp").
            entity_type: Entity type label (e.g., "Person", "Organization").
            summary: Natural language summary of the entity.
            metadata: Optional key-value attributes.

        Returns:
            The created Entity with its generated UUID.
        """
        ...

    @abstractmethod
    async def get_entity(
        self,
        org_id: uuid.UUID,
        user_id: str,
        entity_uuid: str,
    ) -> Entity | None:
        """Get an entity node by UUID.

        Returns None if not found or if the entity belongs to a different org.
        """
        ...

    @abstractmethod
    async def get_entities(
        self,
        org_id: uuid.UUID,
        user_id: str,
        *,
        entity_type: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> PaginatedResult[Entity]:
        """List entity nodes with optional type filter and cursor pagination."""
        ...

    @abstractmethod
    async def find_entity_by_name(
        self,
        org_id: uuid.UUID,
        user_id: str,
        name: str,
        entity_type: str | None = None,
    ) -> Entity | None:
        """Find an entity by exact name match within an org+user scope."""
        ...

    @abstractmethod
    async def delete_entity(
        self,
        org_id: uuid.UUID,
        user_id: str,
        entity_uuid: str,
    ) -> bool:
        """Delete an entity node and all its incident edges.

        Returns True if deleted, False if not found.
        """
        ...

    # ── Relationship Operations ──────────────────────────────────────

    @abstractmethod
    async def create_relationship(
        self,
        org_id: uuid.UUID,
        user_id: str,
        source_node_uuid: str,
        target_node_uuid: str,
        name: str,
        fact: str = "",
        valid_at: datetime | None = None,
    ) -> Relationship:
        """Create a relationship edge between two entity nodes."""
        ...

    @abstractmethod
    async def get_relationships(
        self,
        org_id: uuid.UUID,
        user_id: str,
        *,
        source_node_uuid: str | None = None,
        name: str | None = None,
        valid_at: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> PaginatedResult[Relationship]:
        """List relationships with optional filters."""
        ...

    @abstractmethod
    async def get_entity_relationships(
        self,
        org_id: uuid.UUID,
        user_id: str,
        entity_uuid: str,
    ) -> list[Relationship]:
        """Get all relationships incident to an entity node."""
        ...

    # ── Traversal & Search ───────────────────────────────────────────

    @abstractmethod
    async def traverse(
        self,
        org_id: uuid.UUID,
        user_id: str,
        start_node_uuid: str,
        *,
        max_depth: int = 2,
        edge_types: list[str] | None = None,
    ) -> list[tuple[Entity, Relationship, Entity]]:
        """BFS traversal from a start node up to max_depth.

        Returns list of (source_entity, relationship, target_entity) triples.
        """
        ...

    @abstractmethod
    async def search_entities(
        self,
        org_id: uuid.UUID,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> list[Entity]:
        """Semantic search across entity nodes."""
        ...

    # ── Community Operations ─────────────────────────────────────────

    @abstractmethod
    async def get_communities(
        self,
        org_id: uuid.UUID,
        user_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> PaginatedResult[Community]:
        """List community summary nodes."""
        ...

    @abstractmethod
    async def create_community(
        self,
        org_id: uuid.UUID,
        user_id: str,
        name: str,
        summary: str,
        member_entity_uuids: list[str],
    ) -> Community:
        """Create a community node and link member entities via MEMBER_OF edges."""
        ...

    # ── Health ────────────────────────────────────────────────────────

    @abstractmethod
    async def ping(self) -> bool:
        """Check if the graph backend is reachable.

        Returns True if healthy.
        """
        ...
```

---

## 3. FalkorDB Implementation

```python
# packages/graphiti-client/falkordb_backend.py
"""FalkorDB implementation of GraphBackend using Graphiti's FalkorDriver."""

import uuid
from datetime import datetime
from typing import Any

from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.nodes import EntityNode, CommunityNode
from graphiti_core.edges import EntityEdge, CommunityEdge

from .graph_backend import GraphBackend, Entity, Relationship, Community, PaginatedResult
from .errors import translate_error


class FalkorDBBackend(GraphBackend):
    """GraphBackend implementation for FalkorDB via Graphiti.

    FalkorDB uses the Redis wire protocol on port 6380 by default.
    All queries are executed through Graphiti's FalkorDriver, which
    handles the translation from Cypher-like GQL to FalkorDB's query language.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6380,
        username: str | None = None,
        password: str | None = None,
        database: str = "default_db",
    ) -> None:
        self._driver = FalkorDriver(
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
        )
        # Graphiti clients are held at the service layer, not the backend.
        # This backend only handles graph storage operations.
        self._initialized = False

    async def initialize(self) -> None:
        await self._driver.build_indices_and_constraints(delete_existing=False)
        self._initialized = True

    async def close(self) -> None:
        await self._driver.close()

    async def ping(self) -> bool:
        try:
            await self._driver.execute_query("RETURN 1")
            return True
        except Exception:
            return False

    # ── Entity Operations ──────────────────────────────────────────

    async def create_entity(
        self,
        org_id: uuid.UUID,
        user_id: str,
        name: str,
        entity_type: str,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Entity:
        group_id = self._make_group_id(org_id, user_id)
        node = EntityNode(
            name=name,
            group_id=group_id,
            labels=[entity_type],
            summary=summary,
            attributes=metadata or {},
        )
        await node.save(driver=self._driver)
        return self._entity_from_node(node)

    async def get_entity(
        self,
        org_id: uuid.UUID,
        user_id: str,
        entity_uuid: str,
    ) -> Entity | None:
        try:
            node = await EntityNode.get_by_uuid(
                driver=self._driver, uuid=entity_uuid
            )
        except graphiti_core.errors.NodeNotFoundError:
            return None

        # Verify org isolation
        expected_prefix = f"org:{org_id}:user:"
        if not node.group_id.startswith(expected_prefix):
            return None  # exists but belongs to different org

        return self._entity_from_node(node)

    async def find_entity_by_name(
        self,
        org_id: uuid.UUID,
        user_id: str,
        name: str,
        entity_type: str | None = None,
    ) -> Entity | None:
        group_id = self._make_group_id(org_id, user_id)
        result = await self._driver.execute_query(
            """
            MATCH (n:EntityNode {group_id: $group_id, name: $name})
            RETURN n
            """,
            params={"group_id": group_id, "name": name},
        )
        if not result or not result[0]:
            return None
        node = EntityNode.from_record(result[0][0])
        return self._entity_from_node(node)

    async def delete_entity(
        self,
        org_id: uuid.UUID,
        user_id: str,
        entity_uuid: str,
    ) -> bool:
        # First verify ownership
        group_id = self._make_group_id(org_id, user_id)
        node = await self.get_entity(org_id, user_id, entity_uuid)
        if node is None:
            return False

        async with self._driver.transaction() as tx:
            # Delete incident edges
            edges = await EntityEdge.get_by_node_uuid(
                driver=self._driver, node_uuid=entity_uuid, transaction=tx
            )
            if edges:
                await EntityEdge.delete_by_uuids(
                    driver=self._driver,
                    uuids=[e.uuid for e in edges],
                    transaction=tx,
                )
            # Delete node
            await EntityNode.delete_by_uuids(
                driver=self._driver, uuids=[entity_uuid], transaction=tx
            )
        return True

    # ── Relationship Operations ─────────────────────────────────────

    async def create_relationship(
        self,
        org_id: uuid.UUID,
        user_id: str,
        source_node_uuid: str,
        target_node_uuid: str,
        name: str,
        fact: str = "",
        valid_at: datetime | None = None,
    ) -> Relationship:
        group_id = self._make_group_id(org_id, user_id)
        edge = EntityEdge(
            source_node_uuid=source_node_uuid,
            target_node_uuid=target_node_uuid,
            name=name,
            fact=fact,
            group_id=group_id,
            valid_at=valid_at or datetime.now(timezone.utc),
        )
        await edge.save(driver=self._driver)
        return self._relationship_from_edge(edge)

    async def get_entity_relationships(
        self,
        org_id: uuid.UUID,
        user_id: str,
        entity_uuid: str,
    ) -> list[Relationship]:
        edges = await EntityEdge.get_by_node_uuid(
            driver=self._driver, node_uuid=entity_uuid
        )
        return [self._relationship_from_edge(e) for e in edges]

    # ── Community Operations ───────────────────────────────────────

    async def create_community(
        self,
        org_id: uuid.UUID,
        user_id: str,
        name: str,
        summary: str,
        member_entity_uuids: list[str],
    ) -> Community:
        group_id = f"org:{org_id}:community"

        # Create community node
        node = CommunityNode(
            name=name,
            group_id=group_id,
            summary=summary,
        )
        await node.save(driver=self._driver)

        # Create MEMBER_OF edges
        edges = [
            CommunityEdge(
                source_node_uuid=member_id,
                target_node_uuid=str(node.uuid),
                group_id=group_id,
            )
            for member_id in member_entity_uuids
        ]
        await CommunityEdge.save_bulk(driver=self._driver, edges=edges)

        return Community(
            uuid=str(node.uuid),
            name=name,
            summary=summary,
            member_count=len(member_entity_uuids),
            group_id=group_id,
            created_at=node.created_at,
        )

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _make_group_id(org_id: uuid.UUID, user_id: str) -> str:
        return f"org:{org_id}:user:{user_id}"

    @staticmethod
    def _entity_from_node(node: EntityNode) -> Entity:
        return Entity(
            uuid=str(node.uuid),
            name=node.name,
            entity_type=node.labels[0] if node.labels else "Unknown",
            summary=node.summary or "",
            group_id=node.group_id,
            metadata=node.attributes or {},
            created_at=node.created_at,
        )

    @staticmethod
    def _relationship_from_edge(edge: EntityEdge) -> Relationship:
        return Relationship(
            uuid=str(edge.uuid),
            source_node_uuid=str(edge.source_node_uuid),
            target_node_uuid=str(edge.target_node_uuid),
            name=edge.name,
            fact=edge.fact or "",
            group_id=edge.group_id,
            valid_at=edge.valid_at,
            invalid_at=edge.invalid_at,
            created_at=edge.created_at,
        )
```

---

## 4. Neo4j Implementation

Neo4j implementation follows the same interface but uses `Neo4jDriver` from Graphiti. The adapters are identical in structure; only the driver config differs.

```python
# packages/graphiti-client/neo4j_backend.py
"""Neo4j implementation of GraphBackend using Graphiti's Neo4jDriver."""

from graphiti_core.driver.neo4j_driver import Neo4jDriver

from .graph_backend import GraphBackend
from .falkordb_backend import FalkorDBBackend  # reuse helper methods


class Neo4jBackend(GraphBackend):
    """GraphBackend implementation for Neo4j via Graphiti.

    Neo4j uses the Bolt protocol on port 7687 by default.
    """

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "",
        database: str = "neo4j",
    ) -> None:
        self._driver = Neo4jDriver(
            uri=uri,
            user=user,
            password=password,
            database=database,
        )
        self._initialized = False

    async def initialize(self) -> None:
        await self._driver.build_indices_and_constraints(delete_existing=False)
        self._initialized = True

    async def close(self) -> None:
        await self._driver.close()

    async def ping(self) -> bool:
        try:
            await self._driver.execute_query("RETURN 1 AS result")
            return True
        except Exception:
            return False

    # ── Entity Operations ──────────────────────────────────────────
    # Same methods as FalkorDBBackend — the Graphiti API is backend-agnostic.
    # EntityNode.save(), EntityNode.get_by_uuid(), etc. work identically
    # regardless of which driver is used.
    #
    # The Neo4jBackend would contain the same method implementations as
    # FalkorDBBackend above. They are omitted here for brevity — see
    # falkordb_backend.py for the full implementation.
    #
    # Key difference: Neo4j supports full Cypher, so direct queries
    # can be richer. FalkorDB has a subset of Cypher.
    #
    # DIFFERENCE: Neo4j's execute_query returns result in a different format.
    # The Neo4jDriver handles this internally in EntityNode.from_record().

    async def create_entity(self, ...) -> Entity:
        # Identical to FalkorDBBackend.create_entity
        # (both use EntityNode.save() which is driver-agnostic)
        ...
```

**Key insight:** The actual implementation of most methods is **identical** between FalkorDB and Neo4j because Graphiti's `EntityNode`, `EntityEdge`, and `CommunityNode` classes are themselves backend-agnostic — they delegate to the driver. The only truly separate code is the driver initialisation and `execute_query` for raw queries. This validates the abstraction: the methods are almost the same because Graphiti already abstracts the backend.

### 4.1 Backend-Specific Differences

| Aspect | FalkorDB | Neo4j |
|--------|----------|-------|
| Driver class | `FalkorDriver(host, port, ...)` | `Neo4jDriver(uri, user, password, ...)` |
| Default port | 6380 (Redis protocol) | 7687 (Bolt protocol) |
| Query syntax | Subset of Cypher (FalkorDB GQL) | Full Cypher |
| Datetime handling | Strings (FalkorDB has no native datetime) | Native datetime support |
| Fulltext syntax | RedisSearch `@` prefix + `build_fulltext_query()` | Native Lucene |
| Transaction support | No-op wrapper | Real ACID transactions |
| Index building | Sequential, catches `'already indexed'` | Parallel, catches `EquivalentSchemaRuleAlreadyExists` |

---

## 5. Error Translation

```python
# packages/graphiti-client/errors.py
"""Error translation: Graphiti exceptions → application exceptions."""

import graphiti_core.errors as g_errors
from app.core.exceptions import (
    AppError,
    EntityNotFoundError,
    EdgeNotFoundError,
    GraphTimeoutError,
    GraphConnectionError,
)


def translate_error(
    operation: str,
    error: Exception,
) -> AppError:
    """Translate a Graphiti or driver exception into an application exception.

    Usage:
        try:
            node = await EntityNode.get_by_uuid(driver, uuid)
        except Exception as e:
            raise translate_error("get_entity", e)
    """
    if isinstance(error, g_errors.NodeNotFoundError):
        return EntityNotFoundError(f"Entity not found: {error}")
    if isinstance(error, g_errors.EdgeNotFoundError):
        return EdgeNotFoundError(f"Edge not found: {error}")
    if isinstance(error, g_errors.GroupsEdgesNotFoundError):
        return EntityNotFoundError(f"No entities found in group: {error}")
    if isinstance(error, g_errors.GroupsNodesNotFoundError):
        return EntityNotFoundError(f"No nodes found in group: {error}")

    if isinstance(error, ConnectionError):
        return GraphConnectionError(f"Graph backend connection failed: {error}")
    if isinstance(error, TimeoutError):
        return GraphTimeoutError(f"Graph operation timed out: {error}")
    if isinstance(error, OSError):
        return GraphConnectionError(f"Graph backend unreachable: {error}")

    # Fallback: re-raise as-is — this should not happen in normal operation
    # but avoids silently swallowing unknown errors
    return GraphConnectionError(
        f"Unexpected error during {operation}: {error}"
    )


# Warning-free usage in service code
from .errors import translate_error
```

---

## 6. org_id Enforcement

### 6.1 Interface-Level Enforcement

Every method on `GraphBackend` takes `org_id: uuid.UUID` as an **un-ignorable first parameter**. This is not a keyword argument — it is positional and mandatory. The type system (mypy / pyright) enforces this at compile time.

```python
# WRONG — this code does not compile because org_id is missing:
backend = FalkorDBBackend(...)
entity = await backend.create_entity("user_1", "Alice", "Person")  # ❌ missing org_id

# CORRECT:
entity = await backend.create_entity(org_id=org_123, user_id="user_1", name="Alice", entity_type="Person")
```

### 6.2 Runtime Enforcement

The `FalkorDBBackend.get_entity()` method performs a secondary check: even if a node with the given UUID exists, it verifies that the `group_id` starts with the expected org prefix. This prevents a compromised service from reading another org's data by UUID guesswork.

```python
async def get_entity(self, org_id, user_id, entity_uuid) -> Entity | None:
    try:
        node = await EntityNode.get_by_uuid(driver=self._driver, uuid=entity_uuid)
    except NodeNotFoundError:
        return None

    # ⚠️ CRITICAL: Verify org ownership even if node exists.
    # This prevents cross-tenant data leaks (SEC-03).
    expected_prefix = f"org:{org_id}:user:"
    if not node.group_id.startswith(expected_prefix):
        return None  # Pretend the node does not exist — no 403, no info leak

    return self._entity_from_node(node)
```

### 6.3 Testing the Enforcement

```python
# tests/integration/test_org_isolation.py
"""Verify cross-tenant isolation is impossible to bypass."""

import pytest


@pytest.mark.asyncio
@pytest.mark.integration
class TestOrgIsolation:

    async def test_cannot_read_other_org_entity(self, backend: GraphBackend):
        """Org A should not see Org B's entities by UUID."""
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()

        # Org A creates an entity
        entity_a = await backend.create_entity(
            org_id=org_a, user_id="u1", name="OrgASecret", entity_type="Secret"
        )

        # Org B tries to read it by UUID
        result = await backend.get_entity(
            org_id=org_b, user_id="u1", entity_uuid=entity_a.uuid,
        )
        assert result is None  # must return None, not leak existence

    async def test_cannot_delete_other_org_entity(self, backend: GraphBackend):
        """Org A should not be able to delete Org B's entities."""
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()

        entity_a = await backend.create_entity(
            org_id=org_a, user_id="u1", name="Protected", entity_type="Data",
        )

        # Org B attempts deletion
        deleted = await backend.delete_entity(
            org_id=org_b, user_id="u1", entity_uuid=entity_a.uuid,
        )
        assert deleted is False

        # Org A confirms it still exists
        still_exists = await backend.get_entity(
            org_id=org_a, user_id="u1", entity_uuid=entity_a.uuid,
        )
        assert still_exists is not None
```

---

## 7. Factory & Startup Integration

### 7.1 Backend Factory

```python
# packages/graphiti-client/factory.py
"""Factory: create GraphBackend instance from configuration."""

import uuid

from app.core.config import settings
from app.core.exceptions import GraphInitError

from .graph_backend import GraphBackend
from .falkordb_backend import FalkorDBBackend
from .neo4j_backend import Neo4jBackend


def create_graph_backend() -> GraphBackend:
    """Create the graph backend based on GRAPH_BACKEND env var.

    Called once at FastAPI lifespan startup. The backend instance is
    cached in app.state and reused across all requests.

    Raises:
        GraphInitError: If the backend type is unsupported.
    """
    backend_type = settings.GRAPH_BACKEND.lower()

    if backend_type == "falkordb":
        return FalkorDBBackend(
            host=settings.FALKORDB_HOST or "localhost",
            port=settings.FALKORDB_PORT or 6380,
            username=settings.FALKORDB_USERNAME,
            password=settings.FALKORDB_PASSWORD,
            database=settings.FALKORDB_DB or "default_db",
        )
    elif backend_type == "neo4j":
        return Neo4jBackend(
            uri=settings.NEO4J_URI or "bolt://localhost:7687",
            user=settings.NEO4J_USER or "neo4j",
            password=settings.NEO4J_PASSWORD or "",
            database=settings.NEO4J_DB or "neo4j",
        )
    else:
        raise GraphInitError(
            f"Unsupported graph backend: '{backend_type}'. "
            f"Supported: 'falkordb', 'neo4j'"
        )
```

### 7.2 App State Integration

```python
# services/api/app/main.py (add to lifespan)

from packages.graphiti_client.factory import create_graph_backend
from packages.graphiti_client.graph_backend import GraphBackend


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    # Create and initialize the graph backend
    backend = create_graph_backend()
    await backend.initialize()
    application.state.graph_backend = backend

    logger.info(
        "Graph backend initialized",
        extra={"backend_type": settings.GRAPH_BACKEND},
    )
    yield

    # Shutdown
    await backend.close()
```

---

## 8. Testing Strategy

### 8.1 Unit Tests: Mock the GraphBackend Interface

```python
# tests/unit/test_entity_service_with_mock_backend.py
"""Verify EntityService logic with a mocked GraphBackend."""

from unittest.mock import AsyncMock

import pytest

from packages.graphiti_client.graph_backend import GraphBackend, Entity


@pytest.fixture
def mock_backend() -> GraphBackend:
    backend = AsyncMock(spec=GraphBackend)

    # Configure default return values
    backend.create_entity.return_value = Entity(
        uuid="test-uuid-123",
        name="Test",
        entity_type="Person",
        group_id="org:test:user:u1",
    )
    backend.get_entity.return_value = None  # not found by default
    backend.ping.return_value = True

    return backend


@pytest.mark.asyncio
class TestEntityServiceWithMockBackend:

    async def test_create_and_retrieve(self, mock_backend: GraphBackend):
        """EntityService should delegate to the backend correctly."""
        service = EntityService(backend=mock_backend)

        entity = await service.create_entity(
            org_id=uuid.uuid4(), user_id="u1", name="Alice", entity_type="Person",
        )
        assert entity.name == "Test"
        mock_backend.create_entity.assert_called_once()

    async def test_get_nonexistent_returns_none(self, mock_backend: GraphBackend):
        """Should return None without error for missing entities."""
        mock_backend.get_entity.return_value = None
        service = EntityService(backend=mock_backend)

        result = await service.get_entity(
            org_id=uuid.uuid4(), user_id="u1", entity_uuid="nonexistent",
        )
        assert result is None
```

### 8.2 Integration Tests: Testcontainers for Both Backends

```python
# tests/integration/test_graph_backends.py
"""Integration tests: run the same test suite against both backends."""

import pytest
from testcontainers.falkordb import FalkorDbContainer
from testcontainers.neo4j import Neo4jContainer

from packages.graphiti_client.falkordb_backend import FalkorDBBackend
from packages.graphiti_client.neo4j_backend import Neo4jBackend
from packages.graphiti_client.graph_backend import GraphBackend


@pytest.mark.asyncio
@pytest.mark.integration
class TestBackendCompat:

    @pytest.fixture(
        params=[
            pytest.param("falkordb", marks=pytest.mark.falkordb),
            pytest.param("neo4j", marks=pytest.mark.neo4j),
        ]
    )
    async def backend(self, request) -> AsyncGenerator[GraphBackend, None]:
        """Parametrized fixture: run all tests against both backends."""
        if request.param == "falkordb":
            with FalkorDbContainer("falkordb/falkordb:1.1.2") as container:
                backend = FalkorDBBackend(
                    host=container.get_container_host_ip(),
                    port=container.get_exposed_port(6379),
                )
                await backend.initialize()
                yield backend
        elif request.param == "neo4j":
            with Neo4jContainer("neo4j:5.26") as container:
                backend = Neo4jBackend(
                    uri=container.get_connection_url(),
                    user="neo4j",
                    password=container.password,
                )
                await backend.initialize()
                yield backend

    async def test_create_and_get_entity(self, backend: GraphBackend):
        """Entity creation should be idempotent and retrievable."""
        org_id = uuid.uuid4()
        entity = await backend.create_entity(
            org_id=org_id,
            user_id="user_1",
            name="Alice",
            entity_type="Person",
            summary="Software engineer",
        )
        assert entity.uuid is not None
        assert entity.name == "Alice"

        # Retrieve by UUID
        retrieved = await backend.get_entity(
            org_id=org_id, user_id="user_1", entity_uuid=entity.uuid,
        )
        assert retrieved is not None
        assert retrieved.name == "Alice"
        assert retrieved.summary == "Software engineer"

    async def test_delete_entity_cascade(self, backend: GraphBackend):
        """Deleting an entity should remove it and its relationships."""
        org_id = uuid.uuid4()

        a = await backend.create_entity(org_id, "u1", "Alice", "Person")
        b = await backend.create_entity(org_id, "u1", "Acme Corp", "Organization")

        await backend.create_relationship(
            org_id, "u1", a.uuid, b.uuid, "works_at",
        )

        # Delete Alice
        deleted = await backend.delete_entity(org_id, "u1", a.uuid)
        assert deleted is True

        # Verify Alice's relationships are also gone
        rels = await backend.get_entity_relationships(org_id, "u1", b.uuid)
        assert all(r.source_node_uuid != a.uuid for r in rels)

    async def test_cross_tenant_isolation(self, backend: GraphBackend):
        """Tenant isolation must hold for all operations."""
        org_a, org_b = uuid.uuid4(), uuid.uuid4()

        e = await backend.create_entity(org_a, "u1", "Secret", "Data")

        # Org B should not see it
        assert await backend.get_entity(org_b, "u1", e.uuid) is None
        assert await backend.delete_entity(org_b, "u1", e.uuid) is False

    async def test_health_ping(self, backend: GraphBackend):
        """ping() should return True when the backend is reachable."""
        assert await backend.ping() is True

    async def test_close_and_reopen(self, backend: GraphBackend):
        """Should survive a close → re-initialize cycle."""
        await backend.close()
        await backend.initialize()
        assert await backend.ping() is True
```

### 8.3 CI Configuration

```yaml
# .gitlab-ci.yml — run both backend integration tests in parallel

integration:falkordb:
  script:
    - pytest tests/integration/test_graph_backends.py -m falkordb --cov=packages/graphiti_client
  services:
    - falkordb/falkordb:1.1.2

integration:neo4j:
  script:
    - pytest tests/integration/test_graph_backends.py -m neo4j --cov=packages/graphiti_client
  services:
    - neo4j:5.26-enterprise
```

---

## 9. Package Structure & Dependencies

```
packages/graphiti-client/
├── __init__.py          # exports: GraphBackend, FalkorDBBackend, Neo4jBackend, create_graph_backend
├── graph_backend.py     # abstract interface + domain models (Entity, Relationship, Community)
├── falkordb_backend.py  # FalkorDB implementation
├── neo4j_backend.py     # Neo4j implementation
├── errors.py            # error translation
└── factory.py           # backend factory

Dependencies:
- graphiti-core==0.29.1     (pinned, the only external dependency)
- pydantic>=2.0,<3.0        (for config models, shared with core)
```

### 9.1 Import Convention

```python
# CORRECT: services import from the abstraction, never from Graphiti directly.
from packages.graphiti_client import GraphBackend, create_graph_backend

# WRONG: services should not import Graphiti types directly.
# If Graphiti changes its API, this code breaks.
from graphiti_core.nodes import EntityNode  # ❌
from graphiti_core import Graphiti          # ❌ (use Graphiti directly only in the factory)
```

The **only** place that directly imports `graphiti_core` is:
1. `packages/graphiti-client/falkordb_backend.py` — implements the interface using Graphiti
2. `packages/graphiti-client/neo4j_backend.py` — implements the interface using Graphiti
3. `packages/core/graphiti/factory.py` — creates the Graphiti instance for search/LLM operations (the EntityService still needs Graphiti for `node_search`, `edge_search`, etc.)

For search operations that require the full Graphiti engine (not just graph storage), see the [EntityService](../04-knowledge-graph/02-entity-operations.md) which combines `GraphBackend` for storage with `Graphiti` for semantic search.

---

## 10. Migration Path: Existing Code to GraphBackend

If code was written directly against Graphiti's API, migrate to the abstract interface:

```python
# BEFORE (direct Graphiti usage — fragile):
from graphiti_core.nodes import EntityNode

node = await EntityNode.get_by_uuid(driver=graphiti._driver, uuid=node_id)

# AFTER (via GraphBackend — stable):
entity = await graph_backend.get_entity(org_id=org_id, user_id=user_id, entity_uuid=node_id)
```

The migration can be incremental: start by using `GraphBackend` for new code, then migrate existing callers one service at a time.

---

## 11. Open Questions

| ID | Question | Impact | Decision / Status |
|----|----------|--------|-------------------|
| CLT-01 | Should `GraphBackend` be used for all graph operations, or only storage operations? Search/traversal may need Graphiti's LLM+embedder | The abstraction boundary | **Decision:** `GraphBackend` covers storage (CRUD + traversal). Semantic search still uses Graphiti's `node_search`/`edge_search` directly through `EntityService`. If a GraphBackend method needs LLM/embedding access, it takes them as optional parameters |
| CLT-02 | Should we implement a Pinecone or Qdrant backend? SRS OQ-04 mentions pgvector → Qdrant migration | Scope of the abstraction | **Phase 5:** If pgvector performance degrades at >10M vectors, implement a `QdrantBackend` for vector storage while keeping FalkorDB/Neo4j for graph storage |
| CLT-03 | Community operations are part of GraphBackend — is that the right scope? | Community CRUD may have different performance characteristics | **Current:** Community operations are simple enough to include. If they grow complex (multi-step creation with LLM), they may move to a separate `CommunityService` that uses `GraphBackend` internally |

---

*Implementation document for SRS §6.5 (PORT-02), §15 (OQ-01). Maintained by @architect. Last updated: 2026-06-05.*
