# Scheduled Tasks — Cron, Batch & Maintenance

> **Phase:** 2 (Full Feature Parity) for nightly/weekly tasks; Phase 4+ for advanced scheduling
> **SRS Requirements:** NLP-15–NLP-17 (community summarisation schedule), WRK-07 (low-priority batch), SEC-04 (data retention)
> **Dependencies:** [01-arq-setup.md](01-arq-setup.md), [02-task-definitions.md](02-task-definitions.md), [05-priority-queues.md](05-priority-queues.md), [07-user-session-mgmt/03-gdpr-compliance.md](../07-user-session-mgmt/03-gdpr-compliance.md)
> **Design Authority:** @devops (Kubernetes CronJob), @architect (APScheduler for self-hosted)

---

## 1. Overview

Several MemGraph tasks are not triggered by API requests but run on a schedule:

- **Nightly**: Community re-summarisation, data retention cleanup, DLQ purge
- **Weekly**: Entity deduplication
- **On-demand (admin)**: Cache warming, specific community re-summarisation

### 1.1 ARQ Limitation

> ⚠️ ARQ does **not** have a built-in cron/scheduler. Jobs are only enqueued by explicit calls to `enqueue_job`. Scheduled execution must be handled externally.

This document presents two options for scheduled task execution:

| Option | Mechanism | Best For |
|--------|-----------|----------|
| **Option A** (recommended Phase 0-3) | Kubernetes CronJob → HTTP call to admin API | K8s deployments, simplicity, separation of concerns |
| **Option B** (recommended Phase 4+) | APScheduler running in the worker process | Self-hosted (Docker Compose), no K8s dependency |

---

## 2. Scheduled Tasks — Master List

| Task | Schedule | Queue | Priority | SRS |
|------|----------|-------|----------|-----|
| `summarise_community` | Nightly at 02:00 UTC | `low` | P1 | NLP-15–NLP-17 |
| `merge_duplicate_entities` | Weekly on Sunday at 03:00 UTC | `low` | P2 | KG-06 |
| `data_retention_cleanup` | Nightly at 04:00 UTC | `low` | P1 | SEC-04 |
| `cache_warming` | (P2) Every 15 min for active users | `low` | P2 | CTX-06 |
| `refresh_context_cache` | On community regeneration | `high` | P1 | CTX-06 |
| `dlq_purge` | Nightly at 04:30 UTC | `low` | P1 | WRK-06 |

### 2.1 Task Specifications

#### `summarise_community`

| Field | Specification |
|-------|---------------|
| **Task name** | `summarise_community` |
| **Trigger** | Nightly at 02:00 UTC |
| **Queue** | `low` |
| **Scope** | All organisations with > 100 entity nodes in the graph |
| **Behaviour** | (1) Run Louvain/Label Propagation community detection. (2) For each community, generate LLM summary. (3) Upsert CommunityNode in Graphiti. (4) Invalidate affected context cache entries. |
| **Cost control** | Skip orgs with < 100 entities. Batch LLM calls (5 communities per call) for orgs with > 500 communities. |
| **Timeout per org** | 600s (10 min) |
| **Idempotency** | Community summaries are versioned — re-running regenerates with a new timestamp. No dedup needed. |

```python
class SummariseCommunitySchedulePayload(BaseModel):
    """Payload for scheduled community summarisation.

    Unlike the on-demand version, the scheduled version processes ALL
    eligible orgs in sequence (one org per job invocation).
    """

    # When triggered by schedule, process all eligible orgs.
    # The task determines eligibility (entity count > 100) internally.
    org_id: str | None = None  # If None, process all eligible orgs
    trace_id: str = "scheduled:summarise_community"

    class Config:
        frozen = True
```

#### `merge_duplicate_entities`

