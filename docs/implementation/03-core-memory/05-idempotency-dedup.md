# Idempotency & Deduplication — Implementation Guide

> **Domain:** Core Memory
> **SRS Phase:** Phase 1 — Core Memory (Week 3)
> **Requirements:** ING-08 (idempotency), WRK-02 (worker idempotency), SEC-09
> **Doc Dependencies:** [01-message-ingestion.md](01-message-ingestion.md), [03-hybrid-retrieval.md](03-hybrid-retrieval.md), [04-caching-strategy.md](04-caching-strategy.md)

---

## 1. Overview

OpenZep implements idempotency and deduplication at four layers, each handling a different failure mode:

| Layer | Failure Mode | Mechanism | TTL |
|-------|-------------|-----------|-----|
| **HTTP-level** (ING-08) | Client retries same HTTP request | `Idempotency-Key` header → cached response in Redis | 48h |
| **Content-level** | Different clients send identical payload | SHA-256 hash of `(user_id, session_id, messages)` → stored in Redis | 48h |
| **Worker-level** (WRK-02) | ARQ retries or duplicates a task | `episodes.enrichment_status` column with `SELECT ... FOR UPDATE` | Persistent |
| **Graph-level** | Graphiti entity creation on retry | Check entity existence by name + org_id before creating | Persistent |

### 1.1 Why Four Layers?

Each layer protects against a different entry point:

1. **HTTP-level**: The client's network library retried the POST request because the 202 response was lost in transit. The server received the same request twice with the same `Idempotency-Key`.
2. **Content-level**: Two different agent instances independently decided to persist the same conversation turns. Each uses a different `Idempotency-Key` (or none), but the content is identical.
3. **Worker-level**: ARQ's retry mechanism delivered the same task twice after a worker crash. Without checks, the enrichment pipeline would process the same episode twice.
4. **Graph-level**: Graphiti received the same entity creation command twice after a network hiccup. Without dedup, the graph would have duplicate entity nodes.

---

## 2. HTTP-Level Idempotency

### 2.1 Specification

| Field | Value |
|-------|-------|
| Header | `Idempotency-Key` |
| Location | Request header on `POST /v1/users/{user_id}/memory` and `POST /v1/users/{user_id}/facts` |
| Format | UUID v4 string (recommended) or any client-generated unique string |
| Max length | 255 characters |
| TTL | 48 hours after first request |
| Response header | `Idempotency-Key-Replayed: true/false` on all responses |

### 2.2 Behaviour

| Scenario | Response | Notes |
|----------|----------|-------|
| First request with key | 202, `Idempotency-Key-Replayed: false` | Normal processing |
| Duplicate request with same key | 202, `Idempotency-Key-Replayed: true` | Returns cached response; no side effects |
| Same key, different payload | 422 | Conflict — idempotency key already used with different request body |
| Key > 255 chars | 400 | Validation error |
| Key replayed after 48h | 202, `Idempotency-Key-Replayed: false` | TTL expired; treated as new request |

### 2.3 Implementation

