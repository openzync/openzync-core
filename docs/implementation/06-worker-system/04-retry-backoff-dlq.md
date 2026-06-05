# Retry, Backoff & Dead-Letter Queue

> **Phase:** 1 (Core Memory) — retry/backoff; Phase 2 — DLQ
> **SRS Requirements:** WRK-03, WRK-06, WRK-07
> **Dependencies:** [01-arq-setup.md](01-arq-setup.md), [02-task-definitions.md](02-task-definitions.md)
> **Design Authority:** @devops (DLQ inspection endpoints), @senior-dev (retry classification)

---

## 1. Overview

Every task can fail. MemGraph's retry and dead-letter queue system ensures that transient failures (LLM API timeout, DB connection blip) are retried automatically, while permanent failures (invalid payload, unrecoverable LLM error) are captured for human inspection.

### 1.1 Design Principles

1. **Transient != Permanent**: The system distinguishes between errors that will likely succeed on retry (network timeout, 429 rate limit) and errors that will never succeed (bad input, schema mismatch).
2. **Exponential backoff with jitter**: Retries are spaced with increasing delays plus random jitter to prevent thundering herd.
3. **Dead-letter queue (DLQ)**: Tasks that exhaust retries are moved to a DLQ for inspection and manual re-queue.
4. **Configurable per task**: Each task type has its own timeout, max retries, and backoff base — overridable via environment variables.

---

## 2. Retry Policy Per Task Type

### 2.1 Configuration Source

```python
# services/worker/config.py

from pydantic import Field
from pydantic_settings import BaseSettings
from typing import ClassVar


class TaskRetryConfig(BaseSettings):
    """Per-task retry and timeout configuration.

    Each task can be configured via env vars:
        {TASK_NAME_UPPER}_TIMEOUT    — max execution time in seconds
        {TASK_NAME_UPPER}_MAX_RETRIES — max number of retry attempts

    The base backoff is set per task category (LLM vs DB) and is
    not configurable per task — this prevents accidental misconfiguration.

    Example:
        EXTRACT_ENTITIES_TIMEOUT=120
        EXTRACT_ENTITIES_MAX_RETRIES=3
    """

    model_config = {"env_prefix": "", "case_sensitive": False}

    # ── Core extraction tasks ───────────────────────────────
    EXTRACT_ENTITIES_TIMEOUT: int = 120
    EXTRACT_ENTITIES_MAX_RETRIES: int = 3

    EXTRACT_FACTS_TIMEOUT: int = 120
    EXTRACT_FACTS_MAX_RETRIES: int = 3

    # ── Embedding tasks ────────────────────────────────────
    EMBED_EPISODE_TIMEOUT: int = 60
    EMBED_EPISODE_MAX_RETRIES: int = 3

    EMBED_ENTITY_TIMEOUT: int = 60
    EMBED_ENTITY_MAX_RETRIES: int = 3

    # ── Classification tasks ────────────────────────────────
    CLASSIFY_DIALOG_TIMEOUT: int = 60
    CLASSIFY_DIALOG_MAX_RETRIES: int = 3

    EXTRACT_STRUCTURED_TIMEOUT: int = 180
    EXTRACT_STRUCTURED_MAX_RETRIES: int = 3

    # ── Batch / background tasks ────────────────────────────
    SUMMARISE_COMMUNITY_TIMEOUT: int = 600
    SUMMARISE_COMMUNITY_MAX_RETRIES: int = 2

    INGEST_BUSINESS_DATA_TIMEOUT: int = 60
    INGEST_BUSINESS_DATA_MAX_RETRIES: int = 3

    # ── Infrastructure tasks ────────────────────────────────
    SYNC_TO_GRAPH_TIMEOUT: int = 30
    SYNC_TO_GRAPH_MAX_RETRIES: int = 3

    DELETE_USER_DATA_TIMEOUT: int = 300
    DELETE_USER_DATA_MAX_RETRIES: int = 2

    MERGE_DUPLICATE_ENTITIES_TIMEOUT: int = 600
    MERGE_DUPLICATE_ENTITIES_MAX_RETRIES: int = 1

    REFRESH_CONTEXT_CACHE_TIMEOUT: int = 10
    REFRESH_CONTEXT_CACHE_MAX_RETRIES: int = 1

    # ── Task categories (for backoff base selection) ────────

    # Tasks that call LLM APIs (higher backoff base — 2s)
    LLM_TASKS: ClassVar[set[str]] = {
        "extract_entities",
        "extract_facts",
        "classify_dialog",
        "extract_structured",
        "summarise_community",
    }

    # Tasks that only hit DB/Redis (lower backoff base — 1s)
    DB_TASKS: ClassVar[set[str]] = {
        "embed_episode",
        "embed_entity",
        "sync_to_graph",
        "delete_user_data",
        "merge_duplicate_entities",
        "refresh_context_cache",
        "ingest_business_data",
    }


task_retry_config = TaskRetryConfig()


def get_task_timeout(task_name: str) -> int:
    """Get the timeout for a task, with env var override support."""
    env_var = f"{task_name.upper()}_TIMEOUT"
    return getattr(task_retry_config, env_var, task_retry_config.JOB_TIMEOUT_DEFAULT)


def get_task_max_retries(task_name: str) -> int:
    """Get the max retries for a task, with env var override support."""
    env_var = f"{task_name.upper()}_MAX_RETRIES"
    return getattr(task_retry_config, env_var, 3)
```

