# Community Detection — Entity Clustering & LLM Summarisation

## 1. Overview

| Field | Detail |
|-------|--------|
| **Coverage** | Algorithm selection, scheduled ARQ worker, graph export → community detection → LLM summary → upsert, API endpoint, error handling, regeneration trigger |
| **Dependencies** | [01-graphiti-setup.md](01-graphiti-setup.md) (Graphiti init), [02-entity-operations.md](02-entity-operations.md) (EntityService, group_id convention), [06-arq-setup.md](../06-worker-system/01-arq-setup.md) (ARQ configuration), [06-scheduled-tasks.md](../06-worker-system/06-scheduled-tasks.md) (cron scheduling) |
| **SRS Requirement IDs** | KG-08 (community layer), NLP-15 (community detection), NLP-16 (LLM summary generation), NLP-17 (regeneration schedule), WRK-05 (priority queues), WRK-07 (low priority queue), OQ-07 (performance concerns) |
| **Build Phase** | Phase 2 (Full Feature Parity) |
| **Design Authority** | @senior-dev for algorithm implementation, @architect for trigger/threshold design |

### 1.1 What This Doc Covers

- Algorithm choice: Label Propagation (initial) vs Louvain (future)
- Scheduled trigger via ARQ low-priority queue
- Scope: only orgs exceeding a configurable entity count threshold
- End-to-end process flow with LLM summary generation
- Community storage model (CommunityNode + MEMBER_OF edges)
- API endpoint: `GET /users/{user_id}/graph/communities`
- Error handling: timeouts, LLM failures, partial results
- Regeneration strategy

---

## 2. Algorithm Choice

### 2.1 Decision: Label Propagation for MVP, Louvain for Later

| Algorithm | Runtime | Parameters | Quality | Use Case |
|-----------|---------|------------|---------|----------|
| **Label Propagation** (chosen for Phase 2) | O(n) — linear in nodes | None required | Good — detects meaningful clusters quickly | **Initial deployment** — fast, no parameter tuning, good enough for LLM summarisation |
| **Louvain** (Phase 4+) | O(n log n) | Resolution parameter tuning | Better — modularity optimisation yields tighter clusters | **High-quality communities** — for orgs with complex entity graphs |

**Why not both from the start?** Louvain requires resolution parameter tuning and is harder to get right without ground-truth community labels. Label Propagation is deterministic, converges quickly, and produces communities that LLMs can summarise effectively.

**Implementation library:** Use `networkx` + `networkx.algorithms.community.label_propagation` for Label Propagation. The entity graph is exported from Graphiti as a NetworkX graph.

```python
# requirements.txt
networkx>=3.2,<4.0
# installed but not imported directly — used internally by community module
```

### 2.2 Algorithm Details

```python
# packages/core/graphiti/community/algorithms.py
"""Community detection algorithms for entity graphs."""

import networkx as nx
from networkx.algorithms.community import label_propagation_communities


def detect_communities_label_propagation(graph: nx.Graph) -> list[set[str]]:
    """Run Label Propagation community detection on an entity graph.

    Args:
        graph: NetworkX graph where nodes are entity UUIDs and edges are
               RELATES_TO relationships.

    Returns:
        List of node sets, each set representing a community.
    """
    communities = list(label_propagation_communities(graph))
    # Filter out singletons (single-node communities) — they don't need summarisation
    return [c for c in communities if len(c) >= 2]
```

---

## 3. Trigger & Scope

### 3.1 Trigger: Scheduled ARQ Task (Low Priority Queue)

Community detection runs on a **nightly schedule** via ARQ's cron support. It uses the **low-priority queue** (WRK-07) to avoid competing with real-time ingestion tasks.