```python
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import Header, HTTPException, Request, Response, status
from redis import asyncio as aioredis

from app.core.config import settings
from app.schemas.memory import MemoryResponse


class IdempotencyService:
    """Handles HTTP-level idempotency via Redis-backed Idempotency-Key.

    Key schema:
        OpenZep:{env}:idempotency:{key_value}
    Value schema:
        {
            "status_code": 202,
            "response_body": {...},
            "request_body_hash": "sha256...",
            "created_at": "2026-06-05T10:00:00Z"
        }
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._prefix = f"OpenZep:{settings.ENVIRONMENT}:idempotency:"
        self._ttl = settings.IDEMPOTENCY_TTL_SECONDS  # 172800 (48h)
        self._max_key_length = 255

    async def check_and_process(
        self,
        idempotency_key: str | None,
        request_body: dict[str, Any],
        process_fn: Any,
        response: Response,
    ) -> dict[str, Any]:
        """Idempotency check with automatic processing for first-time keys.

        Args:
            idempotency_key: The Idempotency-Key header value (or None).
            request_body: Parsed request body (for hash comparison).
            process_fn: Async callable that processes the request.
            response: FastAPI Response object (for setting headers).

        Returns:
            Response body dict.

        Raises:
            HTTPException 422 if key replayed with different payload.
        """
        if idempotency_key is None:
            # No idempotency key — process normally
            result = await process_fn()
            return result

        # Validate key length
        if len(idempotency_key) > self._max_key_length:
            raise HTTPException(
                status_code=400,
                detail=f"Idempotency-Key must not exceed {self._max_key_length} characters",
            )

        cache_key = self._prefix + idempotency_key
        request_hash = self._hash_request_body(request_body)

        # ── Check existing ──
        cached = await self._redis.get(cache_key)
        if cached is not None:
            entry = json.loads(cached)

            # Verify payload match
            if entry["request_body_hash"] != request_hash:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "Idempotency-Key already used with a different request body. "
                        "Each Idempotency-Key must uniquely identify a single request."
                    ),
                )

            # Replay cached response
            response.headers["Idempotency-Key-Replayed"] = "true"
            return entry["response_body"]

        # ── Process first-time request ──
        result = await process_fn()

        # Cache the result
        entry = {
            "status_code": 202,
            "response_body": result,
            "request_body_hash": request_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._redis.setex(cache_key, self._ttl, json.dumps(entry))

        response.headers["Idempotency-Key-Replayed"] = "false"
        return result

    @staticmethod
    def _hash_request_body(body: dict[str, Any]) -> str:
        """Compute SHA-256 hash of the canonical JSON request body."""
        import hashlib
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

### 2.4 Router Integration

```python
@router.post(
    "/v1/users/{user_id}/memory",
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_messages(
    user_id: str,
    payload: MemoryRequest,
    request: Request,
    response: Response,
    org_id: str = Depends(get_api_key_org),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> MemoryResponse:
    """Ingest messages with idempotency support."""
    idempotency = IdempotencyService(redis=request.app.state.redis)

    return await idempotency.check_and_process(
        idempotency_key=idempotency_key,
        request_body=payload.model_dump(),
        process_fn=lambda: MemoryService(
            db=db, org_id=org_id, redis=request.app.state.redis
        ).ingest(
            external_user_id=user_id,
            messages=payload.messages,
            session_id=payload.session_id,
        ),
        response=response,
    )
```

### 2.5 Testing

```python
@pytest.mark.asyncio
async def test_idempotency_first_request():
    """First request with Idempotency-Key returns processed result."""
    redis = AsyncMock()
    redis.get.return_value = None  # No cached entry
    redis.setex = AsyncMock()

    service = IdempotencyService(redis=redis)
    process_fn = AsyncMock(return_value={"status": "accepted", "job_id": "job_1"})
    response = MagicMock(headers={})

    result = await service.check_and_process(
        idempotency_key="key-123",
        request_body={"messages": [{"role": "user", "content": "Hello"}]},
        process_fn=process_fn,
        response=response,
    )

    assert result["status"] == "accepted"
    assert response.headers["Idempotency-Key-Replayed"] == "false"
    process_fn.assert_called_once()


@pytest.mark.asyncio
async def test_idempotency_replay():
    """Duplicate request returns cached result without calling process_fn."""
    redis = AsyncMock()
    cached_entry = {
        "status_code": 202,
        "response_body": {"status": "accepted", "job_id": "job_1"},
        "request_body_hash": IdempotencyService._hash_request_body(
            {"messages": [{"role": "user", "content": "Hello"}]}
        ),
        "created_at": "2026-06-05T10:00:00Z",
    }
    redis.get.return_value = json.dumps(cached_entry).encode()

    service = IdempotencyService(redis=redis)
    process_fn = AsyncMock()  # Should NOT be called
    response = MagicMock(headers={})

    result = await service.check_and_process(
        idempotency_key="key-123",
        request_body={"messages": [{"role": "user", "content": "Hello"}]},
        process_fn=process_fn,
        response=response,
    )

    assert result["job_id"] == "job_1"
    assert response.headers["Idempotency-Key-Replayed"] == "true"
    process_fn.assert_not_called()


@pytest.mark.asyncio
async def test_idempotency_different_payload():
    """Same key with different payload returns 422."""
    redis = AsyncMock()
    cached_entry = {
        "status_code": 202,
        "response_body": {"status": "accepted"},
        "request_body_hash": IdempotencyService._hash_request_body(
            {"messages": [{"role": "user", "content": "First message"}]}
        ),
        "created_at": "2026-06-05T10:00:00Z",
    }
    redis.get.return_value = json.dumps(cached_entry).encode()

    service = IdempotencyService(redis=redis)
    response = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await service.check_and_process(
            idempotency_key="key-123",
            request_body={"messages": [{"role": "user", "content": "Different message"}]},
            process_fn=AsyncMock(),
            response=response,
        )

    assert exc.value.status_code == 422
```

---

## 3. Content-Level Deduplication

### 3.1 Specification

| Field | Value |
|-------|-------|
| Mechanism | SHA-256 hash of `(user_id, session_id, JSON(messages))` |
| Storage | Redis key `OpenZep:{env}:contenthash:{sha256_hex}` |
| Value | `job_id` string |
| TTL | 48 hours |
| Scope | Per user + session + content combination |
| Effect | If exact same content is ingested (from any client, with any Idempotency-Key), return existing job_id without re-inserting episodes |

### 3.2 Why Separate from HTTP Idempotency?

Two scenarios where content dedup catches what HTTP idempotency misses:

1. **Different keys, same content:** Client A sends content with `Idempotency-Key: X`, client B sends identical content with `Idempotency-Key: Y`. HTTP idempotency sees two different keys — both pass. Content dedup catches the duplicate.

2. **No keys, same content:** Either client sends no `Idempotency-Key`. Content dedup prevents double-insertion.

### 3.3 Implementation

```python
import hashlib
import json
from typing import Any

from redis import asyncio as aioredis

from app.core.config import settings


class ContentDedupService:
    """Content-level deduplication using SHA-256 hashing.

    Prevents the same (user_id, session_id, messages) combination
    from being ingested more than once within the TTL window.
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._prefix = f"OpenZep:{settings.ENVIRONMENT}:contenthash:"
        self._ttl = settings.IDEMPOTENCY_TTL_SECONDS  # 48h

    def compute_hash(
        self,
        user_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> str:
        """Compute the content hash for dedup.

        Uses sort_keys=True to ensure canonical JSON regardless
        of key ordering in the messages dict.
        """
        canonical = json.dumps(
            {
                "user_id": user_id,
                "session_id": session_id,
                "messages": [
                    {
                        "role": m.get("role"),
                        "content": m.get("content"),
                        # Metadata is NOT included in the hash.
                        # Two requests with same content but different
                        # metadata should still be deduplicated.
                    }
                    for m in messages
                ],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def check(self, content_hash: str) -> str | None:
        """Check if content hash exists in Redis.

        Returns existing job_id if found, None otherwise.
        """
        cached = await self._redis.get(self._prefix + content_hash)
        if cached is not None:
            return cached.decode()
        return None

    async def store(self, content_hash: str, job_id: str) -> None:
        """Store content hash with TTL."""
        await self._redis.setex(
            self._prefix + content_hash,
            self._ttl,
            job_id,
        )
```

### 3.4 Integration in MemoryService

See `01-message-ingestion.md` §4.1 for the full integration. The content dedup check happens **after** session resolution but **before** batch-inserting episodes:

```python
async def ingest(self, ...) -> MemoryResponse:
    # ... user resolution, session resolution ...

    # ── Content-level dedup ──
    content_hash = self._content_dedup.compute_hash(
        user_id=str(user.id),
        session_id=str(session.id),
        messages=messages,
    )
    existing_job_id = await self._content_dedup.check(content_hash)
    if existing_job_id:
        return MemoryResponse(
            status="accepted",
            job_id=existing_job_id,
            message_count=len(messages),
            session_id=str(session.external_id),
        )

    # ... proceed with batch insert ...
```

---

## 4. Worker-Level Idempotency

### 4.1 Specification

| Field | Value |
|-------|-------|
| Mechanism | `episodes.enrichment_status` column with `SELECT ... FOR UPDATE` |
| Status values | `pending`, `processing`, `completed`, `failed` |
| Scope | Per episode ID |
| Recovery | ARQ retry (max 3 attempts), then dead-letter queue |
| Race condition prevention | `SELECT ... FOR UPDATE` within a transaction |

### 4.2 Database Column

```sql
-- Add enrichment_status to episodes table (via Alembic migration)
ALTER TABLE episodes
ADD COLUMN enrichment_status TEXT NOT NULL DEFAULT 'pending'
CHECK (enrichment_status IN ('pending', 'processing', 'completed', 'failed'));
```

### 4.3 Worker Task Pattern

Every enrichment worker task follows the same idempotent pattern:

```python
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def process_episode_with_idempotency(
    db: AsyncSession,
    episode_id: str,
    process_func,
) -> None:
    """Idempotent episode processing with row-level locking.

    Args:
        db: Database session (must be in a transaction).
        episode_id: UUID of the episode to process.
        process_func: Async callable that does the actual work.
            Receives (db, episode_id) as arguments.

    Raises:
        EpisodeAlreadyProcessed: If episode is already completed.
        EpisodeProcessingFailed: If status is 'failed' from a prior attempt.
    """
    # ── Atomically claim this episode ──
    result = await db.execute(
        text("""
            SELECT id, enrichment_status
            FROM episodes
            WHERE id = :episode_id
            FOR UPDATE  -- Row-level lock prevents concurrent processing
        """),
        {"episode_id": episode_id},
    )
    row = result.one_or_none()

    if row is None:
        raise ValueError(f"Episode {episode_id} not found")

    status = row.enrichment_status

    if status == "completed":
        # Already processed — idempotent skip
        logger.info("Episode already processed, skipping",
                    extra={"episode_id": episode_id, "task": process_func.__name__})
        return

    if status == "failed":
        # Previous attempt failed. Retrying is allowed (max 3 retries in ARQ).
        # Reset to 'processing' and proceed.
        logger.warning("Episode previously failed, retrying",
                       extra={"episode_id": episode_id, "task": process_func.__name__})

    if status == "processing":
        # Another worker may have claimed this. Check for stuck tasks.
        # If the task was claimed > 5 minutes ago, it's likely stuck.
        # In practice, ARQ's timeout should prevent this, but we handle it.
        logger.warning("Episode already being processed, skipping (possible duplicate)",
                       extra={"episode_id": episode_id, "task": process_func.__name__})
        return

    # ── Mark as processing ──
    await db.execute(
        text("UPDATE episodes SET enrichment_status = 'processing' WHERE id = :episode_id"),
        {"episode_id": episode_id},
    )
    await db.flush()

    # ── Execute the actual work ──
    try:
        await process_func(db, episode_id)

        # ── Mark as completed ──
        await db.execute(
            text("UPDATE episodes SET enrichment_status = 'completed' WHERE id = :episode_id"),
            {"episode_id": episode_id},
        )
        await db.commit()

    except Exception:
        await db.rollback()
        # Mark as failed for observability
        async with db.begin():
            await db.execute(
                text("UPDATE episodes SET enrichment_status = 'failed' WHERE id = :episode_id"),
                {"episode_id": episode_id},
            )
        raise  # ARQ will retry (up to 3 times)
```

### 4.4 ARQ Task Wrapper

```python
# services/worker/tasks/entity_extraction.py

from arq import Retry
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal


async def extract_entities_task(ctx, org_id: str, user_id: str, session_id: str, episode_ids: list[str]):
    """ARQ task: extract entities from episodes.

    This function wraps the idempotent processing pattern.
    """
    async with AsyncSessionLocal() as db:
        try:
            async with db.begin():
                for episode_id in episode_ids:
                    await process_episode_with_idempotency(
                        db=db,
                        episode_id=episode_id,
                        process_func=_do_extract_entities,
                    )
        except Exception as e:
            # Log failure
            logger.error("Entity extraction task failed",
                         extra={
                             "org_id": org_id,
                             "user_id": user_id,
                             "session_id": session_id,
                             "episode_ids": episode_ids,
                             "error": str(e),
                         })
            raise Retry(defer=ctx["job_try"] * 10)  # Exponential backoff: 10s, 20s, 30s


async def _do_extract_entities(db: AsyncSession, episode_id: str) -> None:
    """Actual entity extraction logic.

    This is called ONLY after the idempotency check passes.
    """
    # Fetch episode content
    result = await db.execute(
        text("SELECT role, content FROM episodes WHERE id = :episode_id"),
        {"episode_id": episode_id},
    )
    episode = result.one_or_none()
    if episode is None:
        return

    # Call LLM for entity extraction
    entities = await llm_extract_entities(episode.content)

    # Persist entities to DB + Graphiti
    await entity_repo.batch_insert(episode_id=episode_id, entities=entities)
```

### 4.5 Stuck Task Detection

If a worker crashes while `enrichment_status = 'processing'`, the episode stays stuck in that state. A **stuck task reconciler** runs on a schedule:

```python
async def reconcile_stuck_tasks(db: AsyncSession) -> None:
    """Find episodes stuck in 'processing' state for > 5 minutes.

    Reset them to 'pending' so they can be re-queued.
    """
    result = await db.execute(
        text("""
            UPDATE episodes
            SET enrichment_status = 'pending'
            WHERE enrichment_status = 'processing'
              AND updated_at < NOW() - INTERVAL '5 minutes'
            RETURNING id
        """),
    )
    stuck_ids = [row.id for row in result.fetchall()]

    if stuck_ids:
        logger.warning(f"Reset {len(stuck_ids)} stuck episodes to pending",
                       extra={"episode_ids": [str(i) for i in stuck_ids]})
        # Optionally re-enqueue ARQ tasks for these episodes
        await re_enqueue_tasks(stuck_ids)
```

---

## 5. Graph-Level Deduplication

### 5.1 Specification

| Field | Value |
|-------|-------|
| Mechanism | Check entity existence by `(name, org_id)` before creating |
| Scope | Per entity name + organisation |
| Effect | If entity already exists, add relationship to existing node instead of creating duplicate |

### 5.2 Implementation

```python
class GraphitiDedupService:
    """Deduplication for Graphiti entity and relationship creation."""

    def __init__(self, graphiti_client) -> None:
        self._graphiti = graphiti_client

    async def get_or_create_entity(
        self,
        org_id: str,
        name: str,
        entity_type: str,
        properties: dict | None = None,
    ) -> str:
        """Get existing entity node or create a new one.

        Checks for existing entity by (name, org_id) before creating.

        Args:
            org_id: Organisation namespace.
            name: Entity name (e.g., "Alice", "Acme Corp").
            entity_type: Entity type (e.g., "Person", "Organization").
            properties: Optional entity properties.

        Returns:
            Entity node UUID (existing or newly created).
        """
        # Check for existing entity
        existing = await self._graphiti.find_entity(
            org_id=org_id,
            name=name,
            entity_type=entity_type,
        )

        if existing is not None:
            logger.info("Entity already exists, returning existing node",
                        extra={"org_id": org_id, "name": name, "entity_id": existing["uuid"]})
            return existing["uuid"]

        # Create new entity
        entity = await self._graphiti.create_entity_node(
            org_id=org_id,
            name=name,
            entity_type=entity_type,
            properties=properties or {},
        )
        return entity["uuid"]

    async def get_or_create_relationship(
        self,
        org_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relationship_type: str = "RELATES_TO",
        properties: dict | None = None,
    ) -> str:
        """Get existing relationship or create a new one.

        Checks for existing relationship between the same source and target.
        """
        existing = await self._graphiti.find_relationship(
            org_id=org_id,
            source_id=source_entity_id,
            target_id=target_entity_id,
            relationship_type=relationship_type,
        )

        if existing is not None:
            return existing["uuid"]

        rel = await self._graphiti.create_relationship(
            org_id=org_id,
            source_id=source_entity_id,
            target_id=target_entity_id,
            relationship_type=relationship_type,
            properties=properties or {},
        )
        return rel["uuid"]
```

---

## 6. Failure Scenarios & Recovery

### 6.1 Worker Crashes Mid-Processing

```
Timeline:
1. ARQ picks up task, calls extract_entities_task
2. Task enters process_episode_with_idempotency
3. Status set to 'processing' via UPDATE ... FOR UPDATE
4. Worker crashes (OOM, node failure, SIGKILL)
5. ARQ detects timeout (job_timeout = 300s)
6. ARQ retries task (up to 3 attempts)
7. On next attempt: status is still 'processing'
   - If < 5min since last attempt: skip (assume another worker is handling it)
   - If > 5min: stuck task reconciler resets to 'pending', task can proceed
8. After 3 failed attempts: task moves to dead-letter queue (DLQ)
```

### 6.2 Duplicate ARQ Enqueue

```
Scenario: Service layer enqueues the same task twice (rare — ARQ pool issue).

1. Two workers pick up the same episode_id
2. Both enter process_episode_with_idempotency
3. Worker A acquires row lock first (FOR UPDATE)
4. Worker B blocks on row lock
5. Worker A processes, sets status to 'completed', commits
6. Worker B acquires lock, sees status = 'completed', skips
```

### 6.3 Graphiti Node Creation After Retry

```
Scenario: sync_to_graph worker retries after creating the entity node
but crashing before creating relationships.

1. First attempt: entity node created, then worker crashes
2. Retry: get_or_create_entity finds existing node → returns existing UUID
3. Worker creates relationships (no duplicates)
```

---

## 7. Summary: Idempotency Decision Tree

```
Client sends POST /memory

        │
        ▼
┌─────────────────────┐
│ Has Idempotency-Key? │
└─────────┬───────────┘
     Yes   │   No
     │     │     │
     ▼     │     ▼
┌────────┐ │ ┌────────┐
│ HTTP   │ │ │ Proceed│
│ Check  │ │ │ without│
└───┬────┘ │ │ HTTP   │
    │      │ │ idempot│
    │      │ └────────┘
    ▼      │
┌────────┐ │
│ Cached? │ │
└───┬────┘ │
   Yes│    │
    │ │   │
    ▼ │   │
┌──────────────────────┐
│ Return cached 202    │ ←── Idempotent response
└──────────────────────┘

        No
        │
        ▼
┌─────────────────────────┐
│ Content-level dedup     │
│ SHA-256(user+session+msgs)│
└───────────┬─────────────┘
         Dup │   │ New
            │   │
            ▼   ▼
     ┌──────────────┐
     │ Return same  │
     │ job_id (202) │
     └──────────────┘
                 │
                 ▼
    ┌────────────────────┐
    │ Batch-insert       │
    │ episodes (Postgres)│
    └────────┬───────────┘
             │
             ▼
    ┌────────────────────┐
    │ Enqueue ARQ tasks  │
    │ (sync_to_graph,    │
    │  extract_entities, │
    │  extract_facts,    │
    │  embed_episode)    │
    └────────┬───────────┘
             │
             ▼
    ┌────────────────────┐
    │ Worker checks      │
    │ enrichment_status  │
    │ (FOR UPDATE lock)  │
    └────────┬───────────┘
         Done│   │Pending
            │   │
            ▼   ▼
     ┌──────────────┐
     │ Skip (done)  │
     └──────────────┘
                 │
                 ▼
    ┌────────────────────┐
    │ Process & mark     │
    │ completed (or fail)│
    └────────────────────┘
```

---

## 8. Testing Guide

### 8.1 Unit Tests

```python
@pytest.mark.asyncio
async def test_content_dedup_compute_hash():
    """Verify content hash is deterministic regardless of key order."""
    dedup = ContentDedupService(redis=AsyncMock())

    messages_a = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi!"},
    ]
    messages_b = [
        {"assistant": "assistant", "content": "Hi!", "role": "assistant"},
        {"role": "user", "content": "Hello"},
    ]

    hash_a = dedup.compute_hash(
        user_id="u1", session_id="s1", messages=messages_a
    )
    hash_b = dedup.compute_hash(
        user_id="u1", session_id="s1", messages=messages_b
    )

    assert hash_a == hash_b


@pytest.mark.asyncio
async def test_content_dedup_different_users_different_hash():
    """Verify different user produces different hash for same content."""
    dedup = ContentDedupService(redis=AsyncMock())

    messages = [{"role": "user", "content": "Hello"}]

    hash_a = dedup.compute_hash(user_id="u1", session_id="s1", messages=messages)
    hash_b = dedup.compute_hash(user_id="u2", session_id="s1", messages=messages)

    assert hash_a != hash_b


@pytest.mark.asyncio
async def test_content_dedup_metadata_excluded():
    """Verify metadata differences do NOT change the hash."""
    dedup = ContentDedupService(redis=AsyncMock())

    messages_no_meta = [{"role": "user", "content": "Hello"}]
    messages_with_meta = [
        {"role": "user", "content": "Hello", "metadata": {"tag": "important"}}
    ]

    hash_no_meta = dedup.compute_hash(user_id="u1", session_id="s1", messages=messages_no_meta)
    hash_with_meta = dedup.compute_hash(user_id="u1", session_id="s1", messages=messages_with_meta)

    assert hash_no_meta == hash_with_meta
```

### 8.2 Worker Idempotency Tests

```python
@pytest.mark.asyncio
async def test_worker_skips_completed_episode():
    """Verify worker skips episodes already marked completed."""
    db = AsyncMock()
    db.execute.return_value.fetchall.return_value = []

    # Episode exists, status = 'completed'
    db.execute.return_value.one_or_none.return_value = MagicMock(
        id=UUID("1111"),
        enrichment_status="completed",
    )

    process_func = AsyncMock()  # Should NOT be called

    await process_episode_with_idempotency(
        db=db,
        episode_id="1111",
        process_func=process_func,
    )

    process_func.assert_not_called()


@pytest.mark.asyncio
async def test_worker_processes_pending_episode():
    """Verify worker processes episodes with status 'pending'."""
    db = AsyncMock()

    # First call: return pending episode
    db.execute.return_value.one_or_none.return_value = MagicMock(
        id=UUID("1111"),
        enrichment_status="pending",
    )

    process_func = AsyncMock()

    await process_episode_with_idempotency(
        db=db,
        episode_id="1111",
        process_func=process_func,
    )

    process_func.assert_called_once()


@pytest.mark.asyncio
async def test_worker_concurrent_processing_second_skips():
    """Verify second concurrent worker skips if first is processing."""
    db = AsyncMock()
    # Simulate SELECT ... FOR UPDATE returning 'processing' status
    db.execute.return_value.one_or_none.return_value = MagicMock(
        id=UUID("1111"),
        enrichment_status="processing",
    )

    process_func = AsyncMock()

    await process_episode_with_idempotency(
        db=db,
        episode_id="1111",
        process_func=process_func,
    )

    process_func.assert_not_called()


@pytest.mark.asyncio
async def test_worker_retries_failed_episode():
    """Verify worker retries episodes marked as 'failed'."""
    db = AsyncMock()

    # Episode exists, status = 'failed'
    db.execute.return_value.one_or_none.return_value = MagicMock(
        id=UUID("1111"),
        enrichment_status="failed",
    )

    process_func = AsyncMock()

    await process_episode_with_idempotency(
        db=db,
        episode_id="1111",
        process_func=process_func,
    )

    process_func.assert_called_once()
```

### 8.3 Integration Tests

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_idempotency_end_to_end(
    async_client: AsyncClient,
    auth_headers: dict,
    seed_user: dict,
):
    """E2E: same Idempotency-Key returns same result."""
    headers = {**auth_headers, "Idempotency-Key": "e2e-test-key"}

    payload = {"messages": [{"role": "user", "content": "E2E idempotency test"}]}

    # First request
    resp1 = await async_client.post(
        f"/v1/users/{seed_user['external_id']}/memory",
        json=payload,
        headers=headers,
    )
    assert resp1.status_code == 202
    assert resp1.headers["Idempotency-Key-Replayed"] == "false"
    job_id_1 = resp1.json()["job_id"]

    # Second request (same key, same payload)
    resp2 = await async_client.post(
        f"/v1/users/{seed_user['external_id']}/memory",
        json=payload,
        headers=headers,
    )
    assert resp2.status_code == 202
    assert resp2.headers["Idempotency-Key-Replayed"] == "true"
    assert resp2.json()["job_id"] == job_id_1

    # Verify only one set of episodes exists in DB
    # (Integration test helper checks episodes table)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_content_dedup_different_keys_same_content(
    async_client: AsyncClient,
    auth_headers: dict,
    seed_user: dict,
):
    """Same content with different Idempotency-Keys returns same job_id."""
    payload = {"messages": [{"role": "user", "content": "Content dedup test"}]}

    # Request with key A
    resp_a = await async_client.post(
        f"/v1/users/{seed_user['external_id']}/memory",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "key-a"},
    )
    assert resp_a.status_code == 202
    job_id = resp_a.json()["job_id"]

    # Request with key B (different key, same content)
    resp_b = await async_client.post(
        f"/v1/users/{seed_user['external_id']}/memory",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "key-b"},
    )
    assert resp_b.status_code == 202
    assert resp_b.json()["job_id"] == job_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_idempotency_different_payload_same_key_422(
    async_client: AsyncClient,
    auth_headers: dict,
    seed_user: dict,
):
    """Same key with different payload returns 422."""
    headers = {**auth_headers, "Idempotency-Key": "conflict-key"}

    # First request
    await async_client.post(
        f"/v1/users/{seed_user['external_id']}/memory",
        json={"messages": [{"role": "user", "content": "Original"}]},
        headers=headers,
    )

    # Second request with different payload
    resp = await async_client.post(
        f"/v1/users/{seed_user['external_id']}/memory",
        json={"messages": [{"role": "user", "content": "Different"}]},
        headers=headers,
    )
    assert resp.status_code == 422
```

---

## 9. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `IDEMPOTENCY_TTL_SECONDS` | `172800` | TTL for idempotency keys and content hashes (48h) |
| `WORKER_STUCK_TIMEOUT_MINUTES` | `5` | Time after which a `processing` episode is considered stuck |
| `ARQ_MAX_RETRIES` | `3` | Maximum ARQ task retry attempts |
| `ARQ_JOB_TIMEOUT` | `300` | ARQ job timeout in seconds (5 min) |

---

## 10. Open Questions

| # | Question | Decision |
|---|----------|----------|
| OQ-1 | Should content hash include metadata? | No — two requests with identical messages but different metadata should still be deduplicated. Metadata is not semantically important for dedup. |
| OQ-2 | 48h TTL for idempotency — is that enough? | Yes — clients should not retry a request beyond 48h. If they do, it's safe to treat as a new request (old episodes are already committed). |
| OQ-3 | Should we add a unique constraint on `(request_body_hash)` in PostgreSQL as a safety net? | Possibly as a P2 hardening step. For P0, Redis-backed dedup is sufficient. A DB constraint would require adding the hash column to the episodes table. |

---

*Document maintained by the OpenZep team. Update this document if idempotency mechanisms are extended to new endpoints or layers.*