| Field | Specification |
|-------|---------------|
| **Task name** | `merge_duplicate_entities` |
| **Trigger** | Weekly on Sunday at 03:00 UTC |
| **Queue** | `low` |
| **Scope** | All organisations (with at least one entity) |
| **Behaviour** | (1) Fetch all entities per org. (2) Group by fuzzy name similarity. (3) Merge duplicates into canonical entity. (4) Update all relationship references. (5) Log merges to entity_merges audit table. |
| **Timeout per org** | 600s (10 min) |
| **Idempotency** | Each run has a unique `run_id`. Merges are logged — re-running is safe but wasteful. |

#### `data_retention_cleanup`

| Field | Specification |
|-------|---------------|
| **Task name** | `data_retention_cleanup` |
| **Trigger** | Nightly at 04:00 UTC |
| **Queue** | `low` |
| **Scope** | Soft-deleted records across all tables |
| **Behaviour** | Hard-deletes records where `is_deleted = True AND updated_at < NOW() - INTERVAL '30 days'`. Covers: users, sessions, episodes, facts. |
| **Timeout** | 300s (5 min) |
| **Idempotency** | `DELETE ... WHERE ...` is inherently idempotent — no duplicate side effects. |

```sql
-- The query executed by this task:
DELETE FROM episodes      WHERE is_deleted = TRUE AND updated_at < NOW() - INTERVAL '30 days';
DELETE FROM facts         WHERE is_deleted = TRUE AND updated_at < NOW() - INTERVAL '30 days';
DELETE FROM sessions      WHERE is_deleted = TRUE AND updated_at < NOW() - INTERVAL '30 days';
DELETE FROM users         WHERE is_deleted = TRUE AND updated_at < NOW() - INTERVAL '30 days';
-- Note: organizations are NEVER hard-deleted (financial records).
-- Note: api_keys are NEVER hard-deleted (audit trail).
```

#### `cache_warming` (P2)

| Field | Specification |
|-------|---------------|
| **Task name** | `cache_warming` |
| **Trigger** | Every 15 minutes |
| **Queue** | `low` |
| **Scope** | Active users (those with ingestion in the last hour) |
| **Behaviour** | Pre-warm context cache by querying `GET /context` for common queries. Store results in Redis with 30s TTL. |
| **Timeout per user** | 10s |
| **Priority** | P2 — defer if not all P1 tasks are stable. |

#### `dlq_purge`

| Field | Specification |
|-------|---------------|
| **Task name** | `dlq_purge` |
| **Trigger** | Nightly at 04:30 UTC |
| **Queue** | `low` |
| **Behaviour** | Removes DLQ entries older than 7 days from the Redis sorted set. |
| **Timeout** | 60s |
| **Idempotency** | `ZREMRANGEBYSCORE` with timestamp cutoff is fully idempotent. |

---

## 3. Option A: Kubernetes CronJob (Recommended Phase 0-3)

### 3.1 Architecture

```
Kubernetes Cluster
│
├── CronJob (nightly 2am)
│   └── Job Pod
│       └── curl POST /v1/admin/workers/schedule/summarise_community
│
├── CronJob (weekly sunday 3am)
│   └── Job Pod
│       └── curl POST /v1/admin/workers/schedule/merge_duplicate_entities
│
├── CronJob (nightly 4am)
│   └── Job Pod
│       └── curl POST /v1/admin/workers/schedule/data_retention_cleanup
│
├── CronJob (nightly 4:30am)
│   └── Job Pod
│       └── curl POST /v1/admin/workers/schedule/dlq_purge
│
└── CronJob (every 15 min) [P2]
    └── Job Pod
        └── curl POST /v1/admin/workers/schedule/cache_warming
```

### 3.2 Admin API Endpoint

A generic schedule endpoint accepts any task name and enqueues it:

