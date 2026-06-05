# Task Definitions — Complete Reference

> **Phase:** 1 (Core Memory) for core tasks, Phase 2/3 for extended tasks
> **SRS Requirements:** WRK-01, WRK-02, WRK-05, WRK-07, ING-04, BIZ-01–BIZ-04, NLP-01–NLP-17
> **Dependencies:** [01-arq-setup.md](01-arq-setup.md), [05-nlp-pipeline/](../05-nlp-pipeline/)
> **Design Authority:** @senior-dev (task implementation), @architect (orchestration decisions)

---

## 1. Overview

This document defines every ARQ task function in OpenZep. Each specification covers: trigger condition, queue assignment, input schema, output/side effects, idempotency mechanism, timeout, and retry policy.

### 1.1 Task Inventory (12 tasks)

| # | Task Name | Queue | Phase | SRS Origin |
|---|-----------|-------|-------|------------|
| 1 | `extract_entities` | high | P1 | NLP-01–NLP-04 |
| 2 | `embed_episode` | high | P1 | KG-05 |
| 3 | `embed_entity` | high | P1 | KG-06 |
| 4 | `extract_facts` | high | P1 | NLP-05–NLP-07 |
| 5 | `classify_dialog` | high | P3 | NLP-08–NLP-11 |
| 6 | `extract_structured` | high | P3 | NLP-12–NLP-14 |
| 7 | `summarise_community` | low | P2 | NLP-15–NLP-17 |
| 8 | `ingest_business_data` | low | P2 | BIZ-01–BIZ-04 |
| 9 | `sync_to_graph` | high | P2 | KG-01, KG-03 (new — fixes dual-write) |
| 10 | `delete_user_data` | high | P2 | USR-04, SEC-04 (GDPR cascade) |
| 11 | `merge_duplicate_entities` | low | P3 | KG-06 (weekly dedup) |
| 12 | `refresh_context_cache` | high | P2 | CTX-06 (cache invalidation) |

### 1.2 Common Task Pattern

All tasks follow this template:

```python
from arq import Retry
from typing import Any

from services.worker.config import get_task_settings

# Each task has per-task config with env-var override
task_config = get_task_settings("extract_entities")

async def extract_entities(ctx: dict, **kwargs: Any) -> dict:
    """One-line description.

    Args:
        ctx: ARQ worker context (redis, job_id, task_type populated by ARQ).
            OpenZep adds: trace_id, org_id, user_id via job payload.
        **kwargs: Task-specific payload fields (defined in Input Schema below).

    Returns:
        dict: Updated context with task-specific result fields.
            ARQ stores this as the job result.

    Raises:
        Retry: Raised for transient errors (LLM timeout, DB connection).
            ARQ re-enqueues the job with exponential backoff.
        ValueError: Raised for non-retryable errors (validation failure).
            Job immediately fails and goes to DLQ.
    """
    from arq.connections import ArqRedis
    import structlog

    redis: ArqRedis = ctx["redis"]
    trace_id = ctx.get("trace_id", kwargs.get("trace_id", "unknown"))
    org_id = ctx.get("org_id", kwargs.get("org_id", "unknown"))
    job_id = ctx["job_id"]

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        trace_id=trace_id,
        org_id=org_id,
        task_type="extract_entities",
        job_id=job_id,
    )

    logger = structlog.get_logger("OpenZep.worker.tasks")

    # ... implementation ...

    return {
        "trace_id": trace_id,
        "org_id": org_id,
        "task_type": "extract_entities",
        # Task-specific result fields
    }
```

---

## 2. Core Tasks (Phase 1)

### 2.1 `extract_entities`

| Field | Specification |
|-------|---------------|
| **Task name** | `extract_entities` |
| **Trigger** | Immediately after message ingestion (`POST /v1/users/{user_id}/memory`) |
| **Queue** | `high` |
| **Input schema** | `ExtractEntitiesPayload` |
| **Output / side effect** | Creates EntityNode and RELATES_TO edges in Graphiti. Upserts entity embeddings. |
| **Idempotency key** | `content_hash:content[:100]` — SHA256 of first 100 chars of message. Check `episodes.enrichment_status & 1` before running. |
| **Timeout** | 120s |
| **Max retries** | 3 |
| **Backoff** | Exponential with jitter: `min(60, 2 * 2^attempt) + random(0, 1000)ms` |

