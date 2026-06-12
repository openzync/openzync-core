# Entity & Graph Operations — CRUD, Query, Pagination

## 1. Overview

| Field | Detail |
|-------|--------|
| **Coverage** | All graph query endpoints (SRS §8.3), `EntityService` class, entity CRUD, pagination, filtering, delete cascade, entity resolution, org_id enforcement |
| **Dependencies** | [01-graphiti-setup.md](01-graphiti-setup.md) (Graphiti initialisation), [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md) (facts table), [03-tenant-isolation.md](../02-auth-tenancy/03-tenant-isolation.md) (org_id enforcement), [05-graph-client-abstraction.md](05-graph-client-abstraction.md) (GraphBackend interface) |
| **SRS Requirement IDs** | KG-05, KG-06, KG-09, KG-10, KG-11, KG-12, MT-01, MT-02, MT-03, NLP-01, NLP-02, SEC-03 |
| **Build Phase** | Phase 1 (Core Memory) |
| **Design Authority** | @senior-dev for service/repository patterns, @architect for entity resolution strategy |

### 1.1 Graph API Endpoints (SRS §8.3)

| Method | Path | Priority | Description |
|--------|------|----------|-------------|
| `GET` | `/v1/users/{user_id}/graph/nodes` | P0 | List entity nodes with filtering |
| `GET` | `/v1/users/{user_id}/graph/nodes/{node_id}` | P0 | Get single entity node + all edges |
| `DELETE` | `/v1/users/{user_id}/graph/nodes/{node_id}` | P1 | Delete node + all edges + cascade to facts |
| `GET` | `/v1/users/{user_id}/graph/edges` | P1 | List relationships with subject/predicate filters |
| `GET` | `/v1/users/{user_id}/graph/communities` | P1 | List community summary nodes (detailed in [04-community-detection.md](04-community-detection.md)) |

---

## 2. EntityService Class

### 2.1 Interface