```python
# services/api/routers/admin_workers.py

from fastapi import APIRouter, Depends, HTTPException
from arq.connections import ArqRedis

from services.api.dependencies import get_arq_redis, get_admin_user
from services.worker.config import enqueue_task

router = APIRouter(prefix="/v1/admin/workers", tags=["admin-workers"])


@router.post("/schedule/{task_name}")
async def trigger_scheduled_task(
    task_name: str,
    redis: ArqRedis = Depends(get_arq_redis),
    admin: ... = Depends(get_admin_user),
) -> dict:
    """Trigger a scheduled task by name.

    This endpoint is called by Kubernetes CronJobs (Option A)
    or can be triggered manually by admins.

    Args:
        task_name: The name of the task to trigger.
            Valid values: summarise_community, merge_duplicate_entities,
            data_retention_cleanup, cache_warming, dlq_purge.

    Returns:
        Status and job ID of the enqueued task.

    Raises:
        404: If the task name is not a valid scheduled task.
    """
    VALID_SCHEDULED_TASKS = {
        "summarise_community",
        "merge_duplicate_entities",
        "data_retention_cleanup",
        "cache_warming",
        "dlq_purge",
    }

    if task_name not in VALID_SCHEDULED_TASKS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown scheduled task '{task_name}'. "
            f"Valid tasks: {', '.join(sorted(VALID_SCHEDULED_TASKS))}",
        )

    job = await enqueue_task(
        redis=redis,
        task_name=task_name,
        trace_id=f"scheduled:{task_name}",
    )

    if job is None:
        raise HTTPException(status_code=500, detail="Failed to enqueue task")

    return {
        "status": "enqueued",
        "task_name": task_name,
        "job_id": job.job_id,
    }
```

### 3.3 Kubernetes CronJob Definition

```yaml
# infra/helm/templates/cronjobs.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: memgraph-summarise-community
  namespace: memgraph
spec:
  schedule: "0 2 * * *"        # Nightly at 02:00 UTC
  concurrencyPolicy: Forbid     # Don't start a new run if previous is still running
  startingDeadlineSeconds: 600  # Give up if not started within 10 min
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: memgraph-scheduler
          restartPolicy: Never
          containers:
            - name: trigger
              image: curlimages/curl:latest
              command:
                - /bin/sh
                - -c
                - |
                  curl -s -X POST \
                    -H "Authorization: Bearer $(MEMGRAPH_ADMIN_KEY)" \
                    http://memgraph-api:8000/v1/admin/workers/schedule/summarise_community
              env:
                - name: MEMGRAPH_ADMIN_KEY
                  valueFrom:
                    secretKeyRef:
                      name: memgraph-secrets
                      key: admin-api-key
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: memgraph-merge-duplicate-entities
  namespace: memgraph
spec:
  schedule: "0 3 * * 0"        # Weekly on Sunday at 03:00 UTC
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 600
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: memgraph-scheduler
          restartPolicy: Never
          containers:
            - name: trigger
              image: curlimages/curl:latest
              command:
                - /bin/sh
                - -c
                - |
                  curl -s -X POST \
                    -H "Authorization: Bearer $(MEMGRAPH_ADMIN_KEY)" \
                    http://memgraph-api:8000/v1/admin/workers/schedule/merge_duplicate_entities
              env:
                - name: MEMGRAPH_ADMIN_KEY
                  valueFrom:
                    secretKeyRef:
                      name: memgraph-secrets
                      key: admin-api-key
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: memgraph-data-retention-cleanup
  namespace: memgraph
spec:
  schedule: "0 4 * * *"        # Nightly at 04:00 UTC
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 600
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: memgraph-scheduler
          restartPolicy: Never
          containers:
            - name: trigger
              image: curlimages/curl:latest
              command:
                - /bin/sh
                - -c
                - |
                  curl -s -X POST \
                    -H "Authorization: Bearer $(MEMGRAPH_ADMIN_KEY)" \
                    http://memgraph-api:8000/v1/admin/workers/schedule/data_retention_cleanup
              env:
                - name: MEMGRAPH_ADMIN_KEY
                  valueFrom:
                    secretKeyRef:
                      name: memgraph-secrets
                      key: admin-api-key
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: memgraph-dlq-purge
  namespace: memgraph
spec:
  schedule: "30 4 * * *"       # Nightly at 04:30 UTC
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: memgraph-scheduler
          restartPolicy: Never
          containers:
            - name: trigger
              image: curlimages/curl:latest
              command:
                - /bin/sh
                - -c
                - |
                  curl -s -X POST \
                    -H "Authorization: Bearer $(MEMGRAPH_ADMIN_KEY)" \
                    http://memgraph-api:8000/v1/admin/workers/schedule/dlq_purge
              env:
                - name: MEMGRAPH_ADMIN_KEY
                  valueFrom:
                    secretKeyRef:
                      name: memgraph-secrets
                      key: admin-api-key
```

