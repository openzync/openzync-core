# Hybrid Retrieval Engine — Implementation Guide

> **Domain:** Core Memory
> **SRS Phase:** Phase 1 — Core Memory (Week 3)
> **Requirements:** RET-01 through RET-06, CTX-03, CTX-05, PERF-03
> **Doc Dependencies:** [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md), [03-embedding-strategy.md](../01-data-models/03-embedding-strategy.md)

---

## 1. Overview

The Hybrid Retrieval Engine is the core search infrastructure behind OpenZep's context assembly and search endpoints. It combines three retrieval methods — **vector similarity**, **BM25 full-text**, and **graph BFS traversal** — merged via **Reciprocal Rank Fusion (RRF)** to produce a single ranked result list.

### 1.1 Why Three Retrieval Paths?

| Method | Strengths | Weaknesses |
|--------|-----------|------------|
| **Vector similarity** (pgvector cosine) | Semantic matching: "coding preferences" matches "likes Python" | Misses exact keyword matches; requires embedding generation |
| **BM25 full-text** (PostgreSQL GIN) | Exact keyword matching; zero latency for embedding; works for rare terms | No semantic understanding; "car" won't match "vehicle" |
| **Graph BFS traversal** (Graphiti) | Entity-aware: finds facts about related entities without semantic search | Limited to graph neighbourhood; doesn't scale to deep traversal |

### 1.2 RRF Formula

RRF merges the ranked lists from all three sources:

```
score(d) = SUM over all sources s of [ 1 / (60 + rank_s(d)) ]
```

Where:
- `rank_s(d)` is the rank of document `d` in source `s`'s result list (1-indexed)
- `60` is the constant `k` that dampens the impact of high ranks
- Documents not returned by a source get `rank_s(d) = ∞` → contribution = 0

**Why k=60?** Standard RRF uses k=60. Lower k gives more weight to top-ranked items. Higher k smooths the score distribution. For our use case (mixing dense vector scores with sparse BM25 scores), k=60 provides good balance.

---

## 2. HybridRetriever Class

Located in `services/api/services/hybrid_retriever.py`.

### 2.1 Class Definition

```python
import asyncio
import logging
import math
from typing import Any

import httpx
from redis import asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)


class HybridRetriever:
    """Engine that runs vector, BM25, and graph search in parallel and merges results with RRF."""

    def __init__(
        self,
        db: AsyncSession,
        graphiti: Any,  # Graphiti client instance
        org_id: str,
        redis: aioredis.Redis | None = None,
    ) -> None:
        self._db = db
        self._graphiti = graphiti
        self._org_id = org_id
        self._redis = redis
        self._rrf_k = 60  # RRF constant

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    async def hybrid_search(
        self,
        query: str,
        user_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Run all three retrieval methods in parallel and merge with RRF.

        Args:
            query: The natural language query string.
            user_id: Internal UUID of the user.
            limit: Maximum number of items in the final merged result.

        Returns:
            Dict with keys: entities, facts, communities, episodes,
            source_counts, total_items.
        """
        per_source_limit = limit * 2  # Fetch 2x limit per-source for RRF to re-rank

        # ── Step 1: Preprocess query ──
        cleaned_query, is_entity_name = self._preprocess(query)

        # ── Step 2: Embed query for vector search ──
        query_embedding = await self._embed_query(cleaned_query)

        # ── Step 3: Run all three searches in parallel ──
        vector_task = self.vector_search(
            query_embedding=query_embedding,
            user_id=user_id,
            limit=per_source_limit,
        )
        bm25_task = self.bm25_search(
            query=cleaned_query,
            user_id=user_id,
            limit=per_source_limit,
        )
        graph_task = self.graph_search(
            user_id=user_id,
            query=cleaned_query,
            is_entity_name=is_entity_name,
            limit=per_source_limit,
        )

        vector_results, bm25_results, graph_results = await asyncio.gather(
            vector_task, bm25_task, graph_task,
            return_exceptions=True,
        )

        # ── Step 4: Handle partial failures ──
        # Each method returns a list of items with a 'rank' field or raises
        results: dict[str, list[dict]] = {
            "episodes": [],
            "facts": [],
            "entities": [],
            "communities": [],
        }

        if isinstance(vector_results, Exception):
            logger.warning("Vector search failed", extra={"error": str(vector_results)})
            vector_results = []
        if isinstance(bm25_results, Exception):
            logger.warning("BM25 search failed", extra={"error": str(bm25_results)})
            bm25_results = []
        if isinstance(graph_results, Exception):
            logger.warning("Graph search failed", extra={"error": str(graph_results)})
            graph_results = []

        # ── Step 5: RRF merge ──
        merged = self.rrf_merge(
            sources={
                "vector": vector_results,
                "bm25": bm25_results,
                "graph": graph_results,
            },
            limit=limit,
        )

        # ── Step 6: Organise by type ──
        source_counts = {"graph": 0, "vector": 0, "bm25": 0}
        for item in merged:
            item_type = item.get("type", "episode")
            source = item.get("_source", "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1

            if item_type == "entity":
                results["entities"].append(item)
            elif item_type == "fact":
                results["facts"].append(item)
            elif item_type == "community":
                results["communities"].append(item)
            else:
                results["episodes"].append(item)

        results["source_counts"] = {
            "graph": source_counts.get("graph", 0),
            "vector": source_counts.get("vector", 0),
            "bm25": source_counts.get("bm25", 0),
        }
        results["total_items"] = len(merged)

        return results

    async def vector_only_search(
        self,
        query: str,
        user_id: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Degraded-mode search: vector similarity only.

        Used as fallback when graph traversal or BM25 fails.
        """
        cleaned_query, _ = self._preprocess(query)
        query_embedding = await self._embed_query(cleaned_query)
        vector_results = await self.vector_search(
            query_embedding=query_embedding,
            user_id=user_id,
            limit=limit,
        )

        if isinstance(vector_results, Exception):
            raise vector_results

        return {
            "entities": [r for r in vector_results if r.get("type") == "entity"],
            "facts": [r for r in vector_results if r.get("type") == "fact"],
            "communities": [r for r in vector_results if r.get("type") == "community"],
            "episodes": [r for r in vector_results if r.get("type") == "episode"],
            "source_counts": {"graph": 0, "vector": len(vector_results), "bm25": 0},
            "total_items": len(vector_results),
        }
```

