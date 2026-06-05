# Task Orchestration — Event-Driven vs Single Orchestrator

> **Phase:** 1 (Core Memory) for event-driven approach; Phase 2+ for single orchestrator
> **SRS Requirements:** WRK-01, WRK-02, WRK-05, ING-04, NLP-01–NLP-14
> **Dependencies:** [01-arq-setup.md](01-arq-setup.md), [02-task-definitions.md](02-task-definitions.md), [05-priority-queues.md](05-priority-queues.md)
> **Design Authority:** @architect

---

## 1. Overview

When messages are ingested, multiple enrichment tasks must run: entity extraction, fact extraction, embedding generation, dialog classification, and graph synchronisation. The question is **how these tasks are sequenced and coordinated**.

This document evaluates two approaches — **event-driven** and **single orchestrator** — and makes a phased recommendation.

### 1.1 The Orchestration Problem

```
After message ingestion (POST /memory → 202 Accepted):

    ┌─────────────────────────────┐
    │        API Layer            │
    │  Writes episode to PG       │
    │  Enqueues enrichment tasks  │
    └─────────────┬───────────────┘
                  │
     ┌────────────┼────────────────┐
     ▼            ▼                ▼
  extract      extract        classify
  entities     facts           dialog
     │            │                │
     ▼            ▼                ▼
  embed       embed             sync_to
  entity      episode           graph
     │            │
     ▼            ▼
  embed        refresh_cache
  entity
     │
     ▼
  sync_to_graph
  refresh_cache

Questions:
1. Should tasks run in parallel or sequentially?
2. If `extract_entities` fails, should `embed_entity` still run?
3. How do we track which enrichment steps have completed for an episode?
4. Can we recover from a worker crash mid-pipeline?
```

---

## 2. Approach A: Event-Driven (Recommended for Phase 0-1)

### 2.1 How It Works

Each task, on successful completion, enqueues the next task(s) in the chain. Tasks are independent actors that know about their dependents.

```
Ingestion endpoint (API):
  1. Writes episode to PostgreSQL
  2. Enqueues: extract_entities, extract_facts, classify_dialog, sync_to_graph
     (all in parallel — they are independent of each other)

extract_entities completes:
  → enqueue embed_entity

embed_entity completes:
  → enqueue sync_to_graph (if entity embedding changed graph state)
  → enqueue refresh_context_cache (invalidate cache for this user)

extract_facts completes:
  → enqueue refresh_context_cache

classify_dialog completes:
  → (no downstream task — classification is terminal)

sync_to_graph completes:
  → update episodes.enrichment_status bit
```

### 2.2 Implementation

```python
# In each task function, the last step enqueues downstream tasks:

async def extract_entities(ctx: dict, **kwargs: Any) -> dict:
    payload = ExtractEntitiesPayload(**kwargs)

    # ... extraction logic from 02-task-definitions.md §2.1 ...

    # ── Enqueue downstream tasks ────────────────────────────
    # Entity extraction is complete → enqueue entity embedding
    await ctx["redis"].enqueue_job(
        "embed_entity",
        entity_id=entity_id,
        entity_name=entity.name,
        entity_summary=entity.summary,
        org_id=payload.org_id,
        trace_id=payload.trace_id,
        summary_hash=payload.content_hash,
        _queue_name="OpenZep:dev:queue:high",
    )

    # Also enqueue context cache refresh for this user
    await ctx["redis"].enqueue_job(
        "refresh_context_cache",
        user_id=str(payload.user_id),
        org_id=payload.org_id,
        trigger_type="entity_extraction",
        trigger_id=str(payload.episode_id),
        trace_id=payload.trace_id,
        _queue_name="OpenZep:dev:queue:high",
    )

    return {"trace_id": payload.trace_id, "org_id": payload.org_id, "task_type": "extract_entities"}
```

### 2.3 Advantages

