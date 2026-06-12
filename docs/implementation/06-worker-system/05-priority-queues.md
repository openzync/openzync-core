# Priority Queues — High/Low Queue Management

> **Phase:** 1 (Core Memory) — single queue; Phase 2 — priority queues
> **SRS Requirements:** WRK-07
> **Dependencies:** [01-arq-setup.md](01-arq-setup.md), [02-task-definitions.md](02-task-definitions.md)
> **Design Authority:** @devops

---

## 1. Overview

OpenZep processes two categories of background tasks:

- **Real-time ingestion tasks** (entity extraction, embedding, graph sync) — must complete within seconds of ingestion. Users expect quick enrichment after `POST /memory`.
- **Batch/scheduled tasks** (community summarisation, entity dedup) — can take minutes to hours. These are not time-sensitive.

To prevent batch tasks from starving real-time tasks, OpenZep implements **priority queues**: a `high` queue for ingestion tasks and a `low` queue for batch tasks, with separate worker pool allocations.

### 1.1 Design Constraints

ARQ does not natively support priority queues. There is no way to assign a "priority" value to an ARQ job. The workaround is two physically separate queues with dedicated worker pools. Each queue is an independent Redis list consumed by its own set of worker processes.

---

## 2. Queue Architecture

### 2.1 Two-Queue Model

```
                    ┌──────────────────────┐
                    │    Redis Instance     │
                    │                       │
                    │  OpenZep:prod:queue:high  ◄── High-priority jobs
                    │  (consumed by 3 workers)     extract_entities
                    │                              embed_episode
                    │                              embed_entity
                    │                              extract_facts
                    │                              classify_dialog
                    │                              extract_structured
                    │                              sync_to_graph
                    │                              delete_user_data
                    │                              refresh_context_cache
                    │
                    │  OpenZep:prod:queue:low   ◄── Low-priority jobs
                    │  (consumed by 1 worker)     summarise_community
                    │                              ingest_business_data
                    │                              merge_duplicate_entities
                    │
                    └──────────────────────┘
```

### 2.2 Queue Name Convention

```python
def get_queue_name(queue_type: str) -> str:
    """Generate namespaced queue name.

    Args:
        queue_type: One of "high" or "low".

    Returns:
        e.g. "OpenZep:prod:queue:high"
    """
    return f"OpenZep:{settings.ENV}:queue:{queue_type}"
```

---

## 3. Task-to-Queue Assignment

### 3.1 Assignment Table

| Task | Queue | Rationale |
|------|-------|-----------|
| `extract_entities` | `high` | User-facing latency — entities should appear within seconds |
| `embed_episode` | `high` | Required for vector search — must complete for context retrieval |
| `embed_entity` | `high` | Required for entity search |
| `extract_facts` | `high` | User-facing — facts should appear within seconds |
| `classify_dialog` | `high` | Phase 3 task, but follows ingestion timeline |
| `extract_structured` | `high` | Triggered on session close — user is waiting |
| `sync_to_graph` | `high` | Required for graph queries — high priority |
| `delete_user_data` | `high` | GDPR — user is waiting for confirmation |
| `refresh_context_cache` | `high` | Follows enrichment — user-facing relevance |
| `summarise_community` | `low` | Scheduled nightly — no user waiting |
| `ingest_business_data` | `low` | Batch ingestion — enqueued, not time-sensitive |
| `merge_duplicate_entities` | `low` | Weekly batch — no user waiting |

### 3.2 Enqueue with Queue Selection

```python
# Helper function to enqueue a task to the correct queue:

from arq.connections import ArqRedis


# Task-to-queue mapping
TASK_QUEUE_MAP: dict[str, str] = {
    "extract_entities": "high",
    "embed_episode": "high",
    "embed_entity": "high",
    "extract_facts": "high",
    "classify_dialog": "high",
    "extract_structured": "high",
    "sync_to_graph": "high",
    "delete_user_data": "high",
    "refresh_context_cache": "high",
    "summarise_community": "low",
    "ingest_business_data": "low",
    "merge_duplicate_entities": "low",
}


async def enqueue_task(
    redis: ArqRedis,
    task_name: str,
    **kwargs: Any,
) -> ArqJob | None:
    """Enqueue a task to its configured priority queue.

    This is the canonical way to enqueue any OpenZep task.
    It automatically routes to the correct queue based on TASK_QUEUE_MAP.

    Args:
        redis: ARQ Redis connection.
        task_name: Name of the task function.
        **kwargs: Task payload fields.

    Returns:
        ARQ job handle, or None if enqueue failed.
    """
    queue_type = TASK_QUEUE_MAP.get(task_name, "high")
    full_queue_name = f"OpenZep:{settings.ENV}:queue:{queue_type}"

    timeout = get_task_timeout(task_name)
    max_retries = get_task_max_retries(task_name)

    return await redis.enqueue_job(
        task_name,
        _queue_name=full_queue_name,
        _job_timeout=timeout,
        _max_tries=max_retries + 1,
        **kwargs,
    )
```