### 2.2 Query Preprocessing

```python
    @staticmethod
    def _preprocess(query: str) -> tuple[str, bool]:
        """Preprocess query for all retrieval methods.

        Returns (cleaned_query, is_entity_name_query).
        """
        import re

        STOPWORDS = {
            "a", "an", "the", "is", "are", "was", "were", "be", "been",
            "in", "on", "at", "to", "for", "of", "with", "and", "or", "but",
            "it", "its", "this", "that", "these", "those",
            "do", "does", "did", "done", "doing",
            "have", "has", "had", "having",
            "can", "could", "will", "would", "shall", "should", "may", "might",
        }

        lower = query.lower().strip()
        # Remove punctuation except spaces
        cleaned = re.sub(r'[^\w\s]', ' ', lower)
        tokens = [t for t in cleaned.split() if t not in STOPWORDS and len(t) > 1]
        cleaned_query = " ".join(tokens)

        # Heuristic entity-name detection
        # Entity names: 1-3 tokens, first letter capitalised in original query,
        # no wh-question words
        original_tokens = query.strip().split()
        is_entity = (
            len(original_tokens) <= 3
            and original_tokens[0][0].isupper()
            and not any(
                w in lower for w in
                {"what", "how", "why", "when", "where", "who", "tell", "give", "find", "show"}
            )
        )

        return cleaned_query, is_entity
```

### 2.3 Query Embedding

```python
    async def _embed_query(self, query: str) -> list[float]:
        """Generate embedding for the query string.

        Uses the configured embedding API (OpenAI, Ollama, or Azure).
        """
        # Check embedding cache first (query embedding cache TTL: 1 hour)
        if self._redis:
            cache_key = f"emb:query:{hashlib.sha256(query.encode()).hexdigest()}"
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)

        # Call embedding API
        embedding = await self._call_embedding_api(query)

        # Cache
        if self._redis:
            await self._redis.setex(cache_key, 3600, json.dumps(embedding))

        return embedding

    async def _call_embedding_api(self, text: str) -> list[float]:
        """Call the configured embedding API.

        Supports: OpenAI / Azure OpenAI / Ollama
        Configured via EMBEDDING_BACKEND and EMBEDDING_MODEL env vars.
        """
        backend = settings.EMBEDDING_BACKEND  # "openai", "azure", "ollama"
        model = settings.EMBEDDING_MODEL  # e.g. "text-embedding-3-small"

        if backend == "ollama":
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{settings.OLLAMA_BASE_URL}/api/embeddings",
                    json={"model": model, "prompt": text},
                )
                resp.raise_for_status()
                return resp.json()["embedding"]

        # OpenAI / Azure
        # Uses the langchain or openai client; simplified here for illustration
        # In production, use the configured LLM client
        openai_client = settings.get_openai_client()
        resp = await openai_client.embeddings.create(
            model=model,
            input=text,
            dimensions=settings.EMBEDDING_DIM,
        )
        return resp.data[0].embedding
```