| Pro | Explanation |
|-----|-------------|
| **Simple to implement** | Each task is self-contained. No central orchestrator to build and maintain. |
| **Natural parallelism** | Independent tasks (`extract_entities`, `extract_facts`, `classify_dialog`) run concurrently without coordination code. |
| **Resilient to partial failure** | If `classify_dialog` fails, `extract_facts` continues unaffected. |
| **Easy to add new tasks** | Adding a new enrichment step means: (1) write the task, (2) add enqueue calls from its upstream tasks. No orchestrator changes. |
| **Matches Phase 0-1 scope** | The simple chain (ingest → extract → embed → refresh) is well-suited to a small number of tasks. |

### 2.4 Disadvantages

| Con | Explanation |
|-----|-------------|
| **Implicit DAG** | The task dependency graph is scattered across task implementations. A new developer must read every task to understand the pipeline. No single source of truth. |
| **Difficult to debug** | If a downstream task is not being enqueued, you must trace through the upstream task's code to find the missing enqueue call. |
| **No transactional enqueue** | ARQ's `enqueue_job` is a Redis call — if the task crashes *after* doing work but *before* enqueuing the next task, the chain breaks and data is lost. |
| **Circular dependency risk** | With many tasks enqueuing each other, it's possible to create accidental circular chains. |
| **Hard to add conditional logic** | "Run task C only if task A succeeded and task B returned > 5 entities" — this requires the enqueue logic to check upstream results, which are not available in the current task's scope. |

---

## 3. Approach B: Single Orchestrator (Recommended for Phase 2+)

### 3.1 How It Works

The ingestion endpoint enqueues a single **orchestrator task** (`process_memory`). This orchestrator contains the full pipeline as sequential steps within a single job, with explicit dependency checking and state persistence.

```
Ingestion endpoint (API):
  1. Writes episode to PostgreSQL
  2. Enqueues: process_memory (single job)

process_memory orchestrator:
  Step 1: extract_entities()          → updates enrichment_status bit
  Step 2: extract_facts()             → updates enrichment_status bit
  Step 3: classify_dialog()           → updates enrichment_status bit
  Step 4: sync_to_graph()             → updates enrichment_status bit
  Step 5: embed_episode()             → updates enrichment_status bit
  Step 6: if entities were found:
              embed_entity()          → updates enrichment_status bit
  Step 7: refresh_context_cache()     → invalidates Redis caches
  Step 8: mark episode fully enriched → updates enrichment_status to COMPLETE
```

### 3.2 Implementation