```python
# Input schema
from pydantic import BaseModel, Field
from uuid import UUID

class ExtractEntitiesPayload(BaseModel):
    """Payload for the extract_entities task."""

    episode_id: UUID = Field(..., description="The episode UUID to extract entities from.")
    session_id: UUID = Field(..., description="The session UUID this episode belongs to.")
    user_id: UUID = Field(..., description="The user UUID.")
    org_id: str = Field(..., description="Organization ID for tenant isolation.")
    content: str = Field(..., description="Message content to extract entities from.", max_length=65536)
    trace_id: str = Field(default="unknown", description="Trace ID for observability correlation.")
    content_hash: str = Field(..., description="SHA256 hash of content[:100] for idempotency.")

    class Config:
        frozen = True  # Immutable — payload should not change during processing
```

**Idempotency implementation:**

```python
# services/worker/tasks/extract_entities.py

ENRICHMENT_BIT_EXTRACT_ENTITIES = 1  # Bit 0 — see enrichment_status flags

async def extract_entities(ctx: dict, **kwargs: Any) -> dict:
    payload = ExtractEntitiesPayload(**kwargs)

    # ── Idempotency check ────────────────────────────────────
    # Check enrichment_status bit. If entity extraction already completed, skip.
    episode = await episode_repo.get_by_id(payload.episode_id)
    if episode.enrichment_status & ENRICHMENT_BIT_EXTRACT_ENTITIES:
        logger.info("task.skipped.already_done", episode_id=str(payload.episode_id))
        return {"trace_id": payload.trace_id, "org_id": payload.org_id, "task_type": "extract_entities", "skipped": True}

    # ── LLM call ─────────────────────────────────────────────
    try:
        entities = await llm_client.extract_entities(
            text=payload.content,
            org_id=payload.org_id,
        )
    except (LLMTimeoutError, LLMAPIError) as exc:
        logger.warning("task.llm_failed", error=str(exc), attempt=ctx.get("attempt", 0))
        raise Retry(exc) from exc  # ARQ will re-enqueue with backoff

    # ── Persist to Graphiti ──────────────────────────────────
    for entity in entities:
        await graphiti_client.upsert_entity(
            org_id=payload.org_id,
            entity=entity,
            source_episode_id=payload.episode_id,
        )

    # ── Mark enrichment bit ──────────────────────────────────
    await episode_repo.set_enrichment_bit(
        episode_id=payload.episode_id,
        bit=ENRICHMENT_BIT_EXTRACT_ENTITIES,
        value=True,
    )

    logger.info("task.completed", entity_count=len(entities))
    return {
        "trace_id": payload.trace_id,
        "org_id": payload.org_id,
        "task_type": "extract_entities",
        "entities_count": len(entities),
    }
```

**Enrichment status bit flags:**
```python
# packages/core/models/episode.py
# Stored in episodes.enrichment_status (INTEGER, bitmask)
class EnrichmentStatus:
    EXTRACT_ENTITIES  = 1 << 0  # 1
    EMBED_EPISODE      = 1 << 1  # 2
    EXTRACT_FACTS      = 1 << 2  # 4
    CLASSIFY_DIALOG    = 1 << 3  # 8
    EXTRACT_STRUCTURED = 1 << 4  # 16
    SYNC_TO_GRAPH      = 1 << 5  # 32
```

---

### 2.2 `embed_episode`

| Field | Specification |
|-------|---------------|
| **Task name** | `embed_episode` |
| **Trigger** | After message is stored in episodes table |
| **Queue** | `high` |
| **Input schema** | `EmbedEpisodePayload` |
| **Output / side effect** | Updates `episodes.embedding` with pgvector. Updates `episodes.enrichment_status` bit 1. |
| **Idempotency key** | `episode_id` — check `enrichment_status & 2` before running |
| **Timeout** | 60s (OpenAI) / 300s (Ollama) |
| **Max retries** | 3 |
| **Backoff** | `min(60, 1 * 2^attempt) + random(0, 1000)ms` |

