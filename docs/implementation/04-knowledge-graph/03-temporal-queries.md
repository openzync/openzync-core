# Temporal Knowledge Graph Queries — Time-Travel & Bi-Temporal Facts

## 1. Overview

| Field | Detail |
|-------|--------|
| **Coverage** | Bi-temporal data model decision, query patterns for time-travel, fact versioning, temporal filtering in Graphiti, index strategy |
| **Dependencies** | [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md) (facts table DDL), [01-graphiti-setup.md](01-graphiti-setup.md) (Graphiti init), [02-entity-operations.md](02-entity-operations.md) (EntityService) |
| **SRS Requirement IDs** | KG-07 (temporal layer), NLP-05 (fact extraction), NLP-06 (fact storage with valid_from/valid_to), NLP-07 (facts listing), PERF-01–PERF-03 (latency targets) |
| **Build Phase** | Phase 1 (Core Memory) |
| **Design Authority** | @architect for temporal model decision, @senior-dev for query implementation |

### 1.1 What This Doc Covers

- The bi-temporal data model: valid time + transaction time
- Decision: uni-temporal vs true bi-temporal (with audit rationale)
- Concrete query patterns for temporal queries (point-in-time, delta, snapshot)
- Graphiti's built-in temporal support and how to use it
- Fact versioning strategy
- Time-travel query endpoint (`GET /facts?valid_at=...`)
- Composite index design for temporal query performance

---

## 2. Temporal Data Model

### 2.1 Two Time Axes

| Axis | Field | What It Tracks | Set By |
|------|-------|---------------|--------|
| **Valid time** | `valid_from`, `valid_to` | When the fact is/was true in the real world | Caller (business data) or LLM (extracted facts) |
| **Transaction time** | `created_at`, `updated_at`, `invalid_at` | When the fact was recorded / modified in the system | System (server timestamp) |

### 2.2 Decision: Uni-Temporal vs True Bi-Temporal

**Verdict: Uni-temporal for MVP (Phase 1), with schema designed for upgrade to bi-temporal in Phase 2.**

| Approach | Pros | Cons | Verdict |
|----------|------|------|---------|
| **Uni-temporal** (chosen for Phase 1) | Simple queries (`WHERE valid_from <= T AND valid_to > T`). One set of time columns. No complexity around system-time snapshots. | Cannot answer "What did the system believe at time T?" If a fact is corrected, you lose the original. | **Phase 1** — matches Zep CE parity |
| **True bi-temporal** | Full audit trail: every fact correction creates a new transaction-time version. You can query "as of yesterday" and see old facts. | `FOR SYSTEM_TIME AS OF` queries add complexity. Storage doubles. Requires careful index design. | **Phase 2** — differentiate from Zep CE |

**Schema audit finding (SRS §7.1):** The `facts` table already has all bi-temporal columns (`valid_from`, `valid_to`, `invalid_at`, `created_at`), but query patterns in Phase 1 only use valid-time. The `invalid_at` column serves double duty:
- In valid-time context: marks when a fact ceased being true
- In transaction-time context: marks when the system learned the fact was superseded

**Upgrade path to bi-temporal (Phase 2):**

```sql
-- Phase 2: Add system-versioning to the facts table
-- Requires PostgreSQL 13+ with system-versioned temporal tables
-- or manual versioning via a facts_history table.

CREATE TABLE facts_history (LIKE facts INCLUDING ALL);
ALTER TABLE facts ADD COLUMN sys_period tstzrange NOT NULL DEFAULT tstzrange(now(), null);
ALTER TABLE facts ADD COLUMN fact_id UUID NOT NULL DEFAULT gen_random_uuid();
-- Each fact update moves the old row to facts_history
```

For Phase 1, the schema is already capable of bi-temporal — we just don't query the transaction-time axis yet.

---

## 3. Graphiti's Built-in Temporal Support

Graphiti natively supports temporal queries through the `EntityEdge` class:

```python
# EntityEdge temporal fields (from Graphiti source)
class EntityEdge(Edge):
    name: str                          # Relationship type (e.g., "works_at")
    fact: str | None                   # Human-readable fact description
    valid_at: datetime | None          # When the fact became true (valid-from)
    invalid_at: datetime | None        # When the fact became false (valid-to)
    expired_at: datetime | None        # Manual expiry (for scheduled expirations)
    created_at: datetime               # When the edge was created (transaction-time)
```

### 3.1 Automatic Fact Invalidation

When Graphiti processes a new episode and detects a contradiction (e.g., "Alice works at Google" after previously recording "Alice works at Acme"), it automatically sets `invalid_at` on the old `EntityEdge`. This is the **automatic fact invalidation** mechanism.

```python
# Internal flow in Graphiti's resolve_extracted_edges():
# 1. Extract new edges from episode
# 2. For each edge, check if a contradictory edge exists (same subject + predicate, different object)
# 3. If contradiction found: set invalid_at = now on old edge
# 4. Create new edge with valid_at = now
```

### 3.2 Temporal Search Filters

Graphiti's `SearchFilters` supports four DateFilter keys for fine-grained temporal queries:

```python
from graphiti_core.search import SearchFilters, DateFilter
from graphiti_core.search.search_config import ComparisonOperator

filters = SearchFilters()

# What facts were valid on 2026-01-15?
filters.valid_at = [[
    DateFilter(comparison=ComparisonOperator.less_than_equal, value=datetime(2026, 1, 15)),
]]
filters.invalid_at = [[
    DateFilter(comparison=ComparisonOperator.greater_than, value=datetime(2026, 1, 15)),
    DateFilter(comparison=ComparisonOperator.is_null, value=None),  # still valid
]]

# What facts changed after 2026-03-01? (by transaction time)
filters.created_at = [[
    DateFilter(comparison=ComparisonOperator.greater_than_equal, value=datetime(2026, 3, 1)),
]]
```

---

## 4. Query Patterns

### 4.1 Pattern 1: "What facts were true at time T?"

```python
# services/api/app/services/fact_service.py
from datetime import datetime
from uuid import UUID

from graphiti_core.search import SearchFilters, DateFilter
from graphiti_core.search.search_config import ComparisonOperator


async def get_facts_valid_at(
    self,
    org_id: UUID,
    user_id: str,
    valid_at: datetime,
    limit: int = 50,
) -> list[dict]:
    """Return all facts that were true at the given point in time.

    Query logic (translated to SQL for the facts table):
        WHERE valid_from <= T
          AND (valid_to IS NULL OR valid_to > T)
          AND invalid_at IS NULL
        ORDER BY created_at DESC

    For the graph DB, this uses Graphiti's SearchFilters.
    """
    group_id = f"org:{org_id}:user:{user_id}"

    filters = SearchFilters()
    # Edge valid_at <= query time (fact became true before or at T)
    filters.valid_at = [[
        DateFilter(comparison=ComparisonOperator.less_than_equal, value=valid_at),
    ]]
    # Edge invalid_at > query time OR not yet invalidated
    filters.invalid_at = [[
        DateFilter(comparison=ComparisonOperator.greater_than, value=valid_at),
    ], [
        DateFilter(comparison=ComparisonOperator.is_null, value=None),
    ]]

    from graphiti_core.search.search import edge_search
    edges, scores = await edge_search(
        driver=self._driver,
        llm_client=self._graphiti._llm_client,
        embedder=self._graphiti._embedder,
        group_ids=[group_id],
        search_filters=filters,
        limit=limit,
    )
    return edges
```

**Cypher/GQL equivalent** (for direct driver access):

```cypher
MATCH (s:EntityNode)-[r:RELATES_TO]->(t:EntityNode)
WHERE r.group_id = $group_id
  AND r.valid_at <= $query_time
  AND (r.invalid_at IS NULL OR r.invalid_at > $query_time)
RETURN s.name AS subject, r.name AS predicate, t.name AS object, r.fact AS fact
ORDER BY r.created_at DESC
```

### 4.2 Pattern 2: "What facts have changed since time T?"