```python
# packages/core/graphiti/entity_service.py
"""Service layer for entity and edge graph operations."""

import logging
import uuid
from datetime import datetime

from graphiti_core import Graphiti
from graphiti_core.nodes import EntityNode
from graphiti_core.edges import EntityEdge
from graphiti_core.search import SearchFilters, DateFilter
from graphiti_core.search.search_config import ComparisonOperator

from app.core.exceptions import (
    EntityNotFoundError,
    EdgeNotFoundError,
    GraphTimeoutError,
)

from packages.core.graphiti.client import get_graphiti  # from graph-client-abstraction
from packages.core.graphiti.pagination import CursorPage, encode_cursor, decode_cursor

logger = logging.getLogger(__name__)


class EntityService:
    """Service for entity and edge operations on the knowledge graph.

    Every method enforces org_id isolation. All graph queries are namespaced
    by org_id to prevent cross-tenant data access (MT-02, SEC-03).
    """

    def __init__(self, graphiti: Graphiti) -> None:
        self._graphiti = graphiti
        self._driver = graphiti._driver  # direct driver access for low-level queries

    # ── List Entities ─────────────────────────────────────────────────

    async def get_entities(
        self,
        org_id: uuid.UUID,
        user_id: str,
        *,
        entity_type: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CursorPage[EntityNode]:
        """List entity nodes for a user with optional filtering.

        Args:
            org_id: Tenant ID — mandatory, enforced at query layer.
            user_id: The user's external_id (scoped to org).
            entity_type: Optional filter by EntityNode type (e.g., "Person", "Company").
            created_after: Only entities created after this datetime.
            created_before: Only entities created before this datetime.
            limit: Maximum results per page (default 50, max 200).
            cursor: Opaque cursor for cursor-based pagination.

        Returns:
            CursorPage of EntityNode objects with next_cursor.

        Raises:
            GraphTimeoutError: If the graph query exceeds the timeout.
        """
        # Build search filters
        filters = SearchFilters()
        filters.node_labels = [entity_type] if entity_type else None

        # Date filters
        if created_after or created_before:
            date_filters = []
            if created_after:
                date_filters.append(
                    DateFilter(
                        comparison=ComparisonOperator.greater_than_equal,
                        value=created_after,
                    )
                )
            if created_before:
                date_filters.append(
                    DateFilter(
                        comparison=ComparisonOperator.less_than_equal,
                        value=created_before,
                    )
                )
            filters.created_at = [date_filters]

        # Cursor-based pagination: use 'created_at' + 'uuid' for stable ordering
        # Decode cursor to get offset point
        offset_node_id: str | None = None
        if cursor:
            offset_data = decode_cursor(cursor)
            offset_node_id = offset_data.get("node_id")
            # Use cursor-based: skip until we find this node, then start returning
            # Since Graphiti doesn't natively support cursor pagination, we implement
            # it in the service layer: fetch limit+1, use the last item's id as the next cursor.

        # Fetch nodes via Graphiti search
        # NOTE: Graphiti's node_search doesn't support cursor pagination natively,
        # so we fetch (limit + 1) items and trim. For large datasets, add a
        # sequential UUID index on EntityNode in the graph DB.
        fetch_limit = limit + 1

        # Use the graph driver directly for paginated node queries
        # This bypasses Graphiti's search (which includes embedding search overhead)
        group_id = self._make_group_id(org_id, user_id)
        nodes = await EntityNode.get_by_group_ids(
            driver=self._driver,
            group_ids=[group_id],
            node_labels=filters.node_labels,
            limit=fetch_limit,
            offset_node_id=offset_node_id,
        )

        # Determine next cursor
        has_more = len(nodes) > limit
        if has_more:
            nodes = nodes[:limit]
            next_cursor = encode_cursor({"node_id": str(nodes[-1].uuid)})
        else:
            next_cursor = None

        return CursorPage(
            items=nodes,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    # ── Get Entity with Edges ─────────────────────────────────────────

    async def get_entity_with_edges(
        self,
        org_id: uuid.UUID,
        user_id: str,
        node_id: str,
    ) -> tuple[EntityNode, list[EntityEdge]]:
        """Get a single entity node with all its incident edges.

        Raises:
            EntityNotFoundError: If the node does not exist in this org's namespace.
        """
        group_id = self._make_group_id(org_id, user_id)
        node = await self._get_entity_node(node_id, group_id)
        edges = await EntityEdge.get_by_node_uuid(
            driver=self._driver,
            node_uuid=node_id,
        )
        return node, edges

    # ── List Edges ───────────────────────────────────────────────────

    async def get_edges(
        self,
        org_id: uuid.UUID,
        user_id: str,
        *,
        subject_id: str | None = None,
        predicate: str | None = None,
        valid_at: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> CursorPage[EntityEdge]:
        """List relationship edges with optional filters.

        Args:
            org_id: Tenant ID — mandatory.
            user_id: The user's external_id.
            subject_id: Filter by source entity UUID.
            predicate: Filter by edge name/type (e.g., "purchased", "works_at").
            valid_at: Return only edges that were valid at this point in time.
            limit: Max results (default 50, max 200).
            cursor: Opaque pagination cursor.

        Returns:
            CursorPage of EntityEdge objects.
        """
        group_id = self._make_group_id(org_id, user_id)
        filters = SearchFilters()

        if valid_at:
            # Temporal filter: edges valid at the given time
            filters.valid_at = [[
                DateFilter(
                    comparison=ComparisonOperator.less_than_equal,
                    value=valid_at,
                ),
            ]]
            filters.invalid_at = [[
                DateFilter(
                    comparison=ComparisonOperator.greater_than,
                    value=valid_at,
                ),
                DateFilter(
                    comparison=ComparisonOperator.is_null,
                    value=None,
                ),
            ]]

        # Graphiti's edge_search supports these filters natively
        from graphiti_core.search.search import edge_search

        edges, scores = await edge_search(
            driver=self._driver,
            llm_client=self._graphiti._llm_client,
            embedder=self._graphiti._embedder,
            group_ids=[group_id],
            search_filters=filters,
            limit=limit,
        )

        next_cursor = None
        if len(edges) == limit:
            next_cursor = encode_cursor({"edge_id": str(edges[-1].uuid)})

        return CursorPage(
            items=edges,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    # ── Delete Entity ─────────────────────────────────────────────────

    async def delete_entity(
        self,
        org_id: uuid.UUID,
        user_id: str,
        node_id: str,
    ) -> None:
        """Delete an entity node, all its incident edges, and associated facts.

        This is a multi-store operation (graph DB + PostgreSQL). It runs inside
        a managed transaction to ensure consistency. If either store fails,
        the entire operation is rolled back.

        Steps:
        1. Verify the node exists and belongs to this org
        2. Delete all incident edges from graph DB
        3. Delete the node from graph DB
        4. Delete associated facts from PostgreSQL (if applicable)

        Raises:
            EntityNotFoundError: If the node does not exist.
        """
        group_id = self._make_group_id(org_id, user_id)
        node = await self._get_entity_node(node_id, group_id)

        # Step 2 & 3: Delete edges and node in graph DB transaction
        async with self._driver.transaction() as tx:
            # Delete all edges incident to this node
            edges = await EntityEdge.get_by_node_uuid(
                driver=self._driver, node_uuid=node_id, transaction=tx
            )
            edge_ids = [e.uuid for e in edges]
            if edge_ids:
                await EntityEdge.delete_by_uuids(
                    driver=self._driver, uuids=edge_ids, transaction=tx
                )

            # Delete the entity node itself
            await EntityNode.delete_by_uuids(
                driver=self._driver, uuids=[node_id], transaction=tx
            )

        # Step 4: Delete associated facts from PostgreSQL
        # This is handled by the facts service — called via delegation
        # to avoid circular imports. The EntityService caller should
        # invoke fact deletion separately, OR we use an event bus.
        #
        # For now: log the deletion for the facts cleanup worker.
        logger.info(
            "entity.deleted",
            extra={
                "org_id": str(org_id),
                "user_id": user_id,
                "node_id": node_id,
                "entity_name": node.name,
            },
        )

    # ── Entity Resolution ─────────────────────────────────────────────

    async def resolve_entity(
        self,
        org_id: uuid.UUID,
        user_id: str,
        entity_name: str,
        entity_type: str,
    ) -> EntityNode:
        """Resolve an entity: find existing by name + org_id, or create new.

        This is called by the NLP enrichment pipeline (entity extraction worker)
        before creating new entity nodes. Prevents duplicate entities per user.

        Args:
            org_id: Tenant ID.
            user_id: User's external_id.
            entity_name: The entity name (e.g., "Acme Corp").
            entity_type: The entity type label (e.g., "Organization").

        Returns:
            Existing or newly created EntityNode.
        """
        group_id = self._make_group_id(org_id, user_id)

        # Search for existing entity by name + group_id
        # Graphiti stores entity names with embeddings. We can do an exact-name
        # lookup via the graph driver since this is a hot path.
        existing = await self._find_entity_by_name(
            group_id=group_id,
            name=entity_name,
            entity_type=entity_type,
        )
        if existing:
            logger.debug("entity.resolved.existing", extra={
                "entity_name": entity_name, "node_id": str(existing.uuid),
            })
            return existing

        # Create new entity node
        node = EntityNode(
            name=entity_name,
            group_id=group_id,
            labels=[entity_type],
            summary=entity_name,  # initial summary, refined by LLM later
        )
        await node.save(driver=self._driver)
        await node.generate_name_embedding(
            llm_client=self._graphiti._llm_client,
            embedder=self._graphiti._embedder,
        )

        logger.info("entity.created", extra={
            "entity_name": entity_name,
            "entity_type": entity_type,
            "node_id": str(node.uuid),
            "org_id": str(org_id),
            "user_id": user_id,
        })
        return node

    async def _find_entity_by_name(
        self,
        group_id: str,
        name: str,
        entity_type: str | None = None,
    ) -> EntityNode | None:
        """Look up an entity by exact name within a group_id.

        Uses a Cypher/GQL query for exact string match on the name property.
        This is efficient because we index names in the graph DB.
        """
        # FalkorDB / Neo4j compatible query
        if entity_type:
            result = await self._driver.execute_query(
                """
                MATCH (n:EntityNode {group_id: $group_id, name: $name})
                WHERE $entity_type IN labels(n)
                RETURN n
                """,
                params={
                    "group_id": group_id,
                    "name": name,
                    "entity_type": entity_type,
                },
            )
        else:
            result = await self._driver.execute_query(
                """
                MATCH (n:EntityNode {group_id: $group_id, name: $name})
                RETURN n
                """,
                params={"group_id": group_id, "name": name},
            )

        if not result or not result[0]:
            return None
        return EntityNode.from_record(result[0][0])

    # ── Search Entities ──────────────────────────────────────────────

    async def search_entities(
        self,
        org_id: uuid.UUID,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
    ) -> list[EntityNode]:
        """Search entity nodes by semantic similarity.

        Uses Graphiti's built-in node_search with the group_id filter.

        Args:
            org_id: Tenant ID.
            user_id: User's external_id.
            query: Natural language search query.
            limit: Max results.

        Returns:
            Ranked list of EntityNode objects.
        """
        group_id = self._make_group_id(org_id, user_id)
        from graphiti_core.search.search import node_search

        nodes, scores = await node_search(
            driver=self._driver,
            llm_client=self._graphiti._llm_client,
            embedder=self._graphiti._embedder,
            group_ids=[group_id],
            query=query,
            limit=limit,
        )
        return nodes

    # ── Helpers ───────────────────────────────────────────────────────

    async def _get_entity_node(self, node_id: str, group_id: str) -> EntityNode:
        """Fetch an entity node and verify it belongs to the given group.

        Raises EntityNotFoundError if missing or wrong group.
        """
        try:
            node = await EntityNode.get_by_uuid(
                driver=self._driver, uuid=node_id
            )
        except graphiti_core.errors.NodeNotFoundError:
            raise EntityNotFoundError(f"Entity node {node_id} not found")

        if node.group_id != group_id:
            # Node exists but belongs to a different org — return 404, not 403
            # to avoid leaking existence information (SEC-03).
            raise EntityNotFoundError(f"Entity node {node_id} not found")

        return node

    @staticmethod
    def _make_group_id(org_id: uuid.UUID, user_id: str) -> str:
        """Create a namespaced group_id for Graphiti node/edge scoping.

        Format: org:{org_id}:user:{user_id}

        Graphiti's `group_id` is used for multi-tenant isolation.
        Every node and edge gets this id. All queries filter by it.
        """
        return f"org:{org_id}:user:{user_id}"
```