### 3.4 Advantages of Option A

| Pro | Explanation |
|-----|-------------|
| **No in-process scheduler** | Worker process stays simple — no extra dependencies or scheduling state to manage. |
| **K8s-native** | CronJobs are standard K8s primitives with built-in monitoring (failed/successful job tracking). |
| **Separation of concerns** | The worker only executes tasks. The CronJob decides *when* to trigger them. |
| **Easy to test manually** | Trigger any task via `curl` to the admin API without waiting for the schedule. |
| **No missed schedules on crash** | If the worker is down at 2am, the CronJob still runs. The request fails, and the CronJob retries (configurable backoff). |

### 3.5 Disadvantages of Option A

| Con | Explanation |
|-----|-------------|
| **Requires Kubernetes** | Does not work with Docker Compose (self-hosted) deployments without adding a cron container. |
| **Extra pod per schedule** | Each CronJob creates a short-lived pod that consumes minimal resources but adds complexity. |
| **Network latency** | Each trigger is an HTTP call from the CronJob pod to the API pod — adds a small overhead. |

---

## 4. Option B: APScheduler in Worker (Recommended Phase 4+)

### 4.1 Architecture

For self-hosted deployments (Docker Compose) or deployments that want to avoid K8s CronJob complexity, APScheduler runs inside the worker process:

```
Worker Process
│
├── ARQ Worker (high queue)
├── ARQ Worker (low queue)
├── Prometheus HTTP Server (port 9090)
├── Health Check Server (port 8081)
└── APScheduler (background thread)
    ├── summarise_community     → 0 2 * * *
    ├── merge_duplicate_entities → 0 3 * * 0
    ├── data_retention_cleanup   → 0 4 * * *
    └── dlq_purge               → 30 4 * * *
```

### 4.2 Implementation