```python
# services/worker/tasks/process_memory.py
"""Single orchestrator task that sequences all memory enrichment steps.

This task replaces the individual enqueue calls with a single job
that runs all enrichment steps in order, with persistence between each step.

Usage:
    Enqueued by the ingestion endpoint instead of individual tasks:
        await redis.enqueue_job("process_memory", **payload)

If the orchestrator crashes mid-pipeline, it resumes from the last
completed step using enrichment_status bits.
"""

from enum import IntFlag
from typing import Any

import structlog
from arq import Retry
from pydantic import BaseModel, Field
from uuid import UUID

from services.worker.config import get_task_settings
from services.worker.tasks.extract_entities import execute_extract_entities
from services.worker.tasks.extract_facts import execute_extract_facts
from services.worker.tasks.classify_dialog import execute_classify_dialog
from services.worker.tasks.sync_to_graph import execute_sync_to_graph
from services.worker.tasks.embed_episode import execute_embed_episode
from services.worker.tasks.embed_entity import execute_embed_entity
from services.worker.tasks.refresh_context_cache import execute_refresh_context_cache
# Note: Each task module exports both the ARQ task function (for event-driven mode)
# and a plain async function (for orchestrator mode).


class EnrichmentStep(IntFlag):
    """Bitmask flags for tracking enrichment progress per episode.

    Stored in episodes.enrichment_status (INTEGER).
    """
    NONE            = 0
    EXTRACT_ENTITIES = 1 << 0  # 1
    EXTRACT_FACTS   = 1 << 1   # 2
    CLASSIFY_DIALOG = 1 << 2   # 4
    SYNC_TO_GRAPH   = 1 << 3   # 8
    EMBED_EPISODE   = 1 << 4   # 16
    EMBED_ENTITY    = 1 << 5   # 32
    REFRESH_CACHE   = 1 << 6   # 64
    COMPLETE        = 1 << 7   # 128 — all steps done


ORCHESTRATOR_STEPS = [
    # (step_name, step_bit, execute_fn, is_required)
    ("extract_entities", EnrichmentStep.EXTRACT_ENTITIES, execute_extract_entities, True),
    ("extract_facts", EnrichmentStep.EXTRACT_FACTS, execute_extract_facts, True),
    ("classify_dialog", EnrichmentStep.CLASSIFY_DIALOG, execute_classify_dialog, False),
    ("sync_to_graph", EnrichmentStep.SYNC_TO_GRAPH, execute_sync_to_graph, True),
    ("embed_episode", EnrichmentStep.EMBED_EPISODE, execute_embed_episode, True),
    # embed_entity is conditional — only if entities were extracted
    ("embed_entity", EnrichmentStep.EMBED_ENTITY, execute_embed_entity, False),
    ("refresh_cache", EnrichmentStep.REFRESH_CACHE, execute_refresh_cache, True),
]


class ProcessMemoryPayload(BaseModel):
    """Payload for the process_memory orchestrator task."""

    episode_id: UUID
    session_id: UUID
    user_id: UUID
    org_id: str
    content: str = Field(..., max_length=65536)
    role: str
    trace_id: str = "unknown"
    content_hash: str

    class Config:
        frozen = True


async def process_memory(ctx: dict, **kwargs: Any) -> dict:
    """Orchestrator task — runs all enrichment steps for a single episode.

    This function:
    1. Loads the current enrichment_status from the database
    2. Iterates through each step, skipping already-completed ones
    3. Runs each step with its own error handling
    4. Updates enrichment_status after each successful step
    5. If a required step fails, the orchestrator fails (uncompleted steps
       will be retried on the next invocation)

    Resume behaviour on crash:
    - enrichment_status is persisted after each step
    - If the orchestrator crashes mid-pipeline, the next invocation
      loads the status and resumes from the first uncompleted step.
    - This is safe because each step is idempotent.
    """
    payload = ProcessMemoryPayload(**kwargs)
    logger = structlog.get_logger("OpenZep.worker.orchestrator")

    # ── Bind context for structured logging ────────────────
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        trace_id=payload.trace_id,
        org_id=payload.org_id,
        task_type="process_memory",
        job_id=ctx["job_id"],
        episode_id=str(payload.episode_id),
    )

    logger.info("orchestrator.started")

    # ── Load current enrichment status ─────────────────────
    episode_repo = get_episode_repository(ctx)  # Injected via ctx on worker start
    current_status = await episode_repo.get_enrichment_status(payload.episode_id)
    logger.info("orchestrator.current_status", current_status=current_status)

    # ── Create shared context for steps ────────────────────
    step_ctx = {
        "redis": ctx["redis"],
        "episode_repo": episode_repo,
        "graphiti_client": get_graphiti_client(ctx),
        "llm_client": get_llm_client(ctx),
        "embedding_client": get_embedding_client(ctx),
        "payload": payload,
    }

    # ── Run each step ──────────────────────────────────────
    step_results = {}
    entities_extracted = False

    for step_name, step_bit, execute_fn, is_required in ORCHESTRATOR_STEPS:
        # Skip if already completed
        if current_status & step_bit:
            logger.info("orchestrator.step.skipped", step=step_name)
            continue

        logger.info("orchestrator.step.starting", step=step_name)
        try:
            result = await execute_fn(step_ctx)
            step_results[step_name] = result

            # Track if entities were extracted (for conditional embed_entity)
            if step_name == "extract_entities" and result.get("entity_count", 0) > 0:
                entities_extracted = True

            # For conditional steps, mark as skipped if not applicable
            if step_name == "embed_entity" and not entities_extracted:
                logger.info("orchestrator.step.skipped_no_entities", step=step_name)
                continue

        except Retry:
            # Retryable error — re-raise to let ARQ handle backoff
            logger.warning("orchestrator.step.retryable_failure", step=step_name)
            raise
        except Exception as exc:
            logger.error("orchestrator.step.failed", step=step_name, error=str(exc))
            if is_required:
                # Required step failed — orchestrator fails
                raise
            else:
                # Optional step failed — log and continue
                logger.warning("orchestrator.step.optional_failed_continuing", step=step_name)
                continue

        # ── Persist progress ───────────────────────────────
        new_status = current_status | step_bit
        await episode_repo.set_enrichment_status(payload.episode_id, new_status)
        current_status = new_status

        logger.info("orchestrator.step.completed", step=step_name)

    # ── Mark complete ──────────────────────────────────────
    await episode_repo.set_enrichment_status(
        payload.episode_id,
        current_status | EnrichmentStep.COMPLETE,
    )

    logger.info("orchestrator.completed", completed_steps=list(step_results.keys()))
    return {
        "trace_id": payload.trace_id,
        "org_id": payload.org_id,
        "task_type": "process_memory",
        "steps_completed": len(step_results),
    }
```