```python
# services/worker/tasks/community.py
"""Scheduled community summarisation task."""

import uuid
from datetime import datetime, timezone

from arq import cron


async def summarise_communities(ctx) -> None:
    """Scheduled task: run community detection for all eligible orgs.

    Scheduled daily at 02:00 UTC via ARQ cron (low priority queue).
    """
    logger.info("community.summarisation.starting")

    # Get all orgs that exceed the entity count threshold
    eligible_orgs = await get_eligible_orgs(
        ctx["db"],
        min_entity_count=settings.COMMUNITY_MIN_ENTITY_COUNT,  # default: 100
    )

    for org in eligible_orgs:
        try:
            # Process each org independently — failure in one org
            # should not affect others
            await process_org_communities(
                ctx=ctx,
                org_id=org["id"],
                org_name=org["name"],
            )
        except Exception as e:
            logger.error(
                "community.summarisation.org_failed",
                extra={
                    "org_id": str(org["id"]),
                    "error": str(e),
                    "elapsed_seconds": ...,
                },
            )
            # Continue with next org

    logger.info(
        "community.summarisation.completed",
        extra={"orgs_processed": len(eligible_orgs)},
    )


# ARQ cron registration (in services/worker/worker.py)
class WorkerSettings:
    cron_jobs: list[cron] = [
        cron(
            summarise_communities,
            minute=0,
            hour=2,  # 02:00 UTC daily
            queue="low",
            job_timeout=3600,  # 1 hour max for the entire pass
            keep_result=0,     # don't keep results
        ),
    ]
```

### 3.2 Eligibility Criteria

An organisation is eligible for community detection when:

```sql
-- Check entity count per org
SELECT o.id, o.name, COUNT(DISTINCT en.id) AS entity_count
FROM organizations o
JOIN entity_graph_metadata egm ON egm.org_id = o.id
WHERE egm.entity_count >= $1  -- COMMUNITY_MIN_ENTITY_COUNT (default: 100)
GROUP BY o.id, o.name;
```

The entity count is stored in a lightweight metadata table updated by a periodic count task:

```sql
CREATE TABLE entity_graph_metadata (
    org_id       UUID PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE,
    entity_count INTEGER NOT NULL DEFAULT 0,
    edge_count   INTEGER NOT NULL DEFAULT 0,
    last_community_run TIMESTAMPTZ,
    entity_count_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 3.3 Threshold Configuration

```python
# packages/core/config/settings.py

# Community detection
COMMUNITY_MIN_ENTITY_COUNT: int = 100     # minimum entities to trigger community detection
COMMUNITY_DETECTION_TIMEOUT: int = 60     # seconds per org
COMMUNITY_REGEN_THRESHOLD_PCT: float = 0.2  # regenerate when entity count increases by >20%
COMMUNITY_MAX_COMMUNITIES: int = 50       # max communities per org (safety limit)
```

---

## 4. Process Flow

### 4.1 End-to-End Process

```
┌──────────────┐    02:00 UTC cron trigger
│   ARQ Cron   │──────────►
└──────────────┘
      │
      ▼
┌──────────────────────────────┐
│ summarise_communities()     │
│ (iterate eligible orgs)      │
└──────────┬───────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ process_org_communities()   │
│                              │
│ 1. Export entity graph       │
│    from Graphiti as nx.Graph │
│                              │
│ 2. Run Label Propagation     │
│    (networkx)                │
│                              │
│ 3. For each community:       │
│    a. Collect entity         │
│       summaries              │
│    b. Generate LLM summary   │
│       (3 sentences)          │
│    c. Upsert CommunityNode   │
│    d. Create MEMBER_OF       │
│       edges                  │
│                              │
│ 4. Update metadata           │
│    (last_community_run)      │
└──────────────────────────────┘
```

### 4.2 Implementation

```python
# packages/core/graphiti/community/detector.py
"""Community detection orchestrator."""

import logging
from datetime import datetime, timezone
from uuid import UUID

import networkx as nx
from graphiti_core import Graphiti
from graphiti_core.nodes import CommunityNode, EntityNode
from graphiti_core.edges import CommunityEdge, EntityEdge

from packages.core.graphiti.community.algorithms import (
    detect_communities_label_propagation,
)
from packages.core.graphiti.community.prompts import COMMUNITY_SUMMARY_PROMPT
from app.core.config import settings

logger = logging.getLogger(__name__)