```python
# services/worker/scheduler.py

import asyncio
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.triggers.cron import CronTrigger
from arq.connections import ArqRedis

from services.worker.config import settings, enqueue_task
from services.worker.dlq import purge_dlq


logger = structlog.get_logger("memgraph.worker.scheduler")


class ScheduledTaskScheduler:
    """APScheduler integration for running scheduled tasks within the worker.

    Runs on its own asyncio event loop (or shares the worker's event loop).
    When a schedule fires, it enqueues the corresponding ARQ task.

    This is an alternative to Kubernetes CronJobs for self-hosted deployments.
    """

    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            timezone="UTC",
        )

    def start(self) -> None:
        """Register all schedules and start the scheduler."""

        # ── Community summarisation — nightly at 02:00 UTC ──
        self._scheduler.add_job(
            self._trigger_task,
            trigger=CronTrigger(hour=2, minute=0),
            id="summarise_community",
            name="Community summarisation",
            args=["summarise_community"],
            misfire_grace_time=600,  # If worker was down, catch up within 10 min
            coalesce=True,           # If multiple misfires, only run once
        )

        # ── Entity dedup — weekly on Sunday at 03:00 UTC ──
        self._scheduler.add_job(
            self._trigger_task,
            trigger=CronTrigger(day_of_week="sun", hour=3, minute=0),
            id="merge_duplicate_entities",
            name="Entity deduplication",
            args=["merge_duplicate_entities"],
            misfire_grace_time=3600,
            coalesce=True,
        )

        # ── Data retention cleanup — nightly at 04:00 UTC ──
        self._scheduler.add_job(
            self._trigger_task,
            trigger=CronTrigger(hour=4, minute=0),
            id="data_retention_cleanup",
            name="Data retention cleanup",
            args=["data_retention_cleanup"],
            misfire_grace_time=600,
            coalesce=True,
        )

        # ── DLQ purge — nightly at 04:30 UTC ──
        self._scheduler.add_job(
            self._trigger_task,
            trigger=CronTrigger(hour=4, minute=30),
            id="dlq_purge",
            name="DLQ purge",
            args=["dlq_purge"],
            misfire_grace_time=600,
            coalesce=True,
        )

        # ── Cache warming (P2) — every 15 minutes ──
        if settings.CACHE_WARMING_ENABLED:
            self._scheduler.add_job(
                self._trigger_task,
                trigger=CronTrigger(minute="*/15"),
                id="cache_warming",
                name="Cache warming",
                args=["cache_warming"],
                misfire_grace_time=300,
                coalesce=True,
            )

        self._scheduler.start()
        logger.info("scheduler.started", scheduled_jobs=self._scheduler.get_jobs())

    def stop(self) -> None:
        """Gracefully stop the scheduler."""
        self._scheduler.shutdown(wait=True)
        logger.info("scheduler.stopped")

    async def _trigger_task(self, task_name: str) -> None:
        """Enqueue the scheduled task via ARQ.

        Called by APScheduler when a schedule fires.
        """
        logger.info("scheduler.triggering", task_name=task_name)
        try:
            job = await enqueue_task(
                redis=self._redis,
                task_name=task_name,
                trace_id=f"scheduled:{task_name}",
            )
            if job:
                logger.info("scheduler.enqueued", task_name=task_name, job_id=job.job_id)
            else:
                logger.error("scheduler.enqueue_failed", task_name=task_name)
        except Exception as exc:
            logger.error("scheduler.trigger_failed", task_name=task_name, error=str(exc))
```

### 4.3 Integration with Worker Startup

```python
# In services/worker/worker.py — updated main():

async def main() -> NoReturn:
    setup_logging()
    start_prometheus_server(settings.PROMETHEUS_PORT)

    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)

    # ═══════════════════════════════════════════════════════
    # Start APScheduler (only if SCHEDULED_TASKS_ENABLED)
    # ═══════════════════════════════════════════════════════
    scheduler: ScheduledTaskScheduler | None = None
    if settings.SCHEDULED_TASKS_ENABLED:
        # Create a temporary Redis connection for the scheduler
        scheduler_redis = await create_pool(redis_settings)
        scheduler = ScheduledTaskScheduler(redis=scheduler_redis)
        scheduler.start()
        logger.info("scheduler.initialized")

    # ... create high_worker, low_worker ...

    try:
        await asyncio.gather(
            high_worker.run(),
            low_worker.run(),
        )
    except asyncio.CancelledError:
        logger.info("worker.run_cancelled")
        raise
    finally:
        if scheduler:
            scheduler.stop()
        logger.info("worker.stopped")
```

### 4.4 Advantages of Option B

| Pro | Explanation |
|-----|-------------|
| **No Kubernetes dependency** | Works with Docker Compose, systemd, or any deployment model. |
| **No extra pods** | Scheduler lives in the same process — simpler infrastructure. |
| **Low latency** | Triggers are in-process function calls, not HTTP requests. |
| **Coalescing** | APScheduler coalesces missed runs — if the worker was down at 2am, the community summarisation runs once when it comes back up. |

### 4.5 Disadvantages of Option B

| Con | Explanation |
|-----|-------------|
| **In-process complexity** | Scheduler state lives in the worker process. If the worker crashes, scheduled times are lost (until the next startup). APScheduler's `coalesce=True` mitigates this. |
| **No built-in monitoring** | Unlike K8s CronJobs (which show failed/ successful runs), APScheduler failures are only visible in logs. |
| **Memory job store** | Schedules are not persisted. A worker restart resets the in-memory schedule. This is fine — schedules are static and re-registered on startup. |