### 3.3 Advantages

| Pro | Explanation |
|-----|-------------|
| **Explicit DAG** | The entire pipeline is defined in one place (`ORCHESTRATOR_STEPS`). Easy to understand, modify, and debug. |
| **Transactional progress** | `enrichment_status` is persisted to PostgreSQL after each step. If the worker crashes, the orchestrator resumes from the first uncompleted step — no lost work, no duplicate work. |
| **Conditional logic** | Steps can depend on results of previous steps (e.g., only run `embed_entity` if entities were found). |
| **Single enqueue point** | Ingestion endpoint enqueues one job instead of 5-7. Simpler API code. |
| **Easier to monitor** | One job per episode = one row in the job queue. Clear mapping between ingestion and completion. |
| **Better error reporting** | The orchestrator knows exactly which step failed, with what error, and which steps completed successfully. |

### 3.4 Disadvantages

| Con | Explanation |
|-----|-------------|
| **No parallelism** | Steps run sequentially within a single job. An LLM call blocking for 30s delays all downstream steps. Mitigation: run independent steps as asyncio tasks within the orchestrator. |
| **Longer job timeout** | The orchestrator needs enough timeout for all steps combined (~500s total). ARQ's timeout applies to the whole job. |
| **Harder to scale individual steps** | You cannot independently scale `extract_entities` workers vs `embed_episode` workers — the orchestrator runs as one unit. Mitigation: run the orchestrator on dedicated workers. |
| **Single point of failure** | If the orchestrator code has a bug, all enrichment stops. In the event-driven model, only the broken task is affected. |

---

## 4. Phased Recommendation

### 4.1 Phase 0-1: Event-Driven (Simple, Start Fast)

```python
# In the ingestion router:
@router.post("/memory", status_code=202)
async def ingest_memory(...):
    # 1. Write episode to DB
    episode = await memory_service.add_message(...)

    # 2. Enqueue independent tasks in parallel
    await asyncio.gather(
        redis.enqueue_job("extract_entities", ...),
        redis.enqueue_job("extract_facts", ...),
        redis.enqueue_job("classify_dialog", ...),
        redis.enqueue_job("sync_to_graph", ...),
    )
```

**Use for Phase 0-1 because:**
- Few tasks (< 5), simple chains
- Need to ship quickly with minimal infra code
- Parallelism speeds up enrichment of backlogged messages
- Easy to debug individual tasks

### 4.2 Phase 2-4: Single Orchestrator (Resilient, Observable)

```python
# In the ingestion router:
@router.post("/memory", status_code=202)
async def ingest_memory(...):
    # 1. Write episode to DB
    episode = await memory_service.add_message(...)

    # 2. Enqueue orchestrator (single job)
    await redis.enqueue_job("process_memory", **payload.model_dump())
```