### 2.2 ARQ Retry Mechanism

ARQ supports retries natively via the `max_tries` parameter on `enqueue_job` and via raising `arq.Retry` in task functions:

```python
from arq import Retry


async def my_task(ctx: dict, **kwargs: Any) -> dict:
    try:
        result = await llm_client.extract(...)
    except LLMTimeoutError as exc:
        # Raise Retry to have ARQ re-enqueue the job.
        # ARQ tracks attempt count in ctx['job_try'] (1-based).
        attempt = ctx.get("job_try", 1)
        logger.warning("task.llm_timeout", attempt=attempt, max_retries=3)

        if attempt < 3:
            raise Retry(exc) from exc
        else:
            # Last attempt failed — don't retry again.
            # The task will fail and go to the DLQ.
            raise
```

### 2.3 How ARQ Handles Retries

| Parameter | Description | MemGraph Default |
|-----------|-------------|------------------|
| `max_tries` | Max job execution attempts (including first). Set at enqueue time. | Per-task, matches max_retries + 1 |
| `job_timeout` | Max wall-clock time per attempt. | Per-task timeout |
| `Retry` exception | Raised by task to request immediate re-queue. | Used for all transient errors |
| `ctx['job_try']` | Current attempt number (1 = first attempt, 2 = first retry, etc.) | Read-only in task |

> ⚠️ **ARQ limitation:** `max_tries` is set at enqueue time, not at task definition time. The enqueue call must pass the correct max_tries value. The worker helper `enqueue_with_retry` below handles this.

```python
# Helper function used by all enqueue call sites:

async def enqueue_with_retry(
    redis: ArqRedis,
    task_name: str,
    **kwargs: Any,
) -> ArqJob | None:
    """Enqueue a task with its configured timeout and max retries.

    This should be the ONLY way tasks are enqueued — it ensures
    consistent retry configuration across all call sites.
    """
    from services.worker.config import get_task_timeout, get_task_max_retries

    timeout = get_task_timeout(task_name)
    max_retries = get_task_max_retries(task_name)

    # ARQ's max_tries includes the first attempt, so max_tries = max_retries + 1
    return await redis.enqueue_job(
        task_name,
        _job_timeout=timeout,
        _max_tries=max_retries + 1,
        **kwargs,
    )
```

---

## 3. Exponential Backoff with Jitter

### 3.1 Formula

```
backoff = min(max_delay, base * 2^(attempt - 1)) + jitter

Where:
  base    = 2s for LLM tasks, 1s for DB tasks
  attempt = 1 (first retry), 2 (second retry), 3 (third retry)
  jitter  = random(0, 1000) ms
  max_delay = 60s (cap to prevent excessive wait)
```

### 3.2 Delay Table