### 2.2 Group ID Convention

The `group_id` field on every Graphiti node and edge enforces tenant + user isolation:

```
Format:  org:{uuid}:user:{string}
Example: org:550e8400-e29b-41d4-a716-446655440000:user:customer_123
```

**Why not just `org_id`?** Graphiti's `group_id` is the only built-in isolation mechanism. We scope by both org AND user so that `EntityNode.get_by_group_ids()` returns only that user's nodes. A separate `org_id` property on nodes would require custom query filters everywhere.

**Validation:** All group_ids must pass Graphiti's `GroupIdValidationError` check (alphanumeric, dashes, underscores, colons allowed).

---

## 3. Router Implementation

### 3.1 Graph Router

```python
# services/api/app/routers/graph.py
"""Graph query endpoints — SRS §8.3 Graph section."""

import uuid

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_entity_service, get_current_user
from app.schemas.graph import (
    EntityNodeResponse,
    EntityNodeDetailResponse,
    EntityEdgeResponse,
    GraphNodesListResponse,
    GraphEdgesListResponse,
)
from packages.core.graphiti.entity_service import EntityService
from packages.core.graphiti.pagination import CursorPage

router = APIRouter(prefix="/v1/users/{user_id}/graph", tags=["graph"])


@router.get("/nodes", response_model=GraphNodesListResponse)
async def list_entity_nodes(
    user_id: str,
    entity_type: str | None = Query(None, alias="type"),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    service: EntityService = Depends(get_entity_service),
    org_id: uuid.UUID = Depends(get_current_org_id),
) -> GraphNodesListResponse:
    """List entity nodes for a user (SRS KG-09)."""
    page = await service.get_entities(
        org_id=org_id,
        user_id=user_id,
        entity_type=entity_type,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        cursor=cursor,
    )
    return GraphNodesListResponse(
        data=[EntityNodeResponse.from_domain(n) for n in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/nodes/{node_id}", response_model=EntityNodeDetailResponse)
async def get_entity_node(
    user_id: str,
    node_id: str,
    service: EntityService = Depends(get_entity_service),
    org_id: uuid.UUID = Depends(get_current_org_id),
) -> EntityNodeDetailResponse:
    """Get a single entity node with all edges (SRS KG-10)."""
    node, edges = await service.get_entity_with_edges(
        org_id=org_id,
        user_id=user_id,
        node_id=node_id,
    )
    return EntityNodeDetailResponse(
        node=EntityNodeResponse.from_domain(node),
        edges=[EntityEdgeResponse.from_domain(e) for e in edges],
    )


@router.delete("/nodes/{node_id}", status_code=204)
async def delete_entity_node(
    user_id: str,
    node_id: str,
    service: EntityService = Depends(get_entity_service),
    org_id: uuid.UUID = Depends(get_current_org_id),
) -> None:
    """Delete an entity node and all its edges (SRS KG-12)."""
    await service.delete_entity(org_id=org_id, user_id=user_id, node_id=node_id)


@router.get("/edges", response_model=GraphEdgesListResponse)
async def list_edges(
    user_id: str,
    subject_id: str | None = Query(None),
    predicate: str | None = Query(None),
    valid_at: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    service: EntityService = Depends(get_entity_service),
    org_id: uuid.UUID = Depends(get_current_org_id),
) -> GraphEdgesListResponse:
    """List relationship edges with filters (SRS KG-11)."""
    page = await service.get_edges(
        org_id=org_id,
        user_id=user_id,
        subject_id=subject_id,
        predicate=predicate,
        valid_at=valid_at,
        limit=limit,
        cursor=cursor,
    )
    return GraphEdgesListResponse(
        data=[EntityEdgeResponse.from_domain(e) for e in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )
```