---

## 5. Configuration

### 5.1 Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHEDULED_TASKS_ENABLED` | `True` | Enable APScheduler for scheduled tasks. Set to `False` when using K8s CronJobs. |
| `SCHEDULE_SUMMARISE_COMMUNITY` | `"0 2 * * *"` | Cron expression for community summarisation |
| `SCHEDULE_MERGE_DUPLICATE_ENTITIES` | `"0 3 * * 0"` | Cron expression for entity dedup |
| `SCHEDULE_DATA_RETENTION` | `"0 4 * * *"` | Cron expression for data retention cleanup |
| `SCHEDULE_DLQ_PURGE` | `"30 4 * * *"` | Cron expression for DLQ purge |
| `SCHEDULE_CACHE_WARMING` | `"*/15 * * * *"` | Cron expression for cache warming (P2) |
| `CACHE_WARMING_ENABLED` | `False` | Enable cache warming (P2 — off by default) |
| `COMMUNITY_ENTITY_THRESHOLD` | `100` | Minimum entities for community summarisation |
| `DATA_RETENTION_DAYS` | `30` | Hard-delete soft-deleted records older than N days |
| `SCHEDULE_TIMEZONE` | `"UTC"` | Timezone for all schedule expressions |

### 5.2 Feature Flag

```python
# In WorkerSettings:
SCHEDULED_TASKS_ENABLED: bool = Field(
    default=True,
    description="Enable APScheduler within the worker process. "
    "Set to False when using Kubernetes CronJobs (Option A). "
    "Has no effect if SCHEDULED_TASKS_ENABLED is False - "
    "schedules are managed externally via K8s CronJobs.",
)
```

---

## 6. Docker Compose Self-Hosted Setup

For Docker Compose deployments (no Kubernetes), add a cron container that triggers the admin API:

```yaml
# docker-compose.yml (self-hosted variant)

services:
  # ... api, worker, postgres, redis, falkordb ...

  cron-trigger:
    image: alpine:3.19
    restart: unless-stopped
    depends_on:
      api:
        condition: service_healthy
    environment:
      - MEMGRAPH_ADMIN_KEY=${MEMGRAPH_ADMIN_KEY}
      - API_URL=http://api:8000
    volumes:
      - ./scripts/crontab:/var/spool/cron/crontabs/root
    command:
      - crond
      - -f
      - -l
      - 2
```

```cron
# scripts/crontab
# ── MemGraph Scheduled Task Triggers ──
# Community summarisation — nightly 2am
0 2 * * *   wget -qO- --header="Authorization: Bearer $MEMGRAPH_ADMIN_KEY" $API_URL/v1/admin/workers/schedule/summarise_community

# Entity dedup — weekly Sunday 3am
0 3 * * 0   wget -qO- --header="Authorization: Bearer $MEMGRAPH_ADMIN_KEY" $API_URL/v1/admin/workers/schedule/merge_duplicate_entities

# Data retention cleanup — nightly 4am
0 4 * * *   wget -qO- --header="Authorization: Bearer $MEMGRAPH_ADMIN_KEY" $API_URL/v1/admin/workers/schedule/data_retention_cleanup

# DLQ purge — nightly 4:30am
30 4 * * *  wget -qO- --header="Authorization: Bearer $MEMGRAPH_ADMIN_KEY" $API_URL/v1/admin/workers/schedule/dlq_purge
```

---

## 7. Testing

### 7.1 Unit Tests