async def process_org_communities(
    ctx,
    org_id: UUID,
    graphiti: Graphiti,
    db,
) -> None:
    """Run community detection for a single organisation.

    Args:
        ctx: ARQ worker context.
        org_id: Organisation UUID.
        graphiti: Initialised Graphiti instance.
        db: SQLAlchemy async session for metadata updates.
    """
    driver = graphiti._driver
    llm_client = graphiti._llm_client

    # Step 1: Export the entity graph as a NetworkX graph
    entity_graph = await _export_entity_graph(driver, org_id)

    if entity_graph.number_of_nodes() < settings.COMMUNITY_MIN_ENTITY_COUNT:
        logger.debug("community.skipped.too_few_entities", extra={
            "org_id": str(org_id),
            "entity_count": entity_graph.number_of_nodes(),
        })
        return

    # Step 2: Detect communities
    communities = detect_communities_label_propagation(entity_graph)

    # Safety limit: don't exceed max communities
    if len(communities) > settings.COMMUNITY_MAX_COMMUNITIES:
        logger.warning(
            "community.too_many_communities",
            extra={
                "org_id": str(org_id),
                "count": len(communities),
                "max": settings.COMMUNITY_MAX_COMMUNITIES,
            },
        )
        communities = communities[: settings.COMMUNITY_MAX_COMMUNITIES]

    logger.info(
        "community.detection.completed",
        extra={
            "org_id": str(org_id),
            "entity_count": entity_graph.number_of_nodes(),
            "community_count": len(communities),
        },
    )

    # Step 3: Generate summaries and upsert
    for community_nodes in communities:
        try:
            await _process_single_community(
                driver=driver,
                llm_client=llm_client,
                entity_graph=entity_graph,
                community_nodes=community_nodes,
                org_id=org_id,
            )
        except Exception as e:
            logger.error(
                "community.summary_failed",
                extra={
                    "org_id": str(org_id),
                    "community_size": len(community_nodes),
                    "error": str(e),
                },
            )
            # Continue with other communities

    # Step 4: Update metadata
    await _update_community_metadata(db, org_id, datetime.now(timezone.utc))


async def _export_entity_graph(driver, org_id: UUID) -> nx.Graph:
    """Export all entities and relationships for an org as a NetworkX graph.

    Queries all EntityNodes and EntityEdges where group_id is scoped to the org.
    Even though group_id includes user_id, we aggregate across ALL users in the org
    because communities span users — "Alice" in one user's graph is the same as
    "Alice" in another's.
    """
    # Fetch all entity nodes for the org (across all users)
    # Use org: prefix only (not org:uuid:user:xxx) to span users
    org_prefix = f"org:{org_id}:"

    # Fetch nodes via Graphiti's group_id prefix query
    # NOTE: Graphiti doesn't natively support prefix queries on group_id.
    # Instead, fetch all nodes and filter by org prefix.
    # For large orgs (>50k nodes), optimize with a direct driver query.
    nodes = await EntityNode.get_by_group_ids(
        driver=driver,
        group_ids=[org_prefix],  # may not work with partial prefix
    )

    # If prefix query is not supported, fall back to Cypher:
    # MATCH (n:EntityNode) WHERE n.group_id STARTS WITH $prefix RETURN n
    result = await driver.execute_query(
        "MATCH (n:EntityNode) WHERE n.group_id STARTS WITH $prefix RETURN n",
        params={"prefix": org_prefix},
    )
    nodes = [EntityNode.from_record(row[0]) for row in result]

    # Build the graph
    G = nx.Graph()
    for node in nodes:
        G.add_node(str(node.uuid), name=node.name, summary=node.summary, type=node.labels)

    # Fetch all edges within this org
    edge_result = await driver.execute_query(
        """
        MATCH (s:EntityNode)-[r:RELATES_TO]->(t:EntityNode)
        WHERE s.group_id STARTS WITH $prefix
        RETURN r
        """,
        params={"prefix": org_prefix},
    )
    for row in edge_result:
        edge = EntityEdge.from_record(row[0])
        # Only add edges where both endpoints are in the graph
        if str(edge.source_node_uuid) in G and str(edge.target_node_uuid) in G:
            G.add_edge(
                str(edge.source_node_uuid),
                str(edge.target_node_uuid),
                name=edge.name,
                fact=edge.fact,
            )

    return G