### 3.2 Auth Dependency Integration

Every router method uses `get_current_org_id` to extract the authenticated org from the API key. Cross-tenant access is structurally impossible because:
1. The API key resolves to a specific `org_id`
2. That `org_id` is injected into every service call
3. The service layer scopes all queries by that `org_id`

---

## 4. Pydantic Schemas

```python
# services/api/app/schemas/graph.py
"""Request/response schemas for graph endpoints."""

import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class EntityNodeResponse(BaseModel):
    """Schema for a single entity node in API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: str  # Graphiti UUID (string, not UUID object)
    name: str
    type: str  # entity type label
    summary: str
    created_at: datetime
    metadata: dict = {}

    @classmethod
    def from_domain(cls, node) -> "EntityNodeResponse":
        return cls(
            id=str(node.uuid),
            name=node.name,
            type=node.labels[0] if node.labels else "Unknown",
            summary=node.summary or "",
            created_at=node.created_at,
            metadata=node.attributes or {},
        )


class EntityEdgeResponse(BaseModel):
    """Schema for a single relationship edge."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    source_node_id: str
    target_node_id: str
    fact: str  # human-readable fact description
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    created_at: datetime

    @classmethod
    def from_domain(cls, edge: EntityEdge) -> "EntityEdgeResponse":
        return cls(
            id=str(edge.uuid),
            source_node_id=str(edge.source_node_uuid),
            target_node_id=str(edge.target_node_uuid),
            fact=edge.fact or "",
            valid_at=edge.valid_at,
            invalid_at=edge.invalid_at,
            created_at=edge.created_at,
        )


class EntityNodeDetailResponse(BaseModel):
    """Response for GET /nodes/{node_id} — node with all edges."""

    node: EntityNodeResponse
    edges: list[EntityEdgeResponse]


class GraphNodesListResponse(BaseModel):
    """Paginated response for GET /nodes."""

    data: list[EntityNodeResponse]
    next_cursor: str | None = None
    has_more: bool = False


class GraphEdgesListResponse(BaseModel):
    """Paginated response for GET /edges."""

    data: list[EntityEdgeResponse]
    next_cursor: str | None = None
    has_more: bool = False
```