```python
class EmbedEpisodePayload(BaseModel):
    """Payload for embed_episode task."""

    episode_id: UUID
    content: str = Field(..., max_length=65536)
    org_id: str
    trace_id: str = "unknown"
    content_hash: str  # SHA256 of full content[:100]

    class Config:
        frozen = True
```

```python
# services/worker/tasks/embed_episode.py

ENRICHMENT_BIT_EMBED_EPISODE = 2  # Bit 1

async def embed_episode(ctx: dict, **kwargs: Any) -> dict:
    payload = EmbedEpisodePayload(**kwargs)

    # ── Idempotency check ────────────────────────────────────
    episode = await episode_repo.get_by_id(payload.episode_id)
    if episode.enrichment_status & ENRICHMENT_BIT_EMBED_EPISODE:
        logger.info("task.skipped.already_done", episode_id=str(payload.episode_id))
        return {"trace_id": payload.trace_id, "org_id": payload.org_id, "task_type": "embed_episode", "skipped": True}

    # ── Generate embedding ───────────────────────────────────
    try:
        embedding = await embedding_client.embed_text(payload.content)
    except Exception as exc:
        logger.warning("task.embedding_failed", error=str(exc))
        raise Retry(exc) from exc

    # ── Store in pgvector ────────────────────────────────────
    await episode_repo.update_embedding(
        episode_id=payload.episode_id,
        embedding=embedding,
    )

    # ── Mark enrichment bit ──────────────────────────────────
    await episode_repo.set_enrichment_bit(
        episode_id=payload.episode_id,
        bit=ENRICHMENT_BIT_EMBED_EPISODE,
        value=True,
    )

    logger.info("task.completed", embedding_dim=len(embedding))
    return {"trace_id": payload.trace_id, "org_id": payload.org_id, "task_type": "embed_episode"}
```

---

### 2.3 `embed_entity`

| Field | Specification |
|-------|---------------|
| **Task name** | `embed_entity` |
| **Trigger** | After entity is upserted in Graphiti |
| **Queue** | `high` |
| **Input schema** | `EmbedEntityPayload` |
| **Output / side effect** | Updates `facts.embedding` for entity summary. Entity nodes in Graphiti already carry embeddings. |
| **Idempotency key** | `entity_id` — check `entity_summary_hash` against stored hash |
| **Timeout** | 60s (OpenAI) / 300s (Ollama) |
| **Max retries** | 3 |
| **Backoff** | `min(60, 1 * 2^attempt) + random(0, 1000)ms` |

```python
class EmbedEntityPayload(BaseModel):
    """Payload for embed_entity task."""

    entity_id: str  # Graphiti entity node UUID
    entity_name: str
    entity_summary: str  # Text to embed (entity name + description)
    org_id: str
    trace_id: str = "unknown"
    summary_hash: str  # SHA256 of entity_summary — used for idempotency

    class Config:
        frozen = True
```

---

### 2.4 `extract_facts`

| Field | Specification |
|-------|---------------|
| **Task name** | `extract_facts` |
| **Trigger** | After message ingestion |
| **Queue** | `high` |
| **Input schema** | `ExtractFactsPayload` |
| **Output / side effect** | Creates rows in `facts` table with `confidence`, `source_episode_id`, `valid_from`/`valid_to`. |
| **Idempotency key** | `content_hash` — check `enrichment_status & 4` before running |
| **Timeout** | 120s |
| **Max retries** | 3 |
| **Backoff** | `min(60, 2 * 2^attempt) + random(0, 1000)ms` |