---

## 3. Vector Search

### 3.1 SQL Query

The vector search uses pgvector's `<=>` (cosine distance) operator on the `episodes` and `facts` tables.

```python
    async def vector_search(
        self,
        query_embedding: list[float],
        user_id: str,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Run vector similarity search across episodes and facts tables.

        Uses pgvector IVFFlat index with cosine distance.

        Query plan:
            1. Search episodes by embedding similarity
            2. Search facts by embedding similarity
            3. Merge and return top `limit` results
        """
        embedding_str = f"[{','.join(str(v) for v in query_embedding)}]"

        # ── Episode vector search ──
        episode_sql = text("""
            SELECT
                e.id AS item_id,
                'episode' AS type,
                e.content,
                e.role,
                e.created_at,
                s.external_id AS session_id,
                1 - (e.embedding <=> :embedding::vector) AS score
            FROM episodes e
            JOIN sessions s ON s.id = e.session_id
            WHERE e.user_id = :user_id
              AND e.embedding IS NOT NULL
            ORDER BY e.embedding <=> :embedding::vector
            LIMIT :limit
        """)

        episode_result = await self._db.execute(
            episode_sql,
            {
                "embedding": embedding_str,
                "user_id": user_id,
                "limit": limit,
            },
        )

        episodes = [
            {
                "id": str(row.item_id),
                "type": row.type,
                "content": row.content,
                "role": row.role,
                "session_id": row.session_id,
                "created_at": str(row.created_at),
                "score": float(row.score),
                "_source": "vector",
            }
            for row in episode_result.fetchall()
        ]

        # ── Fact vector search ──
        fact_sql = text("""
            SELECT
                f.id AS item_id,
                'fact' AS type,
                f.content,
                f.confidence,
                f.valid_from,
                f.valid_to,
                f.subject,
                f.predicate,
                f.object,
                1 - (f.embedding <=> :embedding::vector) AS score
            FROM facts f
            WHERE f.user_id = :user_id
              AND f.embedding IS NOT NULL
              AND (f.invalid_at IS NULL OR f.invalid_at > NOW())
            ORDER BY f.embedding <=> :embedding::vector
            LIMIT :limit
        """)

        fact_result = await self._db.execute(
            fact_sql,
            {
                "embedding": embedding_str,
                "user_id": user_id,
                "limit": limit,
            },
        )

        facts = [
            {
                "id": str(row.item_id),
                "type": row.type,
                "content": row.content,
                "confidence": float(row.confidence) if row.confidence else 1.0,
                "subject": row.subject,
                "predicate": row.predicate,
                "object": row.object,
                "valid_from": str(row.valid_from) if row.valid_from else None,
                "valid_to": str(row.valid_to) if row.valid_to else None,
                "score": float(row.score),
                "_source": "vector",
            }
            for row in fact_result.fetchall()
        ]

        # ── Merge and sort ──
        combined = episodes + facts
        combined.sort(key=lambda x: x["score"], reverse=True)

        return combined[:limit]
```

### 3.2 Index Strategy

```sql
-- On episodes.embedding (VECTOR(1536)):
-- IVFFlat with 100 lists. 100 is appropriate for ~100K-1M rows.
-- For >1M rows, increase lists to 200-500 or switch to HNSW.
CREATE INDEX IF NOT EXISTS idx_episodes_embedding
    ON episodes USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- On facts.embedding:
CREATE INDEX IF NOT EXISTS idx_facts_embedding
    ON facts USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Re-indexing schedule:
-- Weekly maintenance window: REINDEX INDEX CONCURRENTLY idx_episodes_embedding;
-- Or: monitor index quality via pgvector's ivfflat_probes parameter
```

**Probe parameter tuning:** Set `ivfflat.probes` per-session or globally to balance recall vs. speed:

```sql
-- Higher probes = better recall, slower queries
SET ivfflat.probes = 10;  -- Default: 1. Recommended: 10-100 for production.
```

---

## 4. BM25 Search

### 4.1 SQL Query

```python
    async def bm25_search(
        self,
        query: str,
        user_id: str,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Run BM25 full-text search on episodes and facts tables.

        Uses PostgreSQL's built-in `ts_rank` with GIN-indexed
        `to_tsvector` columns.

        The GIN index is defined as:
            CREATE INDEX ON episodes USING GIN (to_tsvector('english', content));
            CREATE INDEX ON facts USING GIN (to_tsvector('english', content));
        """
        if not query.strip():
            return []

        # ── Episode BM25 search ──
        episode_sql = text("""
            SELECT
                e.id AS item_id,
                'episode' AS type,
                e.content,
                e.role,
                e.created_at,
                s.external_id AS session_id,
                ts_rank(
                    to_tsvector('english', e.content),
                    plainto_tsquery('english', :query)
                ) AS rank
            FROM episodes e
            JOIN sessions s ON s.id = e.session_id
            WHERE e.user_id = :user_id
              AND to_tsvector('english', e.content) @@ plainto_tsquery('english', :query)
            ORDER BY rank DESC
            LIMIT :limit
        """)

        episode_result = await self._db.execute(
            episode_sql,
            {"query": query, "user_id": user_id, "limit": limit},
        )

        episodes = [
            {
                "id": str(row.item_id),
                "type": row.type,
                "content": row.content,
                "role": row.role,
                "session_id": row.session_id,
                "created_at": str(row.created_at),
                "rank": float(row.rank),
                "_source": "bm25",
            }
            for row in episode_result.fetchall()
        ]

        # ── Fact BM25 search ──
        fact_sql = text("""
            SELECT
                f.id AS item_id,
                'fact' AS type,
                f.content,
                f.confidence,
                f.subject,
                f.predicate,
                f.object,
                ts_rank(
                    to_tsvector('english', f.content),
                    plainto_tsquery('english', :query)
                ) AS rank
            FROM facts f
            WHERE f.user_id = :user_id
              AND to_tsvector('english', f.content) @@ plainto_tsquery('english', :query)
              AND (f.invalid_at IS NULL OR f.invalid_at > NOW())
            ORDER BY rank DESC
            LIMIT :limit
        """)

        fact_result = await self._db.execute(
            fact_sql,
            {"query": query, "user_id": user_id, "limit": limit},
        )

        facts = [
            {
                "id": str(row.item_id),
                "type": row.type,
                "content": row.content,
                "confidence": float(row.confidence) if row.confidence else 1.0,
                "subject": row.subject,
                "predicate": row.predicate,
                "object": row.object,
                "rank": float(row.rank),
                "_source": "bm25",
            }
            for row in fact_result.fetchall()
        ]

        # ── Merge and sort ──
        combined = episodes + facts
        combined.sort(key=lambda x: x["rank"], reverse=True)

        return combined[:limit]
```

### 4.2 Index Strategy

```sql
-- GIN index on tsvector for BM25 full-text search
CREATE INDEX IF NOT EXISTS idx_episodes_content_gin
    ON episodes USING GIN (to_tsvector('english', content));

CREATE INDEX IF NOT EXISTS idx_facts_content_gin
    ON facts USING GIN (to_tsvector('english', content));
```

---

## 5. Graph Search (BFS Traversal)

### 5.1 Method