| Attempt | LLM Task (base=2s) | DB Task (base=1s) |
|---------|-------------------|-------------------|
| 1st retry (attempt 2) | `min(60, 2*2¹) + jitter` = 4s + 0-1s = **4-5s** | `min(60, 1*2¹) + jitter` = 2s + 0-1s = **2-3s** |
| 2nd retry (attempt 3) | `min(60, 2*2²) + jitter` = 8s + 0-1s = **8-9s** | `min(60, 1*2²) + jitter` = 4s + 0-1s = **4-5s** |
| 3rd retry (attempt 4) | `min(60, 2*2³) + jitter` = 16s + 0-1s = **16-17s** | `min(60, 1*2³) + jitter` = 8s + 0-1s = **8-9s** |
| 4th retry (attempt 5) | `min(60, 2*2⁴) + jitter` = 32s + 0-1s = **32-33s** | `min(60, 1*2⁴) + jitter` = 16s + 0-1s = **16-17s** |
| 5th retry (attempt 6) | `min(60, 2*2⁵) + jitter` = 60s + 0-1s = **60-61s** (capped) | `min(60, 1*2⁵) + jitter` = 32s + 0-1s = **32-33s** |

### 3.3 Implementation

ARQ does not support custom backoff functions in its core library. We implement backoff by raising `Retry` with the desired delay:

```python
import random
import asyncio


async def execute_with_backoff(
    task_name: str,
    attempt: int,
    operation: callable,
    logger: structlog.BoundLogger,
) -> Any:
    """Execute an operation with exponential backoff and jitter.

    This wraps a single attempt. The caller (task function) is responsible
    for calling this and raising Retry with the computed delay.

    Args:
        task_name: Name of the task (for base time selection).
        attempt: Current attempt number (1-based, from ctx['job_try']).
        operation: Async callable to execute.
        logger: Structured logger instance.

    Returns:
        Result of the operation.

    Raises:
        Retry with computed delay for transient errors.
    """
    from services.worker.config import task_retry_config

    # Determine base delay
    if task_name in task_retry_config.LLM_TASKS:
        base_delay = 2.0  # seconds
        max_delay = 60.0
    elif task_name in task_retry_config.DB_TASKS:
        base_delay = 1.0  # seconds
        max_delay = 60.0
    else:
        base_delay = 1.0
        max_delay = 30.0

    # Compute exponential backoff
    exponential = base_delay * (2 ** (attempt - 1))
    capped = min(exponential, max_delay)

    # Add jitter: random 0-1000ms
    jitter = random.uniform(0, 1.0)  # 0 to 1 second
    total_delay = capped + jitter

    logger.info(
        "backoff.calculated",
        attempt=attempt,
        base_delay=base_delay,
        exponential=exponential,
        capped=capped,
        jitter_seconds=round(jitter, 3),
        total_delay_seconds=round(total_delay, 3),
    )

    await asyncio.sleep(total_delay)

    # After the delay, raise Retry to let ARQ re-enqueue
    from arq import Retry
    raise Retry("Transient error after backoff")
```

### 3.4 Usage in Tasks

```python
async def extract_entities(ctx: dict, **kwargs: Any) -> dict:
    attempt = ctx.get("job_try", 1)
    logger = structlog.get_logger("memgraph.worker.tasks")

    try:
        entities = await llm_client.extract_entities(text=kwargs["content"])
    except (LLMTimeoutError, LLMAPIError) as exc:
        logger.warning("task.llm_failed", attempt=attempt, error=str(exc))

        if attempt < get_task_max_retries("extract_entities"):
            # Raise Retry — ARQ will re-enqueue
            # But first, implement backoff via execute_with_backoff
            await execute_with_backoff("extract_entities", attempt, lambda: None, logger)
            # execute_with_backoff always raises Retry
        else:
            # Last attempt failed — let the task fail naturally
            # ARQ will move it to the DLQ (see §4)
            raise
    # ... rest of task ...
```

---

## 4. Retryable vs Non-Retryable Errors

### 4.1 Classification

| Error | Retryable? | Rationale |
|-------|-----------|-----------|
| **LLM 408 Request Timeout** | ✅ Yes | Transient — retry may succeed with a different backend node |
| **LLM 429 Rate Limited** | ✅ Yes | Backoff will reduce request rate |
| **LLM 5xx Server Error** | ✅ Yes | OpenAI/Azure can have transient server issues |
| **DB Connection Error** | ✅ Yes | Connection pool may have been exhausted transiently |
| **Network Timeout** | ✅ Yes | TCP-level transient failure |
| **Redis Connection Error** | ✅ Yes | Redis may be recovering from failover |
| **ValueError — Invalid payload** | ❌ No | Bad input will always be bad input |
| **LLM Invalid Response Format** | ❌ No (after 1 retry) | First retry: maybe the LLM output differs. Second failure: LLM is consistently returning bad output — human intervention needed. |
| **Graphiti BadRequest** | ❌ No | Client error — payload sent to Graphiti was malformed |
| **Pydantic Validation Error** | ❌ No | Schema mismatch — fix enqueue code or input |
| **Episode Not Found** | ❌ No | Data integrity issue — episode was deleted before task ran |