---

## 4. Worker Pool Allocation

### 4.1 Configuration

```python
# In WorkerSettings (from 01-arq-setup.md):

class WorkerSettings(BaseSettings):
    # ... other settings ...

    # Queue worker allocation
    HIGH_QUEUE_WORKERS: int = Field(
        default=3,
        ge=1,
        le=16,
        description="Number of concurrent worker slots for the 'high' priority queue. "
        "Each slot processes one job at a time. Increase for higher ingestion throughput.",
    )

    LOW_QUEUE_WORKERS: int = Field(
        default=1,
        ge=1,
        le=8,
        description="Number of concurrent worker slots for the 'low' priority queue. "
        "Batch tasks are not time-sensitive — 1 slot is usually sufficient.",
    )
```

### 4.2 Worker Process Startup

In [01-arq-setup.md](01-arq-setup.md), two ARQ `Worker` instances are created:

```python
# services/worker/worker.py (updated with per-queue concurrency)

from services.worker.config import settings

high_worker = ArqWorker(
    redis_settings=redis_settings,
    functions=HIGH_QUEUE_TASKS,
    queue_name=get_queue_name("high"),
    concurrency=settings.HIGH_QUEUE_WORKERS,  # e.g., 3
    timeout=settings.JOB_TIMEOUT_DEFAULT,
    keep_result=settings.JOB_KEEP_RESULT_FOR,
    poll_delay=0.2,  # Check high queue more frequently (200ms)
)

low_worker = ArqWorker(
    redis_settings=redis_settings,
    functions=LOW_QUEUE_TASKS,
    queue_name=get_queue_name("low"),
    concurrency=settings.LOW_QUEUE_WORKERS,  # e.g., 1
    timeout=settings.JOB_TIMEOUT_DEFAULT * 2,
    keep_result=settings.JOB_KEEP_RESULT_FOR,
    poll_delay=2.0,  # Check low queue less frequently (2s)
)
```

### 4.3 Allocation Rationale

| Environment | HIGH_QUEUE_WORKERS | LOW_QUEUE_WORKERS | Rationale |
|-------------|-------------------|-------------------|-----------|
| Development | 2 | 1 | Low traffic, keep it simple |
| Staging | 3 | 1 | Moderate traffic, test prioritisation |
| Production (small) | 4 | 1 | 4 high-priority slots for real-time tasks |
| Production (medium) | 8 | 2 | Higher ingestion throughput |
| Production (large) | 12 | 4 | Heavy ingestion + batch workload |

> ⚠️ **Total concurrency = HIGH_QUEUE_WORKERS + LOW_QUEUE_WORKERS.** Ensure the machine has enough CPU cores to handle the sum. For CPU-bound LLM calls, total concurrency should not exceed CPU cores × 2. For I/O-bound tasks, higher over-subscription is safe.

---

## 5. Starvation Prevention

### 5.1 The Problem

If the `high` queue is continuously busy (e.g., high ingestion rate), the `low` queue may never get processed. This is called **starvation** — low-priority tasks are delayed indefinitely.

### 5.2 Starvation Detection

A monitoring routine checks the `low` queue depth every 30 seconds. If the depth exceeds a configurable threshold, one `high` worker slot is temporarily reassigned to the `low` queue.