```python
class ExtractFactsPayload(BaseModel):
    """Payload for extract_facts task."""

    episode_id: UUID
    session_id: UUID
    user_id: UUID
    org_id: str
    content: str = Field(..., max_length=65536)
    # The full conversation context (previous turns) helps the LLM
    # extract facts that reference earlier statements.
    conversation_context: list[dict] | None = None
    trace_id: str = "unknown"
    content_hash: str

    class Config:
        frozen = True
```

---

## 3. NLP Enrichment Tasks (Phase 2-3)

### 3.1 `classify_dialog`

| Field | Specification |
|-------|---------------|
| **Task name** | `classify_dialog` |
| **Trigger** | After message ingestion (per turn) |
| **Queue** | `high` |
| **Input schema** | `ClassifyDialogPayload` |
| **Output / side effect** | Creates row in `dialog_classifications` table with `intent`, `emotion`, `valence`, `arousal`. |
| **Idempotency key** | `episode_id` — check `enrichment_status & 8` before running |
| **Timeout** | 60s |
| **Max retries** | 3 |
| **Backoff** | `min(60, 2 * 2^attempt) + random(0, 1000)ms` |

```python
class ClassifyDialogPayload(BaseModel):
    """Payload for classify_dialog task."""

    episode_id: UUID
    session_id: UUID
    user_id: UUID
    org_id: str
    role: str  # user/assistant/system/tool
    content: str = Field(..., max_length=65536)
    # Previous turns for context-aware classification
    previous_turns: list[dict] | None = None
    trace_id: str = "unknown"

    class Config:
        frozen = True
```

**Output shape (stored in `dialog_classifications`):**
```json
{
  "intent": "purchase_intent",
  "emotion": "frustrated",
  "valence": "negative",
  "arousal": "high",
  "raw": {
    "intent_confidence": 0.92,
    "emotion_confidence": 0.87,
    "labels": ["urgent", "billing_issue"]
  }
}
```

---

### 3.2 `extract_structured`

| Field | Specification |
|-------|---------------|
| **Task name** | `extract_structured` |
| **Trigger** | On session close (`PATCH /v1/users/{user_id}/sessions/{session_id}/close` or `closed_at` is set) |
| **Queue** | `high` |
| **Input schema** | `ExtractStructuredPayload` |
| **Output / side effect** | Creates row in `structured_extractions` table. Matches org-defined JSON Schema. |
| **Idempotency key** | `session_id` — ensure one extraction per session (check `session_id` not already in `structured_extractions`) |
| **Timeout** | 180s (may need to process entire session content) |
| **Max retries** | 3 |
| **Backoff** | `min(60, 2 * 2^attempt) + random(0, 1000)ms` |

```python
class ExtractStructuredPayload(BaseModel):
    """Payload for extract_structured task."""

    session_id: UUID
    user_id: UUID
    org_id: str
    # Loaded from org config — the JSON Schema for extraction
    schema_id: UUID
    schema_definition: dict  # The org's JSON Schema
    # All messages in this session, for context
    messages: list[dict]
    trace_id: str = "unknown"

    class Config:
        frozen = True
```

> **Design note:** The extraction schema is passed in the payload (not loaded inside the task) to keep the task self-contained and avoid a DB round-trip. The schema is resolved at enqueue time in the API layer.

---

## 4. Batch & Maintenance Tasks (Phase 2)

### 4.1 `summarise_community`

| Field | Specification |
|-------|---------------|
| **Task name** | `summarise_community` |
| **Trigger** | Scheduled (nightly) or on-demand via admin API |
| **Queue** | `low` |
| **Input schema** | `SummariseCommunityPayload` |
| **Output / side effect** | Detects entity clusters (Louvain/Label Propagation), generates LLM summary, upserts CommunityNode in Graphiti. |
| **Idempotency key** | `(org_id, community_id, date)` — community summaries are versioned. Re-running regenerates the summary. |
| **Timeout** | 600s (10 min) |
| **Max retries** | 2 |
| **Backoff** | `min(120, 5 * 2^attempt) + random(0, 1000)ms` |