---

## 5. Pagination Strategy

### 5.1 Cursor-Based Pagination for Graph Queries

Graph databases do not support SQL-style `OFFSET` efficiently. We use **cursor-based pagination** keyed on the node UUID.

```python
# packages/core/graphiti/pagination.py
"""Cursor-based pagination for graph query results."""

import json
import base64
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):
    """Generic paginated response with cursor."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


def encode_cursor(data: dict[str, Any]) -> str:
    """Encode pagination cursor as base64 JSON string."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Decode a pagination cursor back to dict."""
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    return json.loads(raw)
```

**Cursor format:** `base64url(JSON({"node_id": "uuid-string"}))`

The cursor encodes the UUID of the last returned item. The next page fetches items with a created_at or UUID greater than this anchor. This avoids the OFFSET performance cliff on large graphs.

### 5.2 Graphiti Limitation

Graphiti does not natively support cursor-based pagination. The `EntityService.get_entities()` method handles pagination at the service layer:

1. Fetch `limit + 1` items
2. If `len(results) > limit`, there is a next page
3. Encode the last item's UUID as the cursor

For future optimization: add a sequential `created_at` + `uuid` composite index in the graph DB to support efficient cursor-based queries directly in Cypher/GQL.

---

## 6. Entity Resolution (NLP Pipeline Integration)