async def _process_single_community(
    driver,
    llm_client,
    entity_graph: nx.Graph,
    community_nodes: set[str],
    org_id: UUID,
) -> None:
    """Generate a summary for a single community and upsert it.

    Steps:
    1. Collect entity summaries from the subgraph
    2. Generate LLM summary (3 sentences)
    3. Upsert CommunityNode
    4. Create MEMBER_OF edges from entities to community
    """
    # Step 1: Collect entity data
    entity_details = []
    for node_id in community_nodes:
        data = entity_graph.nodes[node_id]
        entity_details.append({
            "name": data.get("name", "Unknown"),
            "type": data.get("type", ["Unknown"])[0] if data.get("type") else "Unknown",
            "summary": data.get("summary", ""),
        })

    # Build the relationships within this community
    subgraph = entity_graph.subgraph(community_nodes)
    relationship_descriptions = []
    for u, v, data in subgraph.edges(data=True):
        u_name = entity_graph.nodes[u].get("name", u)
        v_name = entity_graph.nodes[v].get("name", v)
        fact = data.get("fact", data.get("name", "related_to"))
        relationship_descriptions.append(f"{u_name} -- {fact} -- {v_name}")

    # Step 2: LLM summary generation
    summary = await _generate_community_summary(
        llm_client=llm_client,
        entities=entity_details,
        relationships=relationship_descriptions,
    )

    # Step 3: Upsert CommunityNode
    community_name = _generate_community_name(entity_details)
    community_node = CommunityNode(
        name=community_name,
        group_id=f"org:{org_id}:community",
        summary=summary,
    )
    await community_node.save(driver=driver)
    await community_node.generate_name_embedding(llm_client=llm_client, embedder=None)

    # Step 4: Create MEMBER_OF edges
    member_edges = []
    for node_id in community_nodes:
        edge = CommunityEdge(
            source_node_uuid=node_id,
            target_node_uuid=str(community_node.uuid),
            group_id=f"org:{org_id}:community",
        )
        member_edges.append(edge)
    await CommunityEdge.save_bulk(driver=driver, edges=member_edges)

    logger.info("community.upserted", extra={
        "org_id": str(org_id),
        "community_name": community_name,
        "entity_count": len(community_nodes),
        "community_node_id": str(community_node.uuid),
    })


async def _generate_community_summary(
    llm_client,
    entities: list[dict],
    relationships: list[str],
) -> str:
    """Generate a 3-sentence community summary via LLM.

    The prompt compresses N entity summaries into a concise community description.
    """
    entities_text = "\n".join(
        f"- {e['name']} ({e['type']}): {e['summary']}" for e in entities[:50]
    )
    relationships_text = "\n".join(relationships[:100])

    prompt = COMMUNITY_SUMMARY_PROMPT.format(
        entity_count=len(entities),
        entities=entities_text,
        relationships=relationships_text,
    )

    response = await llm_client.generate_response(
        messages=[{"role": "user", "content": prompt}],
        model_kwargs={"max_tokens": 500, "temperature": 0.3},
    )
    return response.strip()