**Use for Phase 2+ because:**
- More tasks (8+), complex conditional dependencies
- Transactional progress guarantees needed
- Observability: one orchestrator job = one pipeline trace
- Conditional step execution (skip embedding if no entities)

### 4.3 Migration Path: Event-Driven → Orchestrator

```python
# During migration, both systems can coexist:
# - New episodes use the orchestrator
# - Old episodes in the queue use event-driven chains
# - enrichment_status bits are compatible between both approaches

# Feature flag:
if settings.USE_ORCHESTRATOR:
    await redis.enqueue_job("process_memory", ...)
else:
    await asyncio.gather(
        redis.enqueue_job("extract_entities", ...),
        redis.enqueue_job("extract_facts", ...),
        ...
    )
```

---

## 5. Dependency Tracking — `episodes.enrichment_status`

Regardless of the orchestration approach, `enrichment_status` is the single source of truth for what enrichment has been completed on an episode.

### 5.1 Column Definition

```sql
-- Add to the episodes table (see 01-data-models/01-postgresql-schema.md)
ALTER TABLE episodes ADD COLUMN enrichment_status INTEGER NOT NULL DEFAULT 0;
COMMENT ON COLUMN episodes.enrichment_status IS
    'Bitmask of completed enrichment steps. '
    'Bit 0=extract_entities, 1=embed_episode, 2=extract_facts, '
    '3=classify_dialog, 4=extract_structured, 5=sync_to_graph, '
    '7=all_complete. See EnrichmentStep IntFlag enum.';
```

### 5.2 Status Values

| Status | Meaning |
|--------|---------|
| `0` | No enrichment started |
| `1` | Entities extracted |
| `3` | Entities extracted + episode embedded |
| `7` | Entities extracted + episode embedded + facts extracted |
| `15` | All core enrichment done (bits 0-3) |
| `63` | All enrichment done (bits 0-5) |
| `255` | Enrichment complete (bit 7 set) |

### 5.3 Querying Unenriched Episodes

```python
# Use cases:

# 1. Find episodes that need entity extraction
SELECT * FROM episodes WHERE (enrichment_status & 1) = 0;

# 2. Find episodes that need embedding (entity extraction done, embedding not done)
SELECT * FROM episodes WHERE (enrichment_status & 1) = 1 AND (enrichment_status & 2) = 0;

# 3. Find fully enriched episodes
SELECT * FROM episodes WHERE (enrichment_status & 128) = 128;

# 4. Bulk re-enrich: reset enrichment status for an org
UPDATE episodes SET enrichment_status = 0 WHERE user_id IN (
    SELECT id FROM users WHERE organization_id = 'org_abc123'
);
```

### 5.4 Recovery Query — Episodes Stuck Mid-Pipeline

A maintenance script runs hourly to detect episodes that have been in the pipeline > 30 minutes:

```python
# scripts/recover_stuck_episodes.py

async def recover_stuck_episodes():
    """Re-enqueue enrichment for episodes stuck in the pipeline.

    An episode is "stuck" if:
    - enrichment_status > 0 (some enrichment started)
    - enrichment_status & 128 == 0 (not complete)
    - updated_at < now() - 30 minutes (no progress for 30 min)
    """
    stuck = await episode_repo.get_stuck_episodes(timeout_minutes=30)

    for episode in stuck:
        # Determine which steps need to be re-run
        missing_steps = get_missing_steps(episode.enrichment_status)

        if settings.USE_ORCHESTRATOR:
            await redis.enqueue_job("process_memory", **build_payload(episode))
        else:
            for step in missing_steps:
                await redis.enqueue_job(step, **build_payload(episode))

        logger.info("recovery.enqueued", episode_id=str(episode.id), missing_steps=missing_steps)
```

---

## 6. Failure Handling Comparison