```python
    async def graph_search(
        self,
        user_id: str,
        query: str,
        is_entity_name: bool = False,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Run BFS graph traversal from the user's entity node.

        Starting from the user's `EntityNode`, traverse edges up to
        `max_depth` collecting:
        - Entity summaries (for entity type results)
        - Facts (fact triples connected via RELATES_TO edges)
        - Community summaries (via MEMBER_OF edges)

        Args:
            user_id: Internal UUID of the user.
            query: The cleaned query string. Used for optional
                   node-level scoring (if query matches entity name).
            is_entity_name: If True, boost exact name matches.
            limit: Maximum number of results.
        """
        # Get the user's entity node ID from Graphiti
        # The user entity node is created during user creation
        user_node = await self._graphiti.get_entity_node(
            org_id=self._org_id,
            user_id=user_id,
        )
        if user_node is None:
            logger.warning("No user entity node found for graph search",
                           extra={"org_id": self._org_id, "user_id": user_id})
            return []

        # BFS traversal starting from user entity node
        # Depth 2: direct neighbours + neighbours-of-neighbours
        traversed = await self._graphiti.traverse(
            node_id=user_node["uuid"],
            max_depth=settings.CONTEXT_BFS_MAX_DEPTH,  # default 2
            edge_types=["RELATES_TO", "HAS_EPISODE", "MEMBER_OF"],
        )

        results: list[dict[str, Any]] = []

        for node in traversed.get("nodes", []):
            item: dict[str, Any] = {
                "id": node.get("uuid"),
                "_source": "graph",
            }

            if node.get("label") == "EntityNode":
                item["type"] = "entity"
                item["name"] = node.get("name", "")
                item["summary"] = node.get("summary", "")
                item["entity_type"] = node.get("type", "Unknown")
                item["fact_count"] = node.get("fact_count", 0)
                # Boost score if entity name matches query
                if is_entity_name and query.lower() in node.get("name", "").lower():
                    item["score"] = 1.0
                else:
                    item["score"] = node.get("score", 0.5)

                # Truncate entity summary to prevent context bloat
                summary = item.get("summary", "")
                if len(summary) > 500:
                    item["summary"] = summary[:497] + "..."

            elif node.get("label") == "EpisodicNode":
                item["type"] = "episode"
                item["content"] = node.get("content", "")
                item["role"] = node.get("source", "unknown")
                item["created_at"] = node.get("created_at")
                item["score"] = 0.3  # Lower default score for episodes from graph

            elif node.get("label") == "CommunityNode":
                item["type"] = "community"
                item["name"] = node.get("name", "")
                item["summary"] = node.get("summary", "")
                item["score"] = 0.4

                summary = item.get("summary", "")
                if len(summary) > 1000:
                    item["summary"] = summary[:997] + "..."

            else:
                continue  # Skip unknown node types

            results.append(item)

        # Sort by score descending (entities first, then communities, then episodes)
        results.sort(key=lambda x: x.get("score", 0), reverse=True)

        return results[:limit]
```

### 5.2 Edge Types and Their Meaning

| Relationship Type | Source → Target | Meaning |
|-------------------|----------------|---------|
| `RELATES_TO` | EntityNode → EntityNode | Two entities are related by a fact. Edge property `fact` contains the relationship description. |
| `HAS_EPISODE` | EntityNode → EpisodicNode | Entity appears in this conversation episode. |
| `MEMBER_OF` | EntityNode → CommunityNode | Entity belongs to this community cluster. |

### 5.3 Node Scoring

Graphiti nodes do not have a native relevance score by default. We assign heuristic scores:

| Node Type | Default Score | Boost Condition |
|-----------|---------------|-----------------|
| EntityNode | 0.5 | 1.0 if entity name matches query (entity-name queries only) |
| EpisodicNode | 0.3 | — |
| CommunityNode | 0.4 | — |

These scores are used solely for sorting within the graph source. The RRF merge step re-ranks across all sources, so the absolute values matter less than the relative ordering.

---

## 6. RRF Merge

### 6.1 Method

```python
    def rrf_merge(
        self,
        sources: dict[str, list[dict[str, Any]]],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Merge ranked lists from multiple sources using Reciprocal Rank Fusion.

        Each item in each source list must have an identifier (we use 'id').
        Items are deduplicated by (id, type) across sources.

        Args:
            sources: Dict mapping source name (e.g. "vector", "bm25", "graph")
                     to a list of dicts, each with at least 'id' and 'type' keys.
            limit: Maximum number of items in the merged result.

        Returns:
            Merged list sorted by RRF score descending.
        """
        # Build a map of (item_id, item_type) -> accumulated RRF score + item data
        score_map: dict[tuple[str, str], dict[str, Any]] = {}

        for source_name, items in sources.items():
            for rank, item in enumerate(items, start=1):
                key = (item["id"], item["type"])

                if key not in score_map:
                    score_map[key] = {**item, "rrf_score": 0.0}

                # RRF contribution
                score_map[key]["rrf_score"] += 1.0 / (self._rrf_k + rank)

                # Track which sources contributed
                if "_sources" not in score_map[key]:
                    score_map[key]["_sources"] = []
                score_map[key]["_sources"].append(source_name)

        # Sort by RRF score descending
        sorted_items = sorted(
            score_map.values(),
            key=lambda x: x["rrf_score"],
            reverse=True,
        )

        return sorted_items[:limit]
```