def _generate_community_name(entity_details: list[dict]) -> str:
    """Generate a community name from entity types.

    Heuristic: use the most common entity type as the community category.
    """
    type_counts = {}
    for e in entity_details:
        t = e["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    if type_counts:
        primary_type = max(type_counts, key=type_counts.get)
        return f"{primary_type} Community ({len(entity_details)} members)"
    return f"Entity Community ({len(entity_details)} members)"
```

---

## 5. LLM Summary Prompt

```python
# packages/core/graphiti/community/prompts.py

COMMUNITY_SUMMARY_PROMPT = """You are an expert at analysing knowledge graphs and writing concise community summaries.

## Input Data

This community contains {entity_count} entities. Here are their names, types, and summaries:

{entities}

Here are the key relationships within this community:

{relationships}

## Task

Write a **3-sentence summary** of this community that captures:
1. What this group of entities represents (the common theme or domain)
2. The key relationships and roles within the community
3. What knowledge or expertise this community collectively holds

Guidelines:
- Write in plain English, as if describing this community to someone reading an LLM context block
- Be specific — mention entity names where relevant
- Do NOT use markdown, bullet points, or lists
- Keep it exactly 3 sentences
- Do not mention that this is an AI-generated summary

Summary:"""
```

---

## 6. Storage Model

### 6.1 Graph Schema

```
┌──────────────┐       MEMBER_OF       ┌──────────────┐
│  EntityNode  │──────────────────────►│ CommunityNode │
│ (Person)     │                       │               │
│ "Alice"      │                       │ "Tech Team   │
└──────────────┘                       │  Community"  │
                                       │               │
┌──────────────┐       MEMBER_OF       │ summary:     │
│  EntityNode  │──────────────────────►│ "This        │
│ (Company)    │                       │  community   │
│ "Acme Corp"  │                       │  represents  │
└──────────────┘                       │  ..."        │
                                       └──────────────┘
┌──────────────┐       MEMBER_OF
│  EntityNode  │──────────────────────►
│ (Product)    │
│ "Pro Plan"   │
└──────────────┘
```

### 6.2 CommunityNode Properties

| Property | Type | Description |
|----------|------|-------------|
| `uuid` | string | Graphiti UUID |
| `name` | string | Auto-generated name (e.g., "Person Community (12 members)") |
| `summary` | string | 3-sentence LLM-generated summary |
| `group_id` | string | `org:{org_id}:community` |
| `created_at` | datetime | Creation timestamp |

### 6.3 CommunityEdge (MEMBER_OF) Properties

| Property | Type | Description |
|----------|------|-------------|
| `uuid` | string | Graphiti UUID |
| `source_node_uuid` | string | EntityNode UUID (member) |
| `target_node_uuid` | string | CommunityNode UUID |
| `group_id` | string | `org:{org_id}:community` |
| `created_at` | datetime | Link creation timestamp |

---

## 7. API Endpoint

### 7.1 Contract

```
GET /v1/users/{user_id}/graph/communities?limit=20&cursor=...

Response 200:
{
  "data": [
    {
      "id": "comm_uuid_1",
      "name": "Tech Team Community (5 members)",
      "summary": "This community represents the engineering team including Alice (Lead Engineer), Bob (Backend), and Carol (DevOps). They work on the Pro Plan product and collaborate with Acme Corp. The team collectively holds knowledge about system architecture, deployment pipelines, and customer infrastructure.",
      "member_count": 5,
      "created_at": "2026-06-05T02:00:00Z"
    }
  ],
  "next_cursor": "...",
  "has_more": false
}
```

### 7.2 Router

```python
# services/api/app/routers/graph.py (add to existing graph router)

@router.get("/communities", response_model=CommunityListResponse)
async def list_communities(
    user_id: str,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    service: EntityService = Depends(get_entity_service),
    org_id: UUID = Depends(get_current_org_id),
) -> CommunityListResponse:
    """List community summary nodes for a user (SRS KG-13)."""
    communities = await service.get_communities(
        org_id=org_id, user_id=user_id, limit=limit, cursor=cursor,
    )
    return CommunityListResponse(
        data=[CommunityResponse.from_domain(c) for c in communities],
        next_cursor=communities.next_cursor,
        has_more=communities.has_more,
    )
```

### 7.3 Schema

```python
# services/api/app/schemas/graph.py (add to existing schemas)

class CommunityResponse(BaseModel):
    """Schema for a community summary node."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    summary: str
    member_count: int = 0
    created_at: datetime

    @classmethod
    def from_domain(cls, node: CommunityNode, member_count: int = 0) -> "CommunityResponse":
        return cls(
            id=str(node.uuid),
            name=node.name,
            summary=node.summary or "",
            member_count=member_count,
            created_at=node.created_at,
        )


class CommunityListResponse(BaseModel):
    """Paginated response for GET /communities."""

    data: list[CommunityResponse]
    next_cursor: str | None = None
    has_more: bool = False
```

---

## 8. Error Handling

### 8.1 Community Detection Timeout

Each org's community detection is bounded by `COMMUNITY_DETECTION_TIMEOUT` (default: 60s):

```python
async def process_org_communities_safe(ctx, org_id: UUID, ...) -> None:
    """Wrapper that enforces per-org timeout."""
    try:
        await asyncio.wait_for(
            process_org_communities(ctx, org_id, ...),
            timeout=settings.COMMUNITY_DETECTION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(
            "community.timed_out",
            extra={
                "org_id": str(org_id),
                "timeout_seconds": settings.COMMUNITY_DETECTION_TIMEOUT,
            },
        )
        # The org will be retried on the next scheduled run
```

### 8.2 LLM Failure on Summary Generation

If the LLM call fails for a community summary (rate limit, timeout, refusal):

```python
# In _process_single_community:
try:
    summary = await _generate_community_summary(...)
except Exception as e:
    logger.warning(
        "community.summary_llm_failed",
        extra={"community_size": len(community_nodes), "error": str(e)},
    )
    # Fallback: use a generic summary
    summary = _generate_fallback_summary(entity_details)
    # The community is still created with the fallback summary;
    # it will be regenerated on the next scheduled run.

def _generate_fallback_summary(entity_details: list[dict]) -> str:
    """Generate a summary without LLM when the LLM call fails."""
    names = [e["name"] for e in entity_details[:5]]
    types = set(e["type"] for e in entity_details)
    return (
        f"Group of {len(entity_details)} entities "
        f"({', '.join(sorted(types))}) "
        f"including {', '.join(names)}."
    )
```

### 8.3 Partial Results

If an LLM call fails for one community, the others are still processed. Failed communities retain their previous summaries (if any) or get a fallback. The `last_community_run` timestamp is still updated so that the successful communities are not regenerated unnecessarily.

---

## 9. Regeneration Strategy

### 9.1 Two Triggers

| Trigger | Condition | Action |
|---------|-----------|--------|
| **Scheduled** (default) | Every 24h at 02:00 UTC | Full run for all eligible orgs |
| **Entity count growth** | Entity count increased by >20% since last run | Trigger on next scheduled run (not immediate — avoids bursts) |

### 9.2 Threshold Check

```python
async def is_regeneration_needed(db, org_id: UUID, entity_count: int) -> bool:
    """Check if community summaries should be regenerated.

    Returns True if:
    - No previous community run exists (first time)
    - Entity count has grown by > COMMUNITY_REGEN_THRESHOLD_PCT since last run
    """
    result = await db.execute(
        sqlalchemy.text(
            "SELECT entity_count, last_community_run FROM entity_graph_metadata WHERE org_id = :org_id"
        ),
        {"org_id": org_id},
    )
    row = result.one_or_none()
    if row is None:
        return True  # no metadata yet
    if row.last_community_run is None:
        return True  # never run

    # Check percentage growth
    old_count = row.entity_count
    if old_count == 0:
        return True
    growth = (entity_count - old_count) / old_count
    return growth > settings.COMMUNITY_REGEN_THRESHOLD_PCT  # default: 0.2
```

### 9.3 Backoff for Stable Orgs

If an org's community structure has not changed significantly (entity count growth < 5%), skip the LLM summarisation step and reuse existing summaries. Only re-run community detection every 7 days for stable orgs.

```python
# Entity count threshold: always re-detect if growth >20%
# LLM summary reuse: if growth < 5%, skip LLM and keep existing summaries
# Community detection rerun: minimum 7-day interval for stable orgs
```

---

## 10. Context Assembly Integration

Community summaries are used in the context assembly endpoint (`GET /context`). When the context is assembled for a query, the most relevant community summaries are included to provide long-context compression.

See [02-context-assembly.md](../03-core-memory/02-context-assembly.md) for detailed integration. The relevant community lookup:

```python
# In context assembly: find communities whose member entities match the query
async def _get_relevant_communities(
    self, user_id: str, query: str, limit: int = 3
) -> list[str]:
    """Get community summaries relevant to the query."""
    # Search community node summaries via Graphiti's community_search
    from graphiti_core.search.search import community_search

    communities, scores = await community_search(
        driver=self._driver,
        llm_client=self._graphiti._llm_client,
        embedder=self._graphiti._embedder,
        group_ids=[f"org:{self._org_id}:community"],
        query=query,
        limit=limit,
    )
    return [c.summary for c in communities if c.summary]
```

---

## 11. Testing Guide

### 11.1 Unit Tests

```python
# tests/unit/test_community_detection.py
"""Test community detection algorithms in isolation."""

import networkx as nx

from packages.core.graphiti.community.algorithms import (
    detect_communities_label_propagation,
)


class TestCommunityDetection:

    def test_label_propagation_two_communities(self):
        """Should detect two separate communities in a disconnected graph."""
        G = nx.Graph()
        # Community A: Alice, Bob, Carol (all connected)
        G.add_edges_from([
            ("alice", "bob", {"fact": "works_with"}),
            ("bob", "carol", {"fact": "works_with"}),
            ("alice", "carol", {"fact": "works_with"}),
        ])
        # Community B: Dave, Eve (connected)
        G.add_edges_from([
            ("dave", "eve", {"fact": "related"}),
        ])

        communities = detect_communities_label_propagation(G)
        assert len(communities) == 2
        # Each community should have >= 2 members (singletons filtered)

    def test_singletons_filtered(self):
        """Isolated nodes with no edges should be filtered out."""
        G = nx.Graph()
        G.add_node("lonely")  # no edges
        communities = detect_communities_label_propagation(G)
        assert len(communities) == 0
```

### 11.2 Integration Tests

```python
# tests/integration/test_community_summarisation.py
"""Test the full community detection pipeline with real Graphiti."""

import pytest


@pytest.mark.asyncio
@pytest.mark.integration
class TestCommunitySummarisation:

    async def test_community_detection_pipeline(self, graphiti: Graphiti, db_session):
        """End-to-end: export graph → detect communities → generate summaries → upsert."""
        org_id = uuid.uuid4()
        # Create some entities and edges via Graphiti
        # ...

        # Run community detection
        await process_org_communities(
            ctx={},
            org_id=org_id,
            graphiti=graphiti,
            db=db_session,
        )

        # Verify CommunityNodes were created
        communities = await CommunityNode.get_by_group_ids(
            driver=graphiti._driver,
            group_ids=[f"org:{org_id}:community"],
        )
        assert len(communities) > 0
        for c in communities:
            assert c.summary is not None
            assert len(c.summary) > 20  # non-trivial summary

    async def test_llm_failure_fallback(self, graphiti: Graphiti, monkeypatch):
        """When LLM summarisation fails, a fallback summary should be used."""
        async def mock_llm_failure(*args, **kwargs):
            raise RuntimeError("LLM API unavailable")

        monkeypatch.setattr(
            "packages.core.graphiti.community.detector._generate_community_summary",
            mock_llm_failure,
        )

        # ... run community detection
        # Verify CommunityNode exists with fallback summary
```

---

## 12. Open Questions

| ID | Question | Impact | Decision / Status |
|----|----------|--------|-------------------|
| COMM-01 | Community detection spans all users in an org — is this correct? | If each agent has completely separate entity graphs, cross-user communities may be meaningless | **Decision:** Community detection is per-org, summing across all users. This matches the use case where multiple agents (or conversations) refer to the same real-world entities. If per-user isolation is needed, scope by user prefix in the Cypher query |
| COMM-02 | NetworkX on graphs with 50k+ nodes may be memory-intensive | Could OOM the worker pod | Add a subgraph limit: sample 10k edges if graph > 50k nodes. Optimize with `nx.algorithms.community.asyn_lpa_communities` (asynchronous, uses less memory) |
| COMM-03 | LLM summary quality depends on entity summary quality | Poor entity summaries → poor community summaries | Ensure entity extraction worker generates high-quality summaries. Add a community summary evaluation metric (LLM-as-judge on a golden dataset) |
| COMM-04 | Community membership changes between runs — how to handle stale MEMBER_OF edges? | Old edges remain, new edges added | Delete all existing MEMBER_OF edges for the org before re-creating. Tracked as part of `_process_single_community` using `CommunityEdge.delete_by_group_ids()` |

---

*Implementation document for SRS §5.3.2 (KG-08), §5.5.5 (NLP-15–NLP-17), §5.7 (WRK-05, WRK-07). Maintained by @senior-dev. Last updated: 2026-06-05.*