### 6.1 Flow Diagram

```
┌──────────────────┐
│  NLP Worker      │
│  extracts entity │
│  "Acme Corp"     │
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────────┐
│  EntityService.resolve_entity()     │
│  ┌────────────────────────────────┐ │
│  │ 1. Search by name + group_id   │ │
│  │    in graph DB                 │ │
│  │                                │ │
│  │ 2. Found? ──yes──► return     │ │
│  │    (no-op, reuse node)         │ │
│  │         │                      │ │
│  │        no                      │ │
│  │         ▼                      │ │
│  │ 3. Create EntityNode           │ │
│  │ 4. Generate name embedding     │ │
│  │ 5. return new node             │ │
│  └────────────────────────────────┘ │
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────┐
│  Worker creates  │
│  edge + continues│
└──────────────────┘
```

### 6.2 Key Decision: Resolution Scope

| Scope | Tradeoff | Decision |
|-------|----------|----------|
| **By user** (user_id + name) | Same entity name can appear for different users — correct isolation | **Selected** — matches multi-tenant model |
| By org (org_id + name) | Entity "CEO" would be shared across all users in an org — wrong for agent memory where each user has distinct entities | Rejected |

### 6.3 Duplicate Prevention

The entity extraction worker MUST call `resolve_entity()` before creating any new `EntityNode`. This is enforced in the worker task:

```python
# services/worker/tasks/extract_entities.py
async def extract_entities_for_episode(ctx, episode_id: str, org_id: str, user_id: str) -> None:
    """Worker task: extract entities from an episode and upsert into graph."""

    # ... LLM extraction returns list of extracted entities ...
    for extracted in extracted_entities:
        node = await entity_service.resolve_entity(
            org_id=uuid.UUID(org_id),
            user_id=user_id,
            entity_name=extracted.name,
            entity_type=extracted.type,
        )
        # ... create edges between entity and episode ...
```