```python
@pytest.mark.asyncio
async def test_schedule_endpoint_valid_tasks(async_client, admin_headers):
    """All valid scheduled task names should return 200."""
    valid_tasks = [
        "summarise_community",
        "merge_duplicate_entities",
        "data_retention_cleanup",
        "cache_warming",
        "dlq_purge",
    ]
    for task in valid_tasks:
        response = await async_client.post(
            f"/v1/admin/workers/schedule/{task}",
            headers=admin_headers,
        )
        assert response.status_code == 200, f"Failed for task: {task}"
        assert response.json()["status"] == "enqueued"


@pytest.mark.asyncio
async def test_schedule_endpoint_invalid_task(async_client, admin_headers):
    """Unknown task names should return 404."""
    response = await async_client.post(
        "/v1/admin/workers/schedule/nonexistent_task",
        headers=admin_headers,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_schedule_endpoint_requires_auth(async_client):
    """Schedule endpoint should require admin authentication."""
    response = await async_client.post(
        "/v1/admin/workers/schedule/summarise_community",
    )
    assert response.status_code == 401
```

### 7.2 Integration Tests

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_summarise_community_schedule(async_client, admin_headers, arq_redis):
    """Triggering summarise_community schedule should enqueue a job."""
    response = await async_client.post(
        "/v1/admin/workers/schedule/summarise_community",
        headers=admin_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["task_name"] == "summarise_community"

    # Verify the job was enqueued to the low queue
    low_queue = "memgraph:test:queue:low:jobs"
    depth = await arq_redis.zcard(low_queue)
    assert depth == 1  # Our job plus any cleanup jobs
    assert data["job_id"] is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_apscheduler_registers_jobs():
    """When APScheduler starts, it should register all scheduled jobs."""
    from services.worker.scheduler import ScheduledTaskScheduler

    scheduler = ScheduledTaskScheduler(redis=...)  # mock redis
    scheduler.start()

    jobs = scheduler._scheduler.get_jobs()
    job_ids = {job.id for job in jobs}

    assert "summarise_community" in job_ids
    assert "merge_duplicate_entities" in job_ids
    assert "data_retention_cleanup" in job_ids
    assert "dlq_purge" in job_ids

    scheduler.stop()
```

---

## 8. Monitoring Scheduled Tasks

### 8.1 Prometheus Metrics

The scheduled task trigger is itself an enqueue operation — the same `memgraph_worker_tasks_total` metric covers scheduled tasks:

```yaml
# Example PromQL queries for scheduled tasks:

# Did summarise_community run last night?
increase(memgraph_worker_tasks_total{task_type="summarise_community"}[24h])

# How long did it take?
histogram_quantile(0.95,
  rate(memgraph_worker_task_duration_seconds{task_type="summarise_community"}[7d])
)

# Did it succeed?
rate(memgraph_worker_tasks_total{task_type="summarise_community", status="failure"}[7d])
```

### 8.2 Alert Rules

```yaml
- alert: MemGraphScheduledTaskNotRun
  expr: increase(memgraph_worker_tasks_total{task_type="summarise_community"}[28h]) == 0
  for: 2h
  labels:
    severity: warning
  annotations:
    summary: "Community summarisation has not run in the last 28 hours"
    description: "The nightly summarise_community task may have failed to trigger"

- alert: MemGraphDataRetentionNotRunning
  expr: increase(memgraph_worker_tasks_total{task_type="data_retention_cleanup"}[28h]) == 0
  for: 2h
  labels:
    severity: warning
  annotations:
    summary: "Data retention cleanup has not run in the last 28 hours"
```

---

## 9. SRS Traceability

| SRS ID | Requirement | How Covered |
|--------|-------------|-------------|
| NLP-15–17 | Community detection and summarisation on configurable schedule | §2.1: Nightly schedule; §3: CronJob; §4: APScheduler |
| WRK-07 | Low-priority queue for batch tasks | Scheduled tasks enqueued to `low` queue |
| SEC-04 | GDPR cascade deletion | §2.1 `data_retention_cleanup` — hard-delete after 30 days |
| WRK-06 | Dead-letter queue management | §2.1 `dlq_purge` — auto-purge entries older than 7 days |

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