```python
class SummariseCommunityPayload(BaseModel):
    """Payload for summarise_community task.

    When triggered by schedule, only org_id is provided (all communities).
    When triggered by admin API, specific community_id can be targeted.
    """

    org_id: str
    community_id: str | None = None  # If None, summarise ALL communities for this org
    trace_id: str = "unknown"

    class Config:
        frozen = True
```

> ⚠️ **Cost control:** This task runs an LLM call per community. For orgs with > 500 communities, batch the LLM calls (summarise 5 communities per LLM call) to control cost. See [05-nlp-pipeline/07-llm-cost-control.md](../05-nlp-pipeline/07-llm-cost-control.md).

---

### 4.2 `ingest_business_data`

| Field | Specification |
|-------|---------------|
| **Task name** | `ingest_business_data` |
| **Trigger** | `POST /v1/users/{user_id}/facts` |
| **Queue** | `low` |
| **Input schema** | `IngestBusinessDataPayload` |
| **Output / side effect** | Creates rows in `facts` table. Creates RELATES_TO edges in Graphiti. |
| **Idempotency key** | `request_id` (from `Idempotency-Key` header) or `(subject, predicate, object, valid_at)` tuple |
| **Timeout** | 60s |
| **Max retries** | 3 |
| **Backoff** | `min(60, 1 * 2^attempt) + random(0, 1000)ms` |

```python
class FactTriple(BaseModel):
    subject: str
    predicate: str
    object: str
    valid_at: str | None = None  # ISO-8601
    expires_at: str | None = None
    confidence: float = 1.0

class IngestBusinessDataPayload(BaseModel):
    """Payload for ingest_business_data task."""

    user_id: UUID
    org_id: str
    facts: list[FactTriple] = Field(..., max_length=500)
    request_id: str  # For idempotency
    trace_id: str = "unknown"

    class Config:
        frozen = True
```

---

## 5. New Tasks (Introduced in Phase 2)

### 5.1 `sync_to_graph`

**Why this task exists:** The SRS describes a "dual-write" pattern where episodes are written to both PostgreSQL and Graphiti during ingestion. Dual-writes in the API request path add latency and risk inconsistency. Instead, episodes are written only to PostgreSQL in the request path, and `sync_to_graph` asynchronously synchronises them to Graphiti's episodic layer.

| Field | Specification |
|-------|---------------|
| **Task name** | `sync_to_graph` |
| **Trigger** | After episode is committed to PostgreSQL (enqueued in the same transaction as episode creation) |
| **Queue** | `high` |
| **Input schema** | `SyncToGraphPayload` |
| **Output / side effect** | Creates EpisodicNode in Graphiti. Updates `episodes.graphiti_node_id` with the Graphiti node reference. |
| **Idempotency key** | `episode_id` — check `graphiti_node_id IS NOT NULL` before creating |
| **Timeout** | 30s |
| **Max retries** | 3 |
| **Backoff** | `min(60, 1 * 2^attempt) + random(0, 1000)ms` |

```python
class SyncToGraphPayload(BaseModel):
    """Payload for sync_to_graph task."""

    episode_id: UUID
    user_id: UUID
    org_id: str
    role: str
    content: str
    created_at: str  # ISO-8601 timestamp
    trace_id: str = "unknown"

    class Config:
        frozen = True
```

```python
# services/worker/tasks/sync_to_graph.py

async def sync_to_graph(ctx: dict, **kwargs: Any) -> dict:
    payload = SyncToGraphPayload(**kwargs)

    # ── Idempotency check ────────────────────────────────────
    episode = await episode_repo.get_by_id(payload.episode_id)
    if episode.graphiti_node_id is not None:
        logger.info("task.skipped.already_synced", episode_id=str(payload.episode_id))
        return {"trace_id": payload.trace_id, "org_id": payload.org_id, "task_type": "sync_to_graph", "skipped": True}

    # ── Sync to Graphiti ─────────────────────────────────────
    try:
        node_id = await graphiti_client.add_episode(
            org_id=payload.org_id,
            user_id=str(payload.user_id),
            episode_id=str(payload.episode_id),
            role=payload.role,
            content=payload.content,
            created_at=payload.created_at,
        )
    except GraphitiConnectionError as exc:
        logger.warning("task.graphiti_connection_failed", error=str(exc))
        raise Retry(exc) from exc

    # ── Update graphiti_node_id ──────────────────────────────
    await episode_repo.update_graphiti_node_id(
        episode_id=payload.episode_id,
        graphiti_node_id=node_id,
    )

    logger.info("task.completed", graphiti_node_id=node_id)
    return {
        "trace_id": payload.trace_id,
        "org_id": payload.org_id,
        "task_type": "sync_to_graph",
        "graphiti_node_id": node_id,
    }
```