### 6.2 RRF Example

Given three source lists:

| Rank | Vector | BM25 | Graph |
|------|--------|------|-------|
| 1 | A (fact) | C (episode) | B (entity) |
| 2 | B (entity) | A (fact) | A (fact) |
| 3 | D (entity) | — | E (fact) |

RRF scores:

| Item | Vector contrib | BM25 contrib | Graph contrib | Total RRF |
|------|---------------|--------------|---------------|-----------|
| A | 1/(60+1)=0.0164 | 1/(60+2)=0.0161 | 1/(60+3)=0.0159 | **0.0484** |
| B | 1/(60+2)=0.0161 | — | 1/(60+1)=0.0164 | **0.0325** |
| C | — | 1/(60+1)=0.0164 | — | **0.0164** |
| D | 1/(60+3)=0.0159 | — | — | **0.0159** |
| E | — | — | 1/(60+3)=0.0159 | **0.0159** |

Result: A, B, C, D, E — A wins because it appears in all three sources.

---

## 7. Index Strategy Summary

### 7.1 All Indexes

```sql
-- Table: episodes
CREATE INDEX idx_episodes_user_id ON episodes (user_id);

-- Vector similarity (IVFFlat cosine)
CREATE INDEX idx_episodes_embedding ON episodes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- BM25 full-text (GIN on tsvector)
CREATE INDEX idx_episodes_content_tsv ON episodes USING GIN (to_tsvector('english', content));

-- Table: facts
CREATE INDEX idx_facts_user_id ON facts (user_id);

-- Vector similarity
CREATE INDEX idx_facts_embedding ON facts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- BM25 full-text
CREATE INDEX idx_facts_content_tsv ON facts USING GIN (to_tsvector('english', content));

-- Temporal filter: only active facts
CREATE INDEX idx_facts_validity ON facts (user_id, invalid_at) WHERE invalid_at IS NULL;
```

### 7.2 Index Maintenance

| Index Type | Maintenance | Frequency |
|------------|-------------|-----------|
| IVFFlat | Re-index (REINDEX CONCURRENTLY) | Weekly |
| GIN | Auto-maintained (no manual rebuild needed) | — |
| B-tree | Auto-maintained | — |

**IVFFlat re-index script (cron job):**

```sql
-- Run during low-traffic window
SET maintenance_work_mem = '2GB';  -- Increase for faster re-index
REINDEX INDEX CONCURRENTLY idx_episodes_embedding;
REINDEX INDEX CONCURRENTLY idx_facts_embedding;
```

---

## 8. Configurable Weights (Optional, P1)

The RRF merge supports optional per-source weights for tuning:

```python
    def rrf_merge_weighted(
        self,
        sources: dict[str, list[dict[str, Any]]],
        weights: dict[str, float] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """RRF merge with configurable per-source weights.

        Default weights: equal (1.0 each).

        Example:
            weights = {"vector": 2.0, "bm25": 1.0, "graph": 1.5}
        """
        if weights is None:
            weights = {name: 1.0 for name in sources}

        score_map: dict[tuple[str, str], dict[str, Any]] = {}

        for source_name, items in sources.items():
            weight = weights.get(source_name, 1.0)
            for rank, item in enumerate(items, start=1):
                key = (item["id"], item["type"])
                if key not in score_map:
                    score_map[key] = {**item, "rrf_score": 0.0}
                score_map[key]["rrf_score"] += weight / (self._rrf_k + rank)

        sorted_items = sorted(
            score_map.values(),
            key=lambda x: x["rrf_score"],
            reverse=True,
        )

        return sorted_items[:limit]
```

This is NOT exposed in P0. We start with equal weights and tune based on retrieval quality metrics.

---

## 9. Cross-Encoder Re-Ranker (P2, RET-05)

The cross-encoder re-ranker is **not part of the synchronous retrieval path**. It runs as an async enrichment step that updates RRF scores in the background.