### 4.2 Implementation

```python
# core/exceptions.py

class RetryableError(Exception):
    """Base for transient errors that should be retried."""


class NonRetryableError(Exception):
    """Base for permanent errors that should go directly to DLQ."""


# Retryable
class LLMTimeoutError(RetryableError): ...
class LLMRateLimitError(RetryableError): ...
class LLMServerError(RetryableError): ...
class DBConnectionError(RetryableError): ...
class NetworkTimeoutError(RetryableError): ...
class RedisConnectionError(RetryableError): ...

# Non-retryable
class InvalidPayloadError(NonRetryableError): ...
class LLMInvalidResponseError(NonRetryableError): ...
class GraphitiBadRequestError(NonRetryableError): ...
class EpisodeNotFoundError(NonRetryableError): ...
```

```python
# Helper decorator for task functions:

from functools import wraps
from typing import Any, Callable

from arq import Retry
from core.exceptions import RetryableError, NonRetryableError


def with_retry_policy(task_name: str) -> Callable:
    """Decorator that applies MemGraph's retry policy to a task function.

    - RetryableError → raise Retry (ARQ handles re-enqueue with backoff)
    - NonRetryableError → let it propagate (task fails, goes to DLQ)
    - Unexpected error on last attempt → let it propagate
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(ctx: dict, **kwargs: Any) -> Any:
            attempt = ctx.get("job_try", 1)
            max_retries = get_task_max_retries(task_name)

            try:
                return await func(ctx, **kwargs)
            except RetryableError as exc:
                if attempt <= max_retries:
                    logger.warning(
                        "task.retryable_error",
                        task_name=task_name,
                        attempt=attempt,
                        max_retries=max_retries,
                        error=str(exc),
                    )
                    raise Retry(exc) from exc
                else:
                    # Exhausted retries — let it fail naturally
                    # The task will go to DLQ
                    logger.error(
                        "task.retries_exhausted",
                        task_name=task_name,
                        attempt=attempt,
                        error=str(exc),
                    )
                    raise
            except NonRetryableError as exc:
                # Immediately fail — no retry
                logger.error(
                    "task.non_retryable_error",
                    task_name=task_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise
            except Exception as exc:
                # Unexpected error — retry if within limits
                if attempt <= max_retries:
                    logger.warning(
                        "task.unexpected_error_retrying",
                        task_name=task_name,
                        attempt=attempt,
                        error=str(exc),
                    )
                    raise Retry(exc) from exc
                raise

        return wrapper
    return decorator
```

---

## 5. Dead-Letter Queue (DLQ)

### 5.1 Why a Custom DLQ?

ARQ does not have a native dead-letter queue. When a job exhausts its retries, ARQ simply marks it as failed and keeps the result in Redis (with TTL set by `keep_result_failed`). There is no built-in mechanism for:

- Inspecting all failed tasks
- Re-queuing a failed task
- Alerting on failure rates
- Auto-purging old failures

MemGraph implements these features using a Redis sorted set.

### 5.2 DLQ Data Structure

```
Redis Key: memgraph:{env}:dlq
Type: Sorted Set
Score: Unix timestamp of the failure (for TTL-based auto-purge)
Member: JSON string of failed task metadata
```

### 5.3 DLQ Entry Schema

```json
{
  "job_id": "e7a8b9c0d1f2...",
  "task_name": "extract_entities",
  "queue_name": "memgraph:prod:queue:high",
  "enqueued_at": "2026-06-05T10:29:00Z",
  "failed_at": "2026-06-05T10:30:00Z",
  "error": "LLM 504 Gateway Timeout after 120s",
  "error_type": "LLMTimeoutError",
  "attempts": 3,
  "trace_id": "req_01j9xmf...",
  "org_id": "org_abc123",
  "payload_snapshot": {
    "episode_id": "uuid...",
    "content_hash": "abc123..."
  }
}
```