---

### 5.2 `delete_user_data`

**Why this task exists:** User deletion (GDPR right to erasure, USR-04, SEC-04) must cascade across all stores: PostgreSQL, Graphiti, FalkorDB, and cached data in Redis. Doing this synchronously in the API request would timeout on users with large graphs. The task runs async with visibility into deletion progress.

| Field | Specification |
|-------|---------------|
| **Task name** | `delete_user_data` |
| **Trigger** | `DELETE /v1/users/{user_id}` (enqueued before the API returns 202) |
| **Queue** | `high` |
| **Input schema** | `DeleteUserDataPayload` |
| **Output / side effect** | Deletes from: `episodes`, `facts`, `dialog_classifications`, `structured_extractions`, `sessions`, `users` (PostgreSQL). Deletes EntityNode, EpisodicNode, edges from Graphiti/FalkorDB. Invalidates Redis cache keys. |
| **Idempotency key** | `user_id` — deletion is idempotent by nature. Check `users.is_deleted` before running. |
| **Timeout** | 300s (large graphs may take time) |
| **Max retries** | 2 (if deletion partially fails, re-run to clean up remaining artifacts) |
| **Backoff** | `min(60, 5 * 2^attempt) + random(0, 1000)ms` |

```python
class DeleteUserDataPayload(BaseModel):
    """Payload for delete_user_data GDPR task."""

    user_id: UUID
    org_id: str
    # The external_id is used for Graphiti node lookups
    external_id: str
    # PostgreSQL cascades handle FK relationships if ON DELETE CASCADE is set.
    # Graphiti nodes must be deleted explicitly.
    trace_id: str = "unknown"
    # If True, soft-delete only (mark is_deleted, don't remove from graph)
    # For GDPR hard-delete, this is False.
    soft_delete: bool = False

    class Config:
        frozen = True
```

---

### 5.3 `merge_duplicate_entities`

**Why this task exists:** Entity extraction is LLM-based and non-deterministic — the same person may be extracted as "John Smith" in one session and "John" in another. Entity deduplication merges these into a single node, updating all relationship references.

| Field | Specification |
|-------|---------------|
| **Task name** | `merge_duplicate_entities` |
| **Trigger** | Scheduled (weekly) or on-demand |
| **Queue** | `low` |
| **Input schema** | `MergeDuplicateEntitiesPayload` |
| **Output / side effect** | Merges entity nodes in Graphiti with similarity above threshold. Updates relationship references. Records merge history. |
| **Idempotency key** | `(org_id, run_id)` — each run has a unique run_id. Merge operations are logged in an `entity_merges` audit table. |
| **Timeout** | 600s (10 min) |
| **Max retries** | 1 |
| **Backoff** | `min(120, 10 * 2^attempt) + random(0, 1000)ms` |

```python
class MergeDuplicateEntitiesPayload(BaseModel):
    """Payload for merge_duplicate_entities task."""

    org_id: str
    run_id: str  # UUID for this merge run
    # Similarity threshold for entity name fuzzy matching (0.0 to 1.0)
    similarity_threshold: float = 0.85
    trace_id: str = "unknown"

    class Config:
        frozen = True
```