```python
# P2 — Not in Phase 1
class CrossEncoderReRanker:
    """Optional post-processing step that re-ranks RRF results using a
    cross-encoder model (e.g., ms-marco-MiniLM-L6-v2).

    The cross-encoder computes a relevance score for each (query, item) pair,
    which is more accurate than embedding cosine similarity but too slow
    for real-time use (50-200ms per item).

    When enabled (P2), it runs async and updates cached RRF scores.
    """

    MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L6-v2"
    BATCH_SIZE = 16

    def __init__(self):
        # Loaded lazily to avoid import on startup
        self._model = None

    async def rerank(self, query: str, items: list[dict], limit: int) -> list[dict]:
        # Load model (first call only)
        if self._model is None:
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.MODEL_NAME)

        pairs = [(query, item["content"]) for item in items]

        # Run in executor to avoid blocking event loop
        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            None,
            lambda: self._model.predict(pairs, batch_size=self.BATCH_SIZE),
        )

        for item, score in zip(items, scores):
            item["cross_encoder_score"] = float(score)

        items.sort(key=lambda x: x["cross_encoder_score"], reverse=True)
        return items[:limit]
```

---

## 10. Performance Budget

See [02-context-assembly.md](02-context-assembly.md#5-latency-budget) for the full latency budget breakdown.

Key measurements for the hybrid retrieval engine:

| Component | Measured | Budget | Notes |
|-----------|----------|--------|-------|
| Vector search | 150-200ms | 200ms | pgvector IVFFlat with 10 probes |
| BM25 search | 50-100ms | 100ms | GIN index, plainto_tsquery |
| Graph search | 200-300ms | 300ms | BFS depth=2, Graphiti call |
| RRF merge | 3-5ms | 5ms | In-memory sort of ≤120 items |
| Embedding query | 50-150ms | 100ms | API call to embedding backend |
| **Total retrieval** | **453-755ms** | **705ms** | Within 1500ms cold target |

---

## 11. Testing Guide

### 11.1 Unit Tests

```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_rrf_merge_equal_weights():
    """Verify RRF merge produces correct ordering with equal weights."""
    retriever = HybridRetriever(
        db=MagicMock(),
        graphiti=MagicMock(),
        org_id="org_abc",
    )

    sources = {
        "vector": [
            {"id": "A", "type": "fact", "content": "Fact A"},
            {"id": "B", "type": "fact", "content": "Fact B"},
        ],
        "bm25": [
            {"id": "C", "type": "episode", "content": "Episode C"},
            {"id": "A", "type": "fact", "content": "Fact A"},
        ],
        "graph": [
            {"id": "A", "type": "fact", "content": "Fact A"},
            {"id": "D", "type": "entity", "name": "Entity D"},
        ],
    }

    merged = retriever.rrf_merge(sources, limit=5)

    # A appears in all 3 sources → highest score
    assert merged[0]["id"] == "A"
    assert merged[0]["rrf_score"] > 0.04

    # B and C appear in 1 source each
    assert len(merged) == 4  # A, B, C, D


@pytest.mark.asyncio
async def test_rrf_deduplication():
    """Verify items with same (id, type) are deduplicated across sources."""
    retriever = HybridRetriever(
        db=MagicMock(),
        graphiti=MagicMock(),
        org_id="org_abc",
    )

    sources = {
        "vector": [{"id": "X", "type": "fact", "content": "X"}],
        "bm25": [{"id": "X", "type": "fact", "content": "X"}],
    }

    merged = retriever.rrf_merge(sources, limit=10)

    assert len(merged) == 1
    assert merged[0]["id"] == "X"
    assert set(merged[0]["_sources"]) == {"vector", "bm25"}


@pytest.mark.asyncio
async def test_vector_search_empty_results():
    """Verify vector search returns empty list when no embeddings exist."""
    db_mock = AsyncMock()
    db_mock.execute.return_value.fetchall.return_value = []

    retriever = HybridRetriever(
        db=db_mock,
        graphiti=MagicMock(),
        org_id="org_abc",
    )

    results = await retriever.vector_search(
        query_embedding=[0.1] * 1536,
        user_id="user_123",
        limit=20,
    )

    assert results == []


@pytest.mark.asyncio
async def test_graph_search_no_user_node():
    """Verify graph search returns empty list when user has no entity node."""
    graphiti_mock = MagicMock()
    graphiti_mock.get_entity_node = AsyncMock(return_value=None)

    retriever = HybridRetriever(
        db=MagicMock(),
        graphiti=graphiti_mock,
        org_id="org_abc",
    )

    results = await retriever.graph_search(
        user_id="user_123",
        query="test",
    )

    assert results == []


@pytest.mark.asyncio
async def test_query_preprocessing():
    """Verify query preprocessing produces expected output."""
    retriever = HybridRetriever(
        db=MagicMock(),
        graphiti=MagicMock(),
        org_id="org_abc",
    )

    # Normal query
    cleaned, is_entity = retriever._preprocess("What is Alice working on?")
    assert "Alice working" in cleaned
    assert is_entity is False

    # Entity-name query
    cleaned, is_entity = retriever._preprocess("Alice")
    assert cleaned == "alice"  # lowercased
    assert is_entity is True  # single word, capitalised, no wh-word

    # Entity-name query with qualifier
    cleaned, is_entity = retriever._preprocess("Acme Corp pricing")
    assert is_entity is True
```

### 11.2 Integration Tests

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_hybrid_search_returns_results(
    db_session: AsyncSession,
    graphiti_client: Any,
    seed_user_with_memory: dict,
):
    """Verify hybrid search returns items from all three sources."""
    retriever = HybridRetriever(
        db=db_session,
        graphiti=graphiti_client,
        org_id=seed_user_with_memory["org_id"],
    )

    results = await retriever.hybrid_search(
        query="programming",
        user_id=seed_user_with_memory["id"],
        limit=20,
    )

    assert results["total_items"] > 0
    # At least some sources should have returned items
    total_sources = sum(1 for v in results["source_counts"].values() if v > 0)
    assert total_sources >= 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bm25_index_works(
    db_session: AsyncSession,
    seed_user_with_memory: dict,
):
    """Verify BM25 full-text search returns expected results."""
    retriever = HybridRetriever(
        db=db_session,
        graphiti=MagicMock(),
        org_id=seed_user_with_memory["org_id"],
    )

    # Search for a term that exists in seed data
    results = await retriever.bm25_search(
        query="Python",
        user_id=seed_user_with_memory["id"],
        limit=10,
    )

    # Should find at least the episodes containing "Python"
    assert len(results) > 0
    assert any("Python" in r["content"] for r in results)
```

### 11.3 Benchmarking

```python
@pytest.mark.asyncio
@pytest.mark.benchmark
async def test_hybrid_search_latency(
    db_session: AsyncSession,
    graphiti_client: Any,
    seed_user_with_memory: dict,
):
    """Benchmark hybrid search latency against budget."""
    import time

    retriever = HybridRetriever(
        db=db_session,
        graphiti=graphiti_client,
        org_id=seed_user_with_memory["org_id"],
    )

    start = time.monotonic()
    results = await retriever.hybrid_search(
        query="What programming languages does Alice know?",
        user_id=seed_user_with_memory["id"],
        limit=20,
    )
    elapsed_ms = (time.monotonic() - start) * 1000

    # Should be well within budget
    assert elapsed_ms < 1500, f"Hybrid search took {elapsed_ms}ms (budget: 1500ms)"
    assert results["total_items"] > 0
```

---

## 12. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `EMBEDDING_BACKEND` | `openai` | Embedding API backend: `openai`, `azure`, `ollama` |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model name |
| `EMBEDDING_DIM` | `1536` | Vector dimension (must match model output) |
| `EMBEDDING_CACHE_TTL` | `3600` | Query embedding cache TTL (seconds) |
| `RRF_K` | `60` | RRF constant k |
| `CONTEXT_BFS_MAX_DEPTH` | `2` | Maximum graph BFS traversal depth |
| `IVFFLAT_PROBES` | `10` | pgvector IVFFLat probe count |
| `CONTEXT_PER_SOURCE_LIMIT_MULTIPLIER` | `2` | Multiplier for per-source limit |

---

## 13. Open Questions

| # | Question | Decision |
|---|----------|----------|
| OQ-1 | Should we use HNSW instead of IVFFLat for vector index? | IVFFLat is simpler to manage and adequate for <1M vectors. HNSW evaluation deferred to Phase 5 (scaling). |
| OQ-2 | Should facts and episodes share the same embedding model? | Yes — using a single model simplifies the architecture. Both are embedded with the same API call. |
| OQ-3 | Should we normalise RRF scores to 0-1 range? | Not necessary. RRF scores are only used for relative ordering, not as absolute relevance measures. |
| OQ-4 | Graphiti `traverse` doesn't return scores — should we add scoring? | The heuristic scores in §5.3 are a P0 solution. P1: Graphiti-native scoring using edge weight propagation. |

---

*Document maintained by the OpenZep team. Update this document if retrieval algorithms or index strategies change.*