### 5.4 DLQ Implementation

```python
# services/worker/dlq.py
"""Dead-letter queue implementation using Redis sorted sets.

All failed tasks (after exhausting retries) are recorded here
for inspection and manual re-queue via the admin API.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from arq.connections import ArqRedis


# Redis key for the DLQ sorted set
def dlq_key(env: str = "dev") -> str:
    return f"memgraph:{env}:dlq"


DLQ_MAX_AGE_DAYS = 7  # Auto-purge entries older than 7 days


async def add_to_dlq(
    redis: ArqRedis,
    env: str,
    job_id: str,
    task_name: str,
    queue_name: str,
    error: str,
    error_type: str,
    attempts: int,
    trace_id: str,
    org_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Record a failed task in the dead-letter queue.

    Called from the on_job_failed callback in worker.py.

    Args:
        redis: ARQ Redis connection.
        env: Environment name (dev, staging, prod).
        job_id: ARQ job ID.
        task_name: Name of the task function.
        queue_name: Full ARQ queue name.
        error: Error message string.
        error_type: Exception class name.
        attempts: Total number of attempts made.
        trace_id: Request trace ID for correlation.
        org_id: Organization ID.
        payload: Limited payload snapshot (never the full content — see SEC-02).
    """
    key = dlq_key(env)
    now = time.time()

    entry = {
        "job_id": job_id,
        "task_name": task_name,
        "queue_name": queue_name,
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "error_type": error_type,
        "attempts": attempts,
        "trace_id": trace_id,
        "org_id": org_id,
        # ⚠️ SECURITY: Never store full message content in DLQ.
        # Only metadata and hashes.
        "payload_snapshot": payload or {},
    }

    # Add to sorted set with timestamp score for TTL-based purge
    await redis.zadd(key, {json.dumps(entry): now})

    # Auto-purge entries older than 7 days
    cutoff = now - (DLQ_MAX_AGE_DAYS * 86400)
    await redis.zremrangebyscore(key, 0, cutoff)


async def get_dlq_entries(
    redis: ArqRedis,
    env: str,
    limit: int = 50,
    offset: int = 0,
    task_name: str | None = None,
    org_id: str | None = None,
) -> list[dict[str, Any]]:
    """List failed tasks in the DLQ, newest first.

    Args:
        redis: ARQ Redis connection.
        env: Environment name.
        limit: Max entries to return.
        offset: Pagination offset.
        task_name: Optional filter by task name.
        org_id: Optional filter by org ID.

    Returns:
        List of DLQ entry dicts.
    """
    key = dlq_key(env)
    now = time.time()

    # Get entries in reverse chronological order
    entries = await redis.zrevrange(key, offset, offset + limit - 1)

    results = []
    for entry_json in entries:
        entry = json.loads(entry_json)
        if task_name and entry.get("task_name") != task_name:
            continue
        if org_id and entry.get("org_id") != org_id:
            continue
        results.append(entry)

    return results


async def requeue_from_dlq(
    redis: ArqRedis,
    env: str,
    job_id: str,
) -> bool:
    """Re-enqueue a failed task from the DLQ.

    Removes the entry from the DLQ and enqueues the task again
    using the stored payload snapshot.

    Args:
        redis: ARQ Redis connection.
        env: Environment name.
        job_id: The job ID to re-queue.

    Returns:
        True if the task was re-enqueued, False if not found.
    """
    key = dlq_key(env)

    # Find the entry
    entries = await redis.zrange(key, 0, -1)
    target_entry = None

    for entry_json in entries:
        entry = json.loads(entry_json)
        if entry["job_id"] == job_id:
            target_entry = entry
            break

    if not target_entry:
        return False

    # Remove from DLQ
    await redis.zrem(key, json.dumps(target_entry))

    # Re-enqueue with original payload
    payload = target_entry.get("payload_snapshot", {})
    task_name = target_entry["task_name"]

    await redis.enqueue_job(
        task_name,
        _queue_name=target_entry["queue_name"],
        _job_timeout=get_task_timeout(task_name),
        _max_tries=get_task_max_retries(task_name) + 1,
        **payload,
    )

    return True


async def purge_dlq(redis: ArqRedis, env: str, older_than_days: int = 7) -> int:
    """Remove DLQ entries older than the specified number of days.

    Args:
        redis: ARQ Redis connection.
        env: Environment name.
        older_than_days: Remove entries older than this many days.

    Returns:
        Number of entries purged.
    """
    key = dlq_key(env)
    cutoff = time.time() - (older_than_days * 86400)
    count = await redis.zremrangebyscore(key, 0, cutoff)
    return count


async def get_dlq_count(redis: ArqRedis, env: str) -> int:
    """Get the total number of entries in the DLQ."""
    key = dlq_key(env)
    return await redis.zcard(key)
```