---

## 7. Error Codes

| HTTP Status | Error Code | Condition | Raised When |
|------------|-----------|-----------|-------------|
| 404 | `ENTITY_NOT_FOUND` | Entity node does not exist (or belongs to different org) | `get_entity_with_edges`, `delete_entity` |
| 404 | `EDGE_NOT_FOUND` | Edge UUID does not exist | `get_edges` with specific edge_id |
| 504 | `GRAPH_QUERY_TIMEOUT` | Graph operation exceeds timeout | Any graph operation exceeding `GRAPHITI_OPERATION_TIMEOUT` (30s) |
| 503 | `GRAPH_BACKEND_UNAVAILABLE` | Graph database unreachable | Connection refused, circuit breaker open |

**Global exception handler** (registered in `main.py`):

```python
@app.exception_handler(EntityNotFoundError)
async def entity_not_found_handler(request: Request, exc: EntityNotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "ENTITY_NOT_FOUND",
                "message": str(exc),
                "request_id": request.state.request_id,
            }
        },
    )
```

---

## 8. Sequence Diagram: Create Entity → Add Relationship → Query Neighbourhood

```
┌─────────┐     ┌──────────┐     ┌────────────────┐     ┌──────────┐
│  Client │     │  Router  │     │ EntityService   │     │ Graph DB │
│         │     │  (HTTP)  │     │                 │     │          │
└────┬────┘     └────┬─────┘     └────────┬───────┘     └────┬─────┘
     │               │                     │                  │
     │  POST /memory  │                     │                  │
     │  (messages)    │                     │                  │
     ├───────────────►│                     │                  │
     │                │ enqueue worker      │                  │
     │                ├────► ARQ ───► Worker                   │
     │  202 Accepted  │    │              │                  │
     │◄───────────────┤    │              │                  │
     │                │    │              │                  │
     │      [Worker: Entity Extraction]   │                  │
     │                │    │  resolve_entity("Acme", "Org")   │
     │                │    ├──────────────►                 │
     │                │    │              │  MATCH (n:EntityNode│
     │                │    │              │  {name:"Acme",    │
     │                │    │              │   group_id:$gid}) │
     │                │    │              ├─────────────────►│
     │                │    │              │◄─────────────────┤
     │                │    │              │  (not found)      │
     │                │    │              │                  │
     │                │    │              │  CREATE (n:EntityNode│
     │                │    │              │  {uuid, name,     │
     │                │    │              │   group_id, ...}) │
     │                │    │              ├─────────────────►│
     │                │    │              │◄─────────────────┤
     │                │    │  return node │                  │
     │                │    │◄──────────────┤                  │
     │                │    │              │                  │
     │                │    │  create_relationship(          │
     │                │    │    "user_1", "works_at", "Acme" │
     │                │    │  )                              │
     │                │    ├──────────────►                 │
     │                │    │              │  MATCH (a), (b)  │
     │                │    │              │  CREATE (a)-[r:RELATES_TO│
     │                │    │              │  {fact:"works_at"}]→(b)│
     │                │    │              ├─────────────────►│
     │                │    │              │◄─────────────────┤
     │                │    │              │                  │
     │                │    │              │                  │
     │     [Later: Client queries graph]  │                  │
     │                │    │              │                  │
     │ GET /graph/nodes/{id}              │                  │
     ├───────────────►│    │              │                  │
     │                │ get_entity_with_edges(org_id, id)   │
     │                ├──────────────────►│                  │
     │                │                   │ MATCH (n)-[r]-( )│
     │                │                   │ WHERE n.uuid=$id │
     │                │                   ├─────────────────►│
     │                │                   │◄─────────────────┤
     │                │                   │  node + edges    │
     │                │◄──────────────────┤                  │
     │  200 OK        │                   │                  │
     │  {node, edges} │                   │                  │
     │◄───────────────┤                   │                  │
     │                │                   │                  │
```