```python
async def get_facts_changed_since(
    self,
    org_id: UUID,
    user_id: str,
    since: datetime,
) -> list[dict]:
    """Return facts that were created or modified after 'since'.

    This tracks transaction time (when the system recorded the fact), not
    valid time (when the fact was true).

    Use case: "What new information has been added since the last context
    assembly?" — drives incremental context refresh.
    """
    group_id = f"org:{org_id}:user:{user_id}"

    filters = SearchFilters()
    filters.created_at = [[
        DateFilter(comparison=ComparisonOperator.greater_than_equal, value=since),
    ]]

    from graphiti_core.search.search import edge_search
    edges, scores = await edge_search(
        driver=self._driver,
        llm_client=self._graphiti._llm_client,
        embedder=self._graphiti._embedder,
        group_ids=[group_id],
        search_filters=filters,
        limit=100,
    )
    return edges
```

### 4.3 Pattern 3: "What did the system believe at time T?"

**Phase 2 — requires bi-temporal upgrade.**

This query returns the set of facts as they existed in the system at a given point in transaction time. It reconstructs the state before any corrections or updates.

```sql
-- Phase 2: Requires facts_history table or system-versioned table
SELECT content, subject, predicate, object, valid_from, valid_to
FROM facts FOR SYSTEM_TIME AS OF '2026-01-15 10:00:00+00'
WHERE user_id = $1
  AND invalid_at IS NULL;  -- corrections after T already filtered by SYSTEM_TIME
```

Without system-versioned tables, implement manually:

```sql
-- Get the latest version of each fact as it existed at T
SELECT DISTINCT ON (fact_id) *
FROM (
    SELECT *, 'current' AS version FROM facts WHERE created_at <= $T
    UNION ALL
    SELECT *, 'history' AS version FROM facts_history WHERE created_at <= $T
) AS all_versions
ORDER BY fact_id, created_at DESC;
```

---

## 5. Fact Versioning Strategy

### 5.1 Lifecycle of a Fact

```
Time ──────────────────────────────────────────────────────────────►

Event: Alice works at Acme Corp
  valid_from = 2026-01-01, valid_to = null, invalid_at = null
  └── Currently true

Event: Alice leaves Acme, joins Google
  valid_from = 2026-06-01, valid_to = null, invalid_at = null
  └── New fact

OLD fact update:
  valid_from = 2026-01-01, valid_to = 2026-06-01, invalid_at = 2026-06-01
  └── Historical — no longer valid after June 1

NEW fact:
  valid_from = 2026-06-01, valid_to = null, invalid_at = null
  └── Currently true
```

### 5.2 Create vs Supersede Decision

When a new fact contradicts an existing fact, we have two choices:

| Approach | Mechanism | Verdict |
|----------|-----------|---------|
| **Supersede** (chosen) | Set `invalid_at` on old edge, create new edge. Both exist but only the new one is "active." Preserves history for time-travel queries. | **Selected** — enables temporal queries, aligns with Graphiti's native behaviour |
| **In-place update** | Mutate the existing edge's `valid_from`/`valid_to`. Loses history. | Rejected — breaks bi-temporal model |

### 5.3 Implementation in Fact Service