### 5.5 Integration with Worker Callbacks

```python
# In worker.py — updated on_job_failed callback:

async def on_job_failed(ctx: dict, job_id: str, exc: Exception, **kwargs) -> None:
    """Log and record metrics when a job fails after exhausting retries.

    Additionally, record the failure in the dead-letter queue for inspection.
    """
    task_type = ctx.get("task_type", "unknown")
    org_id = ctx.get("org_id", "unknown")
    trace_id = ctx.get("trace_id", "unknown")
    attempt = ctx.get("job_try", 1)
    max_retries = get_task_max_retries(task_type)

    # Log the failure
    logger.error(
        "job.failed",
        trace_id=trace_id,
        org_id=org_id,
        task_type=task_type,
        job_id=job_id,
        error=str(exc),
        error_type=type(exc).__name__,
        attempt=attempt,
        max_retries=max_retries,
    )

    # Record metrics
    worker_tasks_total.labels(task_type=task_type, status="failure").inc()
    worker_tasks_per_org.labels(org_id=org_id, task_type=task_type, status="failure").inc()

    # Add to DLQ if this is the final failure (after all retries)
    if attempt >= max_retries:
        await add_to_dlq(
            redis=ctx["redis"],
            env=settings.ENV,
            job_id=job_id,
            task_name=task_type,
            queue_name=get_queue_name(ctx.get("queue_name", "high")),
            error=str(exc),
            error_type=type(exc).__name__,
            attempts=attempt,
            trace_id=trace_id,
            org_id=org_id,
            payload=ctx.get("job_kwargs", {}),
        )

        # Alert if failure rate is high (every 10th failure)
        if await get_dlq_count(ctx["redis"], settings.ENV) % 10 == 0:
            logger.error(
                "dlq.threshold_reached",
                dlq_count=await get_dlq_count(ctx["redis"], settings.ENV),
                message="DLQ has accumulated significant entries — manual inspection recommended",
            )
```

---

## 6. DLQ Administration API

### 6.1 `GET /v1/admin/workers/dlq`

List failed tasks in the DLQ.

**Request:**
```
GET /v1/admin/workers/dlq?limit=20&offset=0&task_name=extract_entities&org_id=org_abc123
Authorization: Bearer mg_live_<admin_key>
```

**Response:**
```json
{
  "data": [
    {
      "job_id": "e7a8b9c0d1f2...",
      "task_name": "extract_entities",
      "queue_name": "memgraph:prod:queue:high",
      "failed_at": "2026-06-05T10:30:00Z",
      "error": "LLM 504 Gateway Timeout after 120s",
      "error_type": "LLMTimeoutError",
      "attempts": 3,
      "trace_id": "req_01j9xmf...",
      "org_id": "org_abc123",
      "payload_snapshot": {
        "episode_id": "550e8400-e29b-41d4-a716-446655440000",
        "content_hash": "abc123def456..."
      }
    }
  ],
  "total": 42,
  "has_more": true
}
```

### 6.2 `POST /v1/admin/workers/dlq/{job_id}/retry`

Re-enqueue a failed task.

**Request:**
```
POST /v1/admin/workers/dlq/e7a8b9c0d1f2.../retry
Authorization: Bearer mg_live_<admin_key>
```

**Response:**
```json
{
  "status": "re-queued",
  "job_id": "e7a8b9c0d1f2...",
  "new_job_id": "a1b2c3d4e5f6..."
}
```

### 6.3 Router Implementation