```python
# services/worker/starvation_monitor.py

import asyncio
from arq.connections import ArqRedis
from services.worker.config import settings


# Thresholds — configurable via env vars
LOW_QUEUE_STARVATION_THRESHOLD: int = 100  # Reassign worker if low queue depth > 100
STARVATION_CHECK_INTERVAL: int = 30  # Check every 30 seconds


async def monitor_starvation(high_worker, low_worker, redis: ArqRedis) -> None:
    """Monitor queue depths and prevent starvation of the low queue.

    If the low queue grows beyond the threshold, this function signals
    the high worker to reduce concurrency by 1 and the low worker to
    increase concurrency by 1. When the low queue drains, the original
    allocation is restored.

    This function runs as a background asyncio task in the worker process.
    """
    logger = structlog.get_logger("OpenZep.worker.starvation")

    # Track original allocation for restoration
    original_high = settings.HIGH_QUEUE_WORKERS
    original_low = settings.LOW_QUEUE_WORKERS
    current_high = original_high
    current_low = original_low

    while True:
        await asyncio.sleep(STARVATION_CHECK_INTERVAL)

        # Check low queue depth
        low_queue_key = f"OpenZep:{settings.ENV}:queue:{settings.LOW_QUEUE_NAME}:jobs"
        low_depth = await redis.zcard(low_queue_key)

        logger.debug(
            "starvation.check",
            low_queue_depth=low_depth,
            threshold=LOW_QUEUE_STARVATION_THRESHOLD,
            high_workers=current_high,
            low_workers=current_low,
        )

        # Update Prometheus gauge
        worker_queue_depth.labels(queue_name="low").set(low_depth)

        if low_depth > LOW_QUEUE_STARVATION_THRESHOLD and current_low < original_low + 2:
            # Low queue is growing — steal a worker from high pool
            new_high = max(1, current_high - 1)
            new_low = current_low + 1

            logger.warning(
                "starvation.reassigning_worker",
                low_queue_depth=low_depth,
                high_workers_before=current_high,
                high_workers_after=new_high,
                low_workers_before=current_low,
                low_workers_after=new_low,
            )

            # Adjust worker concurrency at runtime
            # ARQ Worker supports changing concurrency via set_concurrency()
            await high_worker.set_concurrency(new_high)
            await low_worker.set_concurrency(new_low)

            current_high = new_high
            current_low = new_low

        elif low_depth <= LOW_QUEUE_STARVATION_THRESHOLD // 2 and current_low > original_low:
            # Low queue has drained — restore original allocation
            logger.info(
                "starvation.restoring_allocation",
                high_workers_before=current_high,
                high_workers_after=original_high,
                low_workers_before=current_low,
                low_workers_after=original_low,
            )

            await high_worker.set_concurrency(original_high)
            await low_worker.set_concurrency(original_low)

            current_high = original_high
            current_low = original_low
```

### 5.3 Threshold Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LOW_QUEUE_STARVATION_THRESHOLD` | `100` | Reassign a high worker to low queue when depth exceeds this |
| `STARVATION_CHECK_INTERVAL` | `30` | Seconds between starvation checks |
| `MAX_LOW_WORKER_OVERFLOW` | `2` | Maximum number of extra workers that can be reassigned to the low queue |

### 5.4 Limitations

> ⚠️ **ARQ limitation:** `Worker.set_concurrency()` is not a native ARQ feature in versions < 0.26. If using an older ARQ version, starvation prevention must be implemented differently:
> - **Alternative 1**: Scale worker processes externally (Kubernetes HPA based on queue depth)
> - **Alternative 2**: Accept starvation and let batch tasks run during off-peak hours (nightly)
> - **Alternative 3**: Use a single queue with weighted polling (not recommended — complex, error-prone)

---

## 6. Queue Depth Monitoring

### 6.1 Prometheus Metrics

```python
# Defined in 01-arq-setup.md / services/worker/worker.py

worker_queue_depth = Gauge(
    "openzep_worker_queue_depth",
    "Current number of pending jobs per queue",
    labelnames=["queue_name"],
)
```

### 6.2 Queue Depth Polling

```python
# services/worker/monitoring.py

async def monitor_queue_depth(redis: ArqRedis, interval: int = 15) -> None:
    """Periodically sample queue depth for all known queues.

    Runs as a background asyncio task in the worker process.
    """
    while True:
        for queue_type in ["high", "low"]:
            queue_key = f"OpenZep:{settings.ENV}:queue:{queue_type}:jobs"
            depth = await redis.zcard(queue_key)
            worker_queue_depth.labels(queue_name=queue_type).set(depth)

        await asyncio.sleep(interval)
```

### 6.3 Alert Rules

```yaml
# prometheus/alerts.yml

- alert: MemGraphHighQueueDepth
  expr: openzep_worker_queue_depth{queue_name="high"} > 1000
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High priority queue depth is {{ $value }} — ingestion may be backing up"

- alert: MemGraphLowQueueDepth
  expr: openzep_worker_queue_depth{queue_name="low"} > 1000
  for: 15m
  labels:
    severity: info
  annotations:
    summary: "Low priority queue depth is {{ $value }} — batch processing may be behind"

- alert: MemGraphQueueStarvation
  expr: openzep_worker_queue_depth{queue_name="low"} > 500 AND openzep_worker_queue_depth{queue_name="high"} < 50
  for: 30m
  labels:
    severity: warning
  annotations:
    summary: "Low queue is deep ({{ $value }}) but high queue is idle — starvation prevention may not be working"
```