**Merge algorithm sketch:**
```python
async def merge_duplicate_entities(ctx: dict, **kwargs: Any) -> dict:
    payload = MergeDuplicateEntitiesPayload(**kwargs)
    logger = structlog.get_logger("OpenZep.worker.tasks")

    # 1. Fetch all entity nodes for this org from Graphiti
    entities = await graphiti_client.get_all_entities(org_id=payload.org_id)

    # 2. Group by similarity (fuzzy name matching)
    #    Use rapidfuzz or similar for entity name comparison
    groups = group_similar_entities(entities, threshold=payload.similarity_threshold)

    merges_performed = 0
    for group in groups:
        if len(group) < 2:
            continue  # No duplicates in this group

        # 3. Pick canonical entity (most recently updated, or most connected)
        canonical = select_canonical(group)

        # 4. Merge all non-canonical entities into canonical
        for duplicate in group:
            if duplicate.uuid == canonical.uuid:
                continue
            await graphiti_client.merge_entities(
                org_id=payload.org_id,
                source_id=duplicate.uuid,
                target_id=canonical.uuid,
            )
            merges_performed += 1

        # 5. Log merge in audit table
        await entity_merge_repo.log_merge(
            org_id=payload.org_id,
            run_id=payload.run_id,
            canonical_id=canonical.uuid,
            merged_ids=[e.uuid for e in group if e.uuid != canonical.uuid],
        )

    logger.info("task.completed", groups_found=len(groups), merges_performed=merges_performed)
    return {"trace_id": payload.trace_id, "org_id": payload.org_id, "task_type": "merge_duplicate_entities"}
```

---

### 5.4 `refresh_context_cache`

**Why this task exists:** Context blocks are cached in Redis with a 30s TTL (CTX-06). When new data is ingested that could affect context retrieval (new facts, updated entity summaries, new community summaries), the cache should be invalidated for the affected user to avoid stale context. This task performs targeted cache invalidation.

| Field | Specification |
|-------|---------------|
| **Task name** | `refresh_context_cache` |
| **Trigger** | After entity extraction, fact extraction, or community re-summarisation |
| **Queue** | `high` |
| **Input schema** | `RefreshContextCachePayload` |
| **Output / side effect** | Invalidates Redis cache keys for the affected user's context blocks. Rebuilds hot cache entries. |
| **Idempotency key** | `(user_id, trigger_type, trigger_id)` — if a refresh was already queued for the same trigger, skip. |
| **Timeout** | 10s |
| **Max retries** | 1 |
| **Backoff** | `min(30, 1 * 2^attempt) + random(0, 500)ms` |

```python
class RefreshContextCachePayload(BaseModel):
    """Payload for refresh_context_cache task."""

    user_id: UUID
    org_id: str
    # What triggered this cache refresh
    trigger_type: str  # "entity_extraction", "fact_extraction", "community_update"
    trigger_id: str    # The ID of the entity/fact/community that changed
    trace_id: str = "unknown"

    class Config:
        frozen = True
```

---

## 6. Task Function Reference — Registering in worker.py

Each task function must be exported from its module and included in the appropriate queue's task list in [01-arq-setup.md](01-arq-setup.md#33-task-registry).

```python
# services/worker/tasks/__init__.py

# Re-export all task functions for easy registration
from services.worker.tasks.extract_entities import extract_entities
from services.worker.tasks.embed_episode import embed_episode
from services.worker.tasks.embed_entity import embed_entity
from services.worker.tasks.extract_facts import extract_facts
from services.worker.tasks.classify_dialog import classify_dialog
from services.worker.tasks.extract_structured import extract_structured
from services.worker.tasks.summarise_community import summarise_community
from services.worker.tasks.ingest_business_data import ingest_business_data
from services.worker.tasks.sync_to_graph import sync_to_graph
from services.worker.tasks.delete_user_data import delete_user_data
from services.worker.tasks.merge_duplicate_entities import merge_duplicate_entities
from services.worker.tasks.refresh_context_cache import refresh_context_cache

__all__ = [
    "extract_entities",
    "embed_episode",
    "embed_entity",
    "extract_facts",
    "classify_dialog",
    "extract_structured",
    "summarise_community",
    "ingest_business_data",
    "sync_to_graph",
    "delete_user_data",
    "merge_duplicate_entities",
    "refresh_context_cache",
]
```