```python
# services/api/routers/admin_workers.py

from fastapi import APIRouter, Depends, HTTPException, Query
from arq.connections import ArqRedis

from services.worker.dlq import get_dlq_entries, requeue_from_dlq, get_dlq_count
from services.api.dependencies import get_arq_redis, get_admin_user

router = APIRouter(prefix="/v1/admin/workers", tags=["admin-workers"])


@router.get("/dlq")
async def list_dlq(
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    task_name: str | None = Query(default=None),
    org_id: str | None = Query(default=None),
    redis: ArqRedis = Depends(get_arq_redis),
    admin: ... = Depends(get_admin_user),
) -> dict:
    """List failed tasks in the dead-letter queue."""
    entries = await get_dlq_entries(
        redis=redis,
        env=settings.ENV,
        limit=limit,
        offset=offset,
        task_name=task_name,
        org_id=org_id,
    )
    total = await get_dlq_count(redis=redis, env=settings.ENV)
    return {
        "data": entries,
        "total": total,
        "has_more": (offset + limit) < total,
    }


@router.post("/dlq/{job_id}/retry")
async def retry_dlq_job(
    job_id: str,
    redis: ArqRedis = Depends(get_arq_redis),
    admin: ... = Depends(get_admin_user),
) -> dict:
    """Re-enqueue a failed task from the dead-letter queue."""
    success = await requeue_from_dlq(
        redis=redis,
        env=settings.ENV,
        job_id=job_id,
    )
    if not success:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found in DLQ")
    return {"status": "re-queued", "job_id": job_id}
```

---

## 7. DLQ Auto-Purge Schedule

DLQ entries older than 7 days are automatically purged to prevent unbounded Redis memory growth.

### 7.1 On-Request Purge

Every call to `add_to_dlq` triggers an auto-purge of entries older than 7 days (see §5.4). This is sufficient for most workloads.

### 7.2 Nightly Purge Job

For additional safety, a scheduled task runs nightly:

```python
async def dlq_purge_task(ctx: dict, **kwargs: Any) -> dict:
    """Nightly DLQ purge — removes entries older than 7 days.

    Scheduled via: POST /v1/admin/workers/schedule/dlq_purge
    Kubernetes CronJob: nightly at 4am.
    """
    redis = ctx["redis"]
    purged = await purge_dlq(redis, env=settings.ENV, older_than_days=7)
    logger.info("dlq.purge_completed", entries_purged=purged)
    return {"purged": purged}
```

---

## 8. Monitoring & Alerting

### 8.1 Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `memgraph_worker_tasks_total` | Counter | `task_type`, `status` | Tasks by type and status (success/failure) |
| `memgraph_dlq_entries_total` | Gauge | `env` | Current number of entries in the DLQ |
| `memgraph_dlq_requeue_total` | Counter | `task_type` | Number of re-queue operations from DLQ |

### 8.2 Alert Rules

```yaml
# prometheus/alerts.yml

groups:
  - name: memgraph-worker
    rules:
      - alert: MemGraphHighTaskFailureRate
        expr: rate(memgraph_worker_tasks_total{status="failure"}[5m]) / rate(memgraph_worker_tasks_total[5m]) > 0.05
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Task failure rate > 5% for 5 minutes"

      - alert: MemGraphDLQGrowing
        expr: memgraph_dlq_entries_total > 100
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "DLQ has {{ $value }} entries — manual inspection recommended"

      - alert: MemGraphDLQCritical
        expr: memgraph_dlq_entries_total > 500
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "DLQ has {{ $value }} entries — immediate inspection required"
```

---

## 9. Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `{TASK_NAME}_TIMEOUT` | Per-task (see §2.1) | Task timeout in seconds |
| `{TASK_NAME}_MAX_RETRIES` | Per-task (see §2.1) | Max retry attempts |
| `DLQ_MAX_AGE_DAYS` | `7` | Auto-purge DLQ entries older than N days |
| `DLQ_ALERT_THRESHOLD` | `100` | Warning threshold for DLQ entry count |

---

## 10. SRS Traceability

| SRS ID | Requirement | How Covered |
|--------|-------------|-------------|
| WRK-03 | Exponential backoff on LLM API failures (max 3 retries) | §3: Backoff formula with jitter; §2: Per-task retry config |
| WRK-06 | Dead-letter queue for permanently failed tasks | §5: DLQ implementation with Redis sorted set; §6: Admin API |
| WRK-07 | Priority queue support | [05-priority-queues.md](05-priority-queues.md) |

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