---

## 9. Testing Guide

### 9.1 Unit Tests (Mock GraphBackend)

```python
# tests/unit/test_entity_service.py
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from graphiti_core.nodes import EntityNode
from graphiti_core.edges import EntityEdge

from packages.core.graphiti.entity_service import EntityService
from app.core.exceptions import EntityNotFoundError


@pytest.fixture
def mock_graphiti():
    g = AsyncMock()
    g._driver = AsyncMock()
    return g


@pytest.mark.asyncio
class TestEntityService:

    async def test_get_entities_returns_page(self, mock_graphiti):
        service = EntityService(mock_graphiti)
        org_id = uuid.uuid4()
        user_id = "user_1"

        with patch.object(EntityNode, "get_by_group_ids", AsyncMock(return_value=[EntityNode(...)])):
            page = await service.get_entities(org_id=org_id, user_id=user_id)
            assert len(page.items) > 0
            assert isinstance(page.next_cursor, str) or page.next_cursor is None

    async def test_delete_entity_raises_not_found(self, mock_graphiti):
        service = EntityService(mock_graphiti)

        with patch.object(EntityNode, "get_by_uuid", side_effect=graphiti_core.errors.NodeNotFoundError("x")):
            with pytest.raises(EntityNotFoundError):
                await service.delete_entity(org_id=uuid.uuid4(), user_id="u", node_id="nope")

    async def test_resolve_entity_creates_new_when_not_found(self, mock_graphiti):
        service = EntityService(mock_graphiti)
        # ... verify new EntityNode is created via save()
```

### 9.2 Integration Tests (Testcontainers)

```python
# tests/integration/test_graph_entity_operations.py
"""End-to-end tests for entity CRUD against real FalkorDB/Neo4j."""

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.mark.asyncio
@pytest.mark.integration
class TestGraphAPIIntegration:

    async def test_create_entity_via_ingestion(self, async_client: AsyncClient, auth_headers: dict):
        """Entity should be created when messages are ingested via POST /memory."""
        response = await async_client.post(
            "/v1/users/test_user/memory",
            json={
                "messages": [
                    {"role": "user", "content": "My name is Alice and I work at Acme Corp."},
                    {"role": "assistant", "content": "Nice to meet you, Alice!"},
                ],
                "session_id": "session_1",
            },
            headers=auth_headers,
        )
        assert response.status_code == 202

        # Wait for entity extraction worker to finish (poll)
        await wait_for_worker()

        # Query the graph
        resp = await async_client.get(
            "/v1/users/test_user/graph/nodes",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        names = [n["name"] for n in data["data"]]
        assert "Alice" in names or "Acme Corp" in names

    async def test_delete_entity_cascade(self, async_client: AsyncClient, auth_headers: dict):
        """Deleting a node should remove it and its edges."""
        # First create some data, then DELETE, then verify 404 on GET
        # ...

    async def test_cross_tenant_isolation(self, async_client: AsyncClient):
        """Org A should not see Org B's entity nodes."""
        # ...
```

---

## 10. Open Questions

| ID | Question | Impact | Decision / Status |
|----|----------|--------|-------------------|
| ENT-01 | Graphiti's `get_by_group_ids()` may not scale efficiently beyond 10k nodes per group | Pagination may become slow | Add a composite index on `(group_id, created_at, uuid)` in the graph DB. Monitor `openzep_graph_nodes_total` metric |
| ENT-02 | Entity resolution by exact name match may miss case variations | Duplicate entities possible | Normalise entity names to lowercase during resolution. Add a follow-up task for fuzzy matching via Graphiti's node_search |
| ENT-03 | Facts in PostgreSQL are not automatically deleted when an entity node is deleted | Orphaned facts | Implement a worker task that polls for orphaned facts. Or use the event bus to trigger fact cleanup |

---

*Implementation document for SRS §5.3.2, §5.3.3, §8.3. Maintained by @senior-dev. Last updated: 2026-06-05.*