| Scenario | Event-Driven | Single Orchestrator |
|----------|-------------|---------------------|
| **LLM timeout on `extract_entities`** | Individual task retries 3x. `extract_facts` and `classify_dialog` continue unaffected. | Orchestrator retries entire job 3x. Steps that already completed are skipped (enrichment_status check). |
| **`extract_entities` fails permanently** | Downstream `embed_entity` never enqueued. Cache refresh never triggered. Episode stays in "entities pending" state. | Orchestrator fails. Enrichment_status still shows entities not extracted. Episode stays in "entities pending". |
| **`embed_episode` fails permanently** | Episode embedding is missing but entities, facts, and classifications are fine. Partial enrichment is okay. | Orchestrator fails (required step). No enrichment is marked complete. |
| **Worker crash mid-pipeline** | In-flight tasks are re-enqueued by ARQ on next worker start. Some downstream tasks may be enqueued twice — but they are idempotent. | Orchestrator job is re-enqueued by ARQ. Starts from the first uncompleted step (enrichment_status). |
| **Redis outage** | All enqueue calls fail. Tasks that completed successfully cannot enqueue downstream tasks. | Same — but only one enqueue per episode. |
| **DB connection error** | Each task independently retries DB operations. | Single orchestrator retries. Steps with completed status skip re-execution. |

### 6.1 Key Principle: Partial Enrichment is OK