```python
# packages/core/graphiti/fact_service.py
"""Fact management with versioning support."""


async def upsert_fact(
    self,
    org_id: UUID,
    user_id: str,
    subject_id: str,
    predicate: str,
    object_id: str,
    fact_text: str,
    valid_from: datetime | None = None,
) -> EntityEdge:
    """Create or supersede a fact.

    If an active edge with the same (subject, predicate) exists, it is
    invalidated before creating the new one.
    """
    group_id = self._make_group_id(org_id, user_id)

    # Check for existing active edge with same subject + predicate
    existing_edges = await self._find_active_edges(
        group_id=group_id,
        source_node_uuid=subject_id,
        predicate=predicate,
    )

    async with self._driver.transaction() as tx:
        # Supersede existing edges
        for old_edge in existing_edges:
            old_edge.invalid_at = datetime.now(timezone.utc)
            old_edge.expired_at = datetime.now(timezone.utc)
            await old_edge.save(driver=self._driver, transaction=tx)

        # Create new edge
        new_edge = EntityEdge(
            source_node_uuid=subject_id,
            target_node_uuid=object_id,
            name=predicate,
            fact=fact_text,
            group_id=group_id,
            valid_at=valid_from or datetime.now(timezone.utc),
            invalid_at=None,
        )
        await new_edge.save(driver=self._driver, transaction=tx)
        await new_edge.generate_embedding(
            llm_client=self._graphiti._llm_client,
            embedder=self._graphiti._embedder,
        )

    logger.info("fact.upserted", extra={
        "subject": subject_id,
        "predicate": predicate,
        "object": object_id,
        "fact": fact_text,
        "superseded_count": len(existing_edges),
    })
    return new_edge


async def _find_active_edges(
    self,
    group_id: str,
    source_node_uuid: str,
    predicate: str,
) -> list[EntityEdge]:
    """Find active (not invalidated) edges matching subject + predicate."""
    filters = SearchFilters()
    filters.edge_types = [predicate]
    filters.invalid_at = [[
        DateFilter(comparison=ComparisonOperator.is_null, value=None),
    ]]

    from graphiti_core.search.search import edge_search
    edges, scores = await edge_search(
        driver=self._driver,
        llm_client=self._graphiti._llm_client,
        embedder=self._graphiti._embedder,
        group_ids=[group_id],
        search_filters=filters,
        limit=10,
    )
    return [e for e in edges if str(e.source_node_uuid) == source_node_uuid]
```

---

## 6. Time-Travel Query Endpoint

### 6.1 API Contract

```
GET /v1/users/{user_id}/facts?valid_at=2026-01-01T00:00:00Z&limit=50&cursor=...

Response 200:
{
  "data": [
    {
      "id": "uuid",
      "content": "Alice works at Acme Corp",
      "subject": "Alice",
      "predicate": "works_at",
      "object": "Acme Corp",
      "confidence": 0.95,
      "valid_from": "2025-06-01T00:00:00Z",
      "valid_to": "2026-06-01T00:00:00Z",
      "created_at": "2025-06-01T12:00:00Z"
    }
  ],
  "next_cursor": "...",
  "has_more": false
}
```

### 6.2 Router Implementation

```python
# services/api/app/routers/facts.py
from datetime import datetime

from fastapi import APIRouter, Depends, Query

router = APIRouter(prefix="/v1/users/{user_id}/facts", tags=["facts"])


@router.get("")
async def list_facts(
    user_id: str,
    valid_at: datetime | None = Query(None, description="Point-in-time: return facts valid at this timestamp"),
    created_after: datetime | None = Query(None, description="Return facts created after this timestamp"),
    confidence_min: float | None = Query(None, ge=0.0, le=1.0),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    service: FactService = Depends(get_fact_service),
    org_id: UUID = Depends(get_current_org_id),
) -> FactListResponse:
    """List facts for a user with temporal filtering (SRS NLP-07).

    The `valid_at` parameter enables time-travel: return only facts that were
    valid at the given point in time.
    """
    if valid_at:
        facts = await service.get_facts_valid_at(
            org_id=org_id, user_id=user_id, valid_at=valid_at, limit=limit,
        )
    else:
        facts = await service.get_facts(org_id=org_id, user_id=user_id, ...)

    return FactListResponse(data=[FactResponse.from_domain(f) for f in facts])
```

### 6.3 Usage Examples

```python
# "What did I know about Alice on Jan 15?"
GET /v1/users/user_123/facts?valid_at=2026-01-15T00:00:00Z

# "What new facts have been added in the last hour?"
GET /v1/users/user_123/facts?created_after=2026-06-05T09:00:00Z

# "What high-confidence facts are currently true?"
GET /v1/users/user_123/facts?valid_at=2026-06-05T10:00:00Z&confidence_min=0.9
```

---

## 7. PostgreSQL Index Strategy

### 7.1 Composite Indexes for Temporal Queries