---

## 7. Per-Task Configuration Reference

Each task's timeout and max_retries can be overridden via environment variables. The naming convention is `{TASK_NAME}_TIMEOUT` and `{TASK_NAME}_MAX_RETRIES` in UPPER_SNAKE_CASE.

| Variable | Default | Description |
|----------|---------|-------------|
| `EXTRACT_ENTITIES_TIMEOUT` | `120` | Entity extraction task timeout (s) |
| `EXTRACT_ENTITIES_MAX_RETRIES` | `3` | Entity extraction max retries |
| `EMBED_EPISODE_TIMEOUT` | `60` | Episode embedding timeout (s) |
| `EMBED_EPISODE_MAX_RETRIES` | `3` | Episode embedding max retries |
| `EMBED_ENTITY_TIMEOUT` | `60` | Entity embedding timeout (s) |
| `EMBED_ENTITY_MAX_RETRIES` | `3` | Entity embedding max retries |
| `EXTRACT_FACTS_TIMEOUT` | `120` | Fact extraction timeout (s) |
| `EXTRACT_FACTS_MAX_RETRIES` | `3` | Fact extraction max retries |
| `CLASSIFY_DIALOG_TIMEOUT` | `60` | Dialog classification timeout (s) |
| `CLASSIFY_DIALOG_MAX_RETRIES` | `3` | Dialog classification max retries |
| `EXTRACT_STRUCTURED_TIMEOUT` | `180` | Structured extraction timeout (s) |
| `EXTRACT_STRUCTURED_MAX_RETRIES` | `3` | Structured extraction max retries |
| `SUMMARISE_COMMUNITY_TIMEOUT` | `600` | Community summarisation timeout (s) |
| `SUMMARISE_COMMUNITY_MAX_RETRIES` | `2` | Community summarisation max retries |
| `INGEST_BUSINESS_DATA_TIMEOUT` | `60` | Business data ingest timeout (s) |
| `INGEST_BUSINESS_DATA_MAX_RETRIES` | `3` | Business data ingest max retries |
| `SYNC_TO_GRAPH_TIMEOUT` | `30` | Graph sync timeout (s) |
| `SYNC_TO_GRAPH_MAX_RETRIES` | `3` | Graph sync max retries |
| `DELETE_USER_DATA_TIMEOUT` | `300` | User data deletion timeout (s) |
| `DELETE_USER_DATA_MAX_RETRIES` | `2` | User data deletion max retries |
| `MERGE_DUPLICATE_ENTITIES_TIMEOUT` | `600` | Entity dedup timeout (s) |
| `MERGE_DUPLICATE_ENTITIES_MAX_RETRIES` | `1` | Entity dedup max retries |
| `REFRESH_CONTEXT_CACHE_TIMEOUT` | `10` | Cache refresh timeout (s) |
| `REFRESH_CONTEXT_CACHE_MAX_RETRIES` | `1` | Cache refresh max retries |

---

## 8. SRS Traceability

| SRS ID | Requirement | Covered By |
|--------|-------------|------------|
| NLP-01–04 | Entity extraction | §2.1 `extract_entities` |
| NLP-05–07 | Fact extraction | §2.4 `extract_facts` |
| NLP-08–11 | Dialog classification | §3.1 `classify_dialog` |
| NLP-12–14 | Structured extraction | §3.2 `extract_structured` |
| NLP-15–17 | Community summarisation | §4.1 `summarise_community` |
| BIZ-01–04 | Business data ingestion | §4.2 `ingest_business_data` |
| ING-04 | Async enrichment enqueued on ingestion | §2.1–2.4 trigger conditions |
| WRK-01 | All enrichment via ARQ | All tasks |
| WRK-02 | Idempotency | Idempotency key in every task spec |
| WRK-07 | Priority queues | Queue assignment (high/low) per task |
| USR-04, SEC-04 | GDPR cascade deletion | §5.2 `delete_user_data` |

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