If `classify_dialog` fails permanently (e.g., the org hasn't configured classification labels), the system should still function:
- Entities are extracted and searchable
- Facts are extracted and queryable
- Context assembly works (it just won't include classification data)

The orchestrator's `is_required` flag distinguishes:
- **Required steps** (`is_required=True`): Without these, the memory is incomplete. Examples: `extract_entities`, `sync_to_graph`.
- **Optional steps** (`is_required=False`): Nice-to-have enrichment. If they fail, the system degrades gracefully. Examples: `classify_dialog`, `embed_entity`.

---

## 7. Transaction Safety: DB Always Wins

> ⚠️ **Critical rule:** State transitions (enrichment_status, graphiti_node_id) are written to PostgreSQL, not Redis. Redis can lose data. PostgreSQL transactions are durable.

```python
# Correct pattern — update DB, then (optionally) update cache:
async def complete_step(episode_repo, episode_id: UUID, step_bit: int) -> None:
    """Set enrichment bit in a DB transaction."""
    async with episode_repo.transaction():
        episode = await episode_repo.get_by_id(episode_id)
        new_status = episode.enrichment_status | step_bit
        await episode_repo.set_enrichment_status(episode_id, new_status)

# NEVER do this — Redis-based state can be lost:
async def wrong_pattern(redis, episode_id: UUID, step_bit: int) -> None:
    """🔴 WRONG: State stored only in Redis."""
    await redis.setbit(f"episode:{episode_id}:status", step_bit, 1)
    # If Redis goes down, this state is gone.
    # On restart, ARQ will re-enqueue the task and re-run steps
    # that were already completed!
```

---

## 8. Sequence Diagram — Both Approaches

### 8.1 Event-Driven (Phase 0-1)

```
API                     ARQ Worker                     Redis          PostgreSQL
 │                        │                              │               │
 │ POST /memory            │                              │               │
 │────────────────────────►│                              │               │
 │ 202 Accepted            │                              │               │
 │◄────────────────────────│                              │               │
 │                         │  Write episode               │               │
 │                         │─────────────────────────────►│               │
 │                         │                              │               │
 │                         │  Enqueue extract_entities    │               │
 │                         │─────────────────────────────►│               │
 │                         │  Enqueue extract_facts       │               │
 │                         │─────────────────────────────►│               │
 │                         │  Enqueue classify_dialog     │               │
 │                         │─────────────────────────────►│               │
 │                         │  Enqueue sync_to_graph       │               │
 │                         │─────────────────────────────►│               │
 │                         │                              │               │
 │                         │  [Worker picks up job]        │               │
 │                         │◄────────────────────────────│               │
 │                         │  extract_entities()           │               │
 │                         │─────────────────────────────────────────────►│
 │                         │  extract_entities complete    │               │
 │                         │  → enqueue embed_entity      │               │
 │                         │─────────────────────────────►│               │
 │                         │  → enqueue refresh_cache     │               │
 │                         │─────────────────────────────►│               │
```

### 8.2 Single Orchestrator (Phase 2+)

```
API                     ARQ Worker                     Redis          PostgreSQL
 │                        │                              │               │
 │ POST /memory            │                              │               │
 │────────────────────────►│                              │               │
 │ 202 Accepted            │                              │               │
 │◄────────────────────────│                              │               │
 │                         │  Write episode               │               │
 │                         │─────────────────────────────►│               │
 │                         │  Enqueue process_memory      │               │
 │                         │─────────────────────────────►│               │
 │                         │                              │               │
 │                         │  [Worker picks up job]        │               │
 │                         │◄────────────────────────────│               │
 │                         │                              │               │
 │                         │  Step 1: extract_entities    │               │
 │                         │─────────────────────────────────────────────►│
 │                         │  Set status bit 1            │               │
 │                         │─────────────────────────────────────────────►│
 │                         │                              │               │
 │                         │  Step 2: extract_facts       │               │
 │                         │─────────────────────────────────────────────►│
 │                         │  Set status bit 2            │               │
 │                         │─────────────────────────────────────────────►│
 │                         │                              │               │
 │                         │  ... (continue through steps) │               │
 │                         │                              │               │
 │                         │  Step N: Set status COMPLETE │               │
 │                         │─────────────────────────────────────────────►│
 │                         │                              │               │
 │                         │  process_memory complete      │               │
 │                         │◄────────────────────────────│               │
```

---

## 9. Testing

### 9.1 Event-Driven Test

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_event_driven_chain(async_client, auth_headers, arq_redis):
    """When a message is ingested, all enrichment tasks should eventually complete."""
    # Ingest
    response = await async_client.post(
        "/v1/users/user_123/memory",
        json={
            "session_id": "session_abc",
            "messages": [
                {"role": "user", "content": "I prefer Python over JavaScript."}
            ],
        },
        headers=auth_headers,
    )
    assert response.status_code == 202

    # Wait for ARQ to process all jobs
    await asyncio.sleep(5)

    # Check that enrichment_status is COMPLETE
    episode = await episode_repo.get_by_content_hash("...")
    assert episode.enrichment_status & 128  # COMPLETE bit set
```

### 9.2 Orchestrator Test

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_orchestrator_resume_after_crash(arq_worker, episode_factory):
    """If the orchestrator crashes mid-pipeline, it resumes from the last completed step."""
    # Create an episode with only entity extraction completed (status=1)
    episode = await episode_factory(enrichment_status=1)

    # Enqueue process_memory (simulates crash recovery)
    await arq_worker.redis.enqueue_job("process_memory", **build_payload(episode))
    await asyncio.sleep(10)

    # Verify — all steps completed, status is COMPLETE
    refreshed = await episode_repo.get_by_id(episode.id)
    assert refreshed.enrichment_status & 128  # COMPLETE
    assert refreshed.enrichment_status & 2    # facts extracted
    assert refreshed.enrichment_status & 4    # dialog classified
```

---

## 10. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_ORCHESTRATOR` | `False` | Use single orchestrator (True) or event-driven (False). Enable in Phase 2+. |
| `ORCHESTRATOR_TIMEOUT` | `600` | Orchestrator job timeout in seconds (sum of all step timeouts). |
| `ORCHESTRATOR_MAX_RETRIES` | `2` | Max retries for the orchestrator job (each retry resumes from first uncompleted step). |

---

## 11. SRS Traceability

| SRS ID | Requirement | How Covered |
|--------|-------------|-------------|
| WRK-01 | NLP enrichment via ARQ workers | Both approaches use ARQ for async execution |
| WRK-02 | Idempotent tasks | enrichment_status bits prevent duplicate execution |
| WRK-05 | Horizontal scaling | Both approaches support multiple worker processes |
| ING-04 | HTTP 202 on ingestion | Both approaches enqueue async work and return immediately |
| NLP-01–NLP-14 | All enrichment tasks | Either event-driven chains or orchestrator steps |

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