```sql
-- Primary index for time-travel queries: user_id + valid_from + valid_to
-- Covers the most common temporal query pattern
CREATE INDEX idx_facts_temporal_lookup
    ON facts (user_id, valid_from DESC, valid_to)
    WHERE invalid_at IS NULL;

-- Index for transaction-time queries (created_after filter)
CREATE INDEX idx_facts_created_at
    ON facts (user_id, created_at DESC);

-- Index for confidence filtering
CREATE INDEX idx_facts_confidence
    ON facts (user_id, confidence DESC);

-- Index for fact subject lookup (entity resolution)
CREATE INDEX idx_facts_subject
    ON facts (user_id, subject)
    WHERE subject IS NOT NULL;
```

### 7.2 Query Analysis

```sql
-- EXPLAIN ANALYZE for the time-travel query:

EXPLAIN ANALYZE
SELECT content, subject, predicate, object, valid_from, valid_to
FROM facts
WHERE user_id = '550e8400-e29b-41d4-a716-446655440000'
  AND valid_from <= '2026-01-15T00:00:00Z'::timestamptz
  AND (valid_to IS NULL OR valid_to > '2026-01-15T00:00:00Z'::timestamptz)
  AND invalid_at IS NULL
ORDER BY created_at DESC
LIMIT 50;

-- Expected: Index Scan using idx_facts_temporal_lookup
-- Filter: valid_to > '2026-01-15' OR valid_to IS NULL
-- This should return in < 5ms for users with < 10k facts
```

### 7.3 FalkorDB Index

FalkorDB (RedisGraph) does not support composite indexes directly. Graphiti handles index creation via `build_indices_and_constraints()`. The key query optimisation is the `group_id` filter which limits the search space to a single user's namespace.

For FalkorDB, add a full-text index on edge facts:

```python
# During Graphiti init, the FalkorDriver creates these indexes:
# FT.CREATE edge_idx ON graph PREFIX 1 "edge:" SCHEMA fact TEXT group_id TAG name TAG
```

This enables efficient full-text search within a group_id scope.

---

## 8. Temporal Edge Cases

### 8.1 Open-Ended Facts

Facts that are currently true have `valid_to = NULL` and `invalid_at = NULL`. The time-travel query handles this with the `OR valid_to IS NULL` clause.

```sql
WHERE valid_from <= $T AND (valid_to IS NULL OR valid_to > $T)
```

### 8.2 Future-Dated Facts

If a caller posts a fact with `valid_from` in the future, it should not appear in time-travel queries until that date. This is handled naturally by the `valid_from <= T` clause.

### 8.3 Concurrent Fact Creation

Two workers processing the same episode could create duplicate edges. Mitigation:

1. **Application-level dedup:** Check for existing edge with same `(source_node_uuid, name, target_node_uuid, group_id)` before creating
2. **Unique constraint:** Graphiti does not support DB-level unique constraints across property combinations. Managed in the `_find_active_edges()` step

```python
# In upsert_fact, before creating:
existing = await self._find_exact_edge(
    group_id=group_id,
    source=subject_id,
    predicate=predicate,
    target=object_id,
    valid_from=valid_from,
)
if existing:
    logger.debug("fact.duplicate_skipped", ...)
    return existing  # no-op
```

### 8.4 Midnight / DST Boundaries

All temporal fields use `TIMESTAMPTZ` (PostgreSQL) / `datetime` with timezone (Python). Store in UTC, convert to the client's timezone at the API layer only. Never use naive datetimes.

```python
# Always use timezone-aware datetimes
from datetime import datetime, timezone

now = datetime.now(timezone.utc)  # correct
naive = datetime.utcnow()          # WRONG — will cause DST/midnight comparison bugs
```

---

## 9. Testing Guide

### 9.1 Temporal Query Tests