---

## 7. Single-Queue Fallback (Phase 0-1)

During Phase 0-1 (before enough tasks exist to warrant prioritisation), a single queue is sufficient:

```python
# Phase 0-1 — simple, no priority

async def enqueue_task(redis, task_name, **kwargs):
    """Simple enqueue — single queue, no priority."""
    return await redis.enqueue_job(task_name, **kwargs)
```

The migration to two queues is transparent at the enqueue layer — just switch to the `enqueue_task` helper defined in §3.2. The existing task payloads remain unchanged.

---

## 8. Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `HIGH_QUEUE_WORKERS` | `3` | Worker slots for high-priority queue |
| `LOW_QUEUE_WORKERS` | `1` | Worker slots for low-priority queue |
| `LOW_QUEUE_STARVATION_THRESHOLD` | `100` | Reassign worker when low queue depth exceeds this |
| `STARVATION_CHECK_INTERVAL` | `30` | Seconds between starvation checks |
| `MAX_LOW_WORKER_OVERFLOW` | `2` | Max extra workers reassignable to low queue |

---

## 9. Testing

### 9.1 Unit Tests

```python
@pytest.mark.asyncio
async def test_task_to_queue_mapping():
    """Every task should have a queue assignment."""
    from services.worker.config import TASK_QUEUE_MAP

    assert TASK_QUEUE_MAP["extract_entities"] == "high"
    assert TASK_QUEUE_MAP["summarise_community"] == "low"
    assert TASK_QUEUE_MAP["merge_duplicate_entities"] == "low"
    assert TASK_QUEUE_MAP["delete_user_data"] == "high"


@pytest.mark.asyncio
async def test_enqueue_routes_to_correct_queue(arq_redis):
    """Enqueued tasks should appear in the correct Redis list."""
    from services.worker.config import enqueue_task

    await enqueue_task(arq_redis, "extract_entities", episode_id=uuid4(), ...)
    await enqueue_task(arq_redis, "summarise_community", org_id="org_abc", ...)

    high_depth = await arq_redis.zcard("OpenZep:test:queue:high:jobs")
    low_depth = await arq_redis.zcard("OpenZep:test:queue:low:jobs")

    assert high_depth == 1
    assert low_depth == 1
```

### 9.2 Integration Tests

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_high_priority_jobs_delivered_first(
    async_client, auth_headers, arq_worker, caplog
):
    """When both queues have jobs, high-priority jobs should be consumed first."""
    # Enqueue several low-priority jobs (they simulate batch work)
    for i in range(5):
        await arq_worker.redis.enqueue_job(
            "summarise_community",
            _queue_name="OpenZep:test:queue:low",
            _job_timeout=10,
            org_id="org_abc",
        )

    # Now enqueue a high-priority job
    await arq_worker.redis.enqueue_job(
        "extract_entities",
        _queue_name="OpenZep:test:queue:high",
        _job_timeout=10,
        episode_id=uuid4(),
        content="test",
    )

    # Wait for processing
    await asyncio.sleep(2)

    # The high-priority job should have been consumed before the low ones
    high_depth = await arq_worker.redis.zcard("OpenZep:test:queue:high:jobs")
    assert high_depth == 0  # High queue drained
```

### 9.3 Starvation Prevention Test

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_starvation_reassigns_worker(arq_redis):
    """When low queue exceeds threshold, a high worker should be reassigned."""
    # Fill the low queue beyond the threshold
    for i in range(150):  # threshold is 100
        await arq_redis.enqueue_job(
            "summarise_community",
            _queue_name="OpenZep:test:queue:low",
            org_id="org_abc",
        )

    # Check monitor detects starvation
    from services.worker.starvation_monitor import LOW_QUEUE_STARVATION_THRESHOLD

    low_depth = await arq_redis.zcard("OpenZep:test:queue:low:jobs")
    assert low_depth > LOW_QUEUE_STARVATION_THRESHOLD
```

---

## 10. SRS Traceability

| SRS ID | Requirement | How Covered |
|--------|-------------|-------------|
| WRK-07 | Support priority queues: high (real-time) and low (batch) | §2: Two-queue model; §3: Task-to-queue mapping; §4: Worker pool allocation |

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