```python
# tests/unit/test_temporal_queries.py
"""Temporal query accuracy tests."""

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from packages.core.graphiti.fact_service import FactService


@pytest.mark.asyncio
class TestTemporalQueries:

    async def test_facts_valid_at_excludes_expired(self, fact_service: FactService):
        """Facts with valid_to before query time should be excluded."""
        org_id = uuid.uuid4()
        user_id = "user_1"

        # Create fact valid from Jan 1 to Jan 10
        fact = await fact_service.create_fact(
            org_id=org_id, user_id=user_id,
            subject="Alice", predicate="works_at", object="Acme",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )

        # Query at Jan 15 — should NOT include the fact
        results = await fact_service.get_facts_valid_at(
            org_id=org_id, user_id=user_id,
            valid_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        )
        fact_ids = [r.uuid for r in results]
        assert fact.uuid not in fact_ids

    async def test_facts_valid_at_includes_current(self, fact_service: FactService):
        """Facts with null valid_to should be included."""
        fact = await fact_service.create_fact(
            org_id=org_id, user_id=user_id,
            subject="Bob", predicate="prefers", object="Python",
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=None,  # still current
        )
        results = await fact_service.get_facts_valid_at(
            org_id=org_id, user_id=user_id,
            valid_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        assert fact.uuid in [r.uuid for r in results]

    async def test_fact_supersede_creates_new_version(self, fact_service: FactService):
        """Superseding a fact should invalidate the old and create a new one."""
        # Create original fact
        original = await fact_service.upsert_fact(
            org_id=org_id, user_id=user_id,
            subject_id="alice_uuid", predicate="works_at",
            object_id="acme_uuid", fact_text="Alice works at Acme",
        )
        # Supersede with new fact
        new = await fact_service.upsert_fact(
            org_id=org_id, user_id=user_id,
            subject_id="alice_uuid", predicate="works_at",
            object_id="google_uuid", fact_text="Alice works at Google",
        )

        # Original should now have invalid_at set
        assert original.invalid_at is not None

        # Query at current time — should see only the new fact
        results = await fact_service.get_facts_valid_at(
            org_id=org_id, user_id=user_id,
            valid_at=datetime.now(timezone.utc),
        )
        assert new.uuid in [r.uuid for r in results]
        assert original.uuid not in [r.uuid for r in results]

        # Time-travel to before the change — should see the original
        historical = await fact_service.get_facts_valid_at(
            org_id=org_id, user_id=user_id,
            valid_at=original.created_at + timedelta(seconds=1),
        )
        assert original.uuid in [r.uuid for r in historical]
```

### 9.2 Performance Test

```python
# tests/perf/test_temporal_query_latency.py
"""Verify temporal queries meet latency targets."""

@pytest.mark.asyncio
@pytest.mark.perf
async def test_temporal_query_p99_latency(fact_service: FactService):
    """Temporal query on a user with 10k facts should return in < 100ms."""
    # Seed 10k facts
    for i in range(10000):
        await fact_service.create_fact(...)

    # Time travel query
    import time
    start = time.monotonic()
    results = await fact_service.get_facts_valid_at(
        org_id=org_id, user_id=user_id,
        valid_at=datetime.now(timezone.utc),
    )
    duration_ms = (time.monotonic() - start) * 1000

    assert duration_ms < 100, f"p99 temporal query took {duration_ms:.0f}ms (target: <100ms)"
```

---

## 10. Open Questions

| ID | Question | Impact | Decision / Status |
|----|----------|--------|-------------------|
| TEMP-01 | Should `invalid_at` represent valid-time expiry, transaction-time supersession, or both? | Correctness of historical queries | **Phase 1:** `invalid_at` = valid-time expiry. **Phase 2:** Split into `valid_to` (valid-time) and `superseded_at` (transaction-time) |
| TEMP-02 | PostgreSQL `FOR SYSTEM_TIME AS OF` requires table creation with `WITH SYSTEM VERSIONING` | Migration complexity for Phase 2 bi-temporal upgrade | Acceptable — will require a data migration in Phase 2. Schema already has the columns |
| TEMP-03 | Graphiti's automatic fact invalidation may not cover all our temporal query patterns | Some edges may not have `invalid_at` set correctly | Monitor fact accuracy metrics in Grafana. Add a validation worker that checks for edges that should have been invalidated |

---

*Implementation document for SRS §5.3.2 (KG-07), §5.5.2 (NLP-05–NLP-07). Maintained by @senior-dev. Last updated: 2026-06-05.*
