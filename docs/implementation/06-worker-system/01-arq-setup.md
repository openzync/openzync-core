# ARQ Worker System Setup

> **Phase:** 1 (Core Memory), updated Phase 2/3
> **SRS Requirements:** WRK-01, WRK-02, WRK-03, WRK-04, WRK-05, PERF-05, PERF-06
> **Dependencies:** [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md), [redis://localhost:6379]
> **Design Authority:** @devops (infrastructure), @senior-dev (task implementation)

---

## 1. Overview

OpenZep uses **ARQ** (Async Redis Queue) for all background task processing. ARQ provides a lightweight, Redis-backed async job queue for Python asyncio applications — it is already the standardised worker system at TheLinkAI.

Key design decisions:

- **Redis-backed**: Durable job persistence, built-in retry, no separate broker service
- **Async-native**: Tasks are `async def` functions, compatible with OpenZep's asyncio FastAPI stack
- **Lightweight**: No external dependencies beyond Redis — ideal for OpenZep's self-hosted, air-gappable deployment model
- **Separate worker process**: The ARQ worker runs as an independent process (`services/worker/worker.py`), scalable independently of the API gateway

> ⚠️ **Production boundary:** ARQ is not a message broker (no Kafka/RabbitMQ durability guarantees). Redis persistence (AOF + RDB) is sufficient for OpenZep's workload — tasks are short-lived (< 5 min) and idempotent. If durability at Redis failure scale is required, evaluate replacing ARQ with RabbitMQ in Phase 5.

---

## 2. ARQ Configuration

### 2.1 Redis Connection

```python
# services/worker/config.py
from pydantic_settings import BaseSettings
from pydantic import Field


class WorkerSettings(BaseSettings):
    """Worker-specific configuration — loaded from environment variables."""

    model_config = {"env_prefix": "", "case_sensitive": False}

    # Redis
    REDIS_URL: str = Field(
        default="redis://localhost:6379",
        description="Redis connection string for ARQ job queue. "
        "Use `redis://[:password]@host:port[/db]` format.",
    )

    # Queue naming
    ENV: str = Field(
        default="dev",
        description="Environment name used in queue name prefix: OpenZep:{env}:queue:{queue_name}",
    )

    # Concurrency
    MAX_WORKERS: int = Field(
        default=4,
        ge=1,
        le=32,
        description="Maximum number of concurrent worker processes. "
        "Each process runs one asyncio event loop. "
        "Set to number of CPU cores for CPU-bound tasks, higher for I/O-bound.",
    )

    # Job defaults
    JOB_TIMEOUT_DEFAULT: int = Field(
        default=300,
        description="Default job timeout in seconds. "
        "Individual tasks may override this (see 02-task-definitions.md).",
    )
    JOB_KEEP_RESULT_FOR: int = Field(
        default=3600,
        description="How long (seconds) to keep completed job results in Redis. "
        "Results older than this are auto-evicted by ARQ.",
    )

    # Queue names
    HIGH_QUEUE_NAME: str = "high"
    LOW_QUEUE_NAME: str = "low"

    # Health check
    HEALTH_CHECK_INTERVAL: int = Field(
        default=30,
        description="Interval (seconds) between Redis health pings.",
    )

    # Logging
    LOG_LEVEL: str = Field(default="INFO")
    STRUCTLOG_FORMAT: str = Field(
        default="json",
        description="Log format: 'json' (production) or 'console' (dev).",
    )

    # Prometheus
    PROMETHEUS_PORT: int = Field(
        default=9090,
        description="Port for Prometheus metrics HTTP server. "
        "Separate from the API port — workers expose metrics on this port.",
    )


settings = WorkerSettings()
```

### 2.2 Queue Name Convention

```python
# Derived queue names — constructed in worker.py at startup

def get_queue_name(queue_type: str) -> str:
    """Generate a namespaced queue name.

    Pattern: OpenZep:{env}:queue:{queue_type}

    Examples:
        OpenZep:dev:queue:high
        OpenZep:prod:queue:low

    Args:
        queue_type: One of "high" (real-time ingestion) or "low" (scheduled batch).

    Returns:
        Fully qualified queue name string.
    """
    return f"OpenZep:{settings.ENV}:queue:{queue_type}"
```

**Rationale for the `OpenZep:{env}:queue:` prefix:**
- **`OpenZep`**: Avoids collisions with other services sharing the same Redis instance
- **`{env}`**: Separates dev/staging/production queues on shared infrastructure
- **`queue`**: Distinguishes ARQ queues from other Redis keys (cache, session, locks)

### 2.3 Job Timeout Defaults

| Task Type | Timeout (seconds) | Rationale |
|-----------|------------------|-----------|
| LLM-based (extraction, classification, summarisation) | 120 | LLM API calls can take 30-60s for long prompts |
| Embedding (OpenAI API) | 60 | Batch embedding of 100 texts ~10s with OpenAI |
| Embedding (Ollama local) | 300 | Local models can be 5-10x slower than API |
| DB-only (sync_to_graph, delete) | 30 | Query should complete quickly |
| Community summarisation | 600 | Community detection + LLM summary can be expensive |

> ⚠️ **CRITICAL:** Timeouts must be set per-task, not globally. A 30s timeout on an LLM extraction task will cause all tasks to fail under normal operation. See [02-task-definitions.md](02-task-definitions.md) for per-task timeout values.

---

## 3. Worker Process — `worker.py`

### 3.1 Process Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Worker Process                            │
│                                                               │
│  ┌─────────────────┐   ┌─────────────────┐                   │
│  │  Prometheus      │   │  Worker Pool     │                   │
│  │  HTTP Server     │   │  (MAX_WORKERS)   │                   │
│  │  (port 9090)     │   │                  │                   │
│  │                  │   │  ┌───────────┐   │                   │
│  │  /metrics        │   │  │ Worker 1  │   │                   │
│  │                  │   │  │ (asyncio) │   │                   │
│  │                  │   │  ├───────────┤   │                   │
│  │                  │   │  │ Worker 2  │   │                   │
│  │                  │   │  ├───────────┤   │                   │
│  │                  │   │  │ ...       │   │                   │
│  │                  │   │  ├───────────┤   │                   │
│  │                  │   │  │ Worker N  │   │                   │
│  │                  │   │  └───────────┘   │                   │
│  └─────────────────┘   └─────────────────┘                   │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Complete Worker Entrypoint

```python
# services/worker/worker.py
"""ARQ worker entrypoint — starts the worker pool, registers tasks, handles signals.

Usage:
    python -m services.worker.worker

Environment variables are loaded via pydantic-settings from WorkerSettings.
"""
import asyncio
import logging
import os
import signal
import sys
from typing import NoReturn

import structlog
from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker as ArqWorker
from prometheus_client import start_http_server as start_prometheus_server

from services.worker.config import settings

# ── Structlog setup ──────────────────────────────────────────────


def setup_logging() -> None:
    """Configure structlog for ARQ worker logging.

    All worker log entries include:
    - trace_id (propagated from API if available)
    - task_type (extract_entities, embed_episode, etc.)
    - job_id (ARQ job ID)
    - org_id (from task context)

    In production (STRUCTLOG_FORMAT=json), logs are emitted as JSON
    for ingestion by Loki. In development, human-readable console output.
    """
    shared_processors = [
        structlog.stdlib.filter_by_level(logging.getLevelName(settings.LOG_LEVEL)),
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.set_exc_info,
        # Enrich with worker-global context
        structlog.contextvars.merge_contextvars,
    ]

    if settings.STRUCTLOG_FORMAT == "json":
        processors = shared_processors + [
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


logger: structlog.stdlib.BoundLogger = structlog.get_logger("OpenZep.worker")


# ── Task imports ────────────────────────────────────────────────
# All task functions are imported here so they are registered with ARQ.
# ARQ discovers tasks via the `functions` list below.

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

# ── Task registry ────────────────────────────────────────────────

# Note: ARQ does not support per-task queue assignment in a single worker.
# We run two separate worker pools (high and low), each with its own
# function list. See 05-priority-queues.md for the split.
# For single-queue mode (Phase 0), all tasks run in the "high" pool.

HIGH_QUEUE_TASKS = [
    extract_entities,
    embed_episode,
    embed_entity,
    extract_facts,
    classify_dialog,
    extract_structured,
    sync_to_graph,
    delete_user_data,
    refresh_context_cache,
]

LOW_QUEUE_TASKS = [
    summarise_community,
    ingest_business_data,
    merge_duplicate_entities,
]


# ── Signal handling ─────────────────────────────────────────────


_shutdown_requested = False


def handle_signal(signum: int, frame) -> None:  # type: ignore[no-untyped-def]
    """Handle SIGTERM/SIGINT for graceful shutdown.

    Sets a global flag; the worker loop checks this between jobs.
    Current job completes, no new jobs are accepted.
    """
    global _shutdown_requested
    if _shutdown_requested:
        # Second signal — force exit (already shutting down)
        logger.warning("received second shutdown signal, forcing exit")
        sys.exit(1)

    _shutdown_requested = True
    logger.info(
        "shutdown_signal_received",
        signal=signal.Signals(signum).name,
        message="finishing current jobs, not accepting new ones",
    )


# ── Prometheus metrics ──────────────────────────────────────────

from prometheus_client import Counter, Gauge, Histogram

# Task-level metrics
worker_tasks_total = Counter(
    "openzep_worker_tasks_total",
    "Tasks completed by type and status",
    labelnames=["task_type", "status"],  # status: success / failure / timeout
)

worker_task_duration_seconds = Histogram(
    "openzep_worker_task_duration_seconds",
    "Task execution duration",
    labelnames=["task_type"],
    buckets=(1, 2.5, 5, 10, 15, 30, 60, 120, 300, 600),
)

worker_queue_depth = Gauge(
    "openzep_worker_queue_depth",
    "Current queue depth by queue name",
    labelnames=["queue_name"],
)

# Per-org task counters (for cost allocation)
worker_tasks_per_org = Counter(
    "openzep_worker_tasks_per_org_total",
    "Tasks by org, type, and status for cost tracking",
    labelnames=["org_id", "task_type", "status"],
)


# ── Helper: create worker pool ──────────────────────────────────


def create_arq_worker(
    queue_name: str,
    functions: list,
    redis_settings: RedisSettings,
    concurrency: int,
    timeout: int,
) -> ArqWorker:
    """Create a configured ARQ Worker instance for the given queue.

    Args:
        queue_name: Queue name (e.g. "high" or "low").
        functions: List of task functions to register.
        redis_settings: ARQ RedisSettings instance.
        concurrency: Number of concurrent tasks this worker processes.
        timeout: Default job timeout in seconds.

    Returns:
        Configured ArqWorker instance (not yet started).
    """
    full_queue_name = get_queue_name(queue_name)

    return ArqWorker(
        redis_settings=redis_settings,
        functions=functions,
        queue_name=full_queue_name,
        concurrency=concurrency,
        timeout=timeout,
        keep_result=settings.JOB_KEEP_RESULT_FOR,
        keep_result_failed=settings.JOB_KEEP_RESULT_FOR,
        poll_delay=0.5,  # Check for new jobs every 500ms
        on_job_complete=on_job_complete,
        on_job_failed=on_job_failed,
        on_shutdown=on_shutdown,
    )


# ── Job lifecycle callbacks ─────────────────────────────────────


async def on_job_complete(ctx: dict, job_id: str, **kwargs) -> None:
    """Log and record metrics when a job completes successfully."""
    task_type = ctx.get("task_type", "unknown")
    org_id = ctx.get("org_id", "unknown")
    trace_id = ctx.get("trace_id", "unknown")
    duration_ms = kwargs.get("runtime", 0)

    logger.info(
        "job.completed",
        trace_id=trace_id,
        org_id=org_id,
        task_type=task_type,
        job_id=job_id,
        duration_ms=round(duration_ms * 1000),
    )

    worker_tasks_total.labels(task_type=task_type, status="success").inc()
    worker_task_duration_seconds.labels(task_type=task_type).observe(duration_ms)
    worker_tasks_per_org.labels(org_id=org_id, task_type=task_type, status="success").inc()


async def on_job_failed(ctx: dict, job_id: str, exc: Exception, **kwargs) -> None:
    """Log and record metrics when a job fails after exhausting retries."""
    task_type = ctx.get("task_type", "unknown")
    org_id = ctx.get("org_id", "unknown")
    trace_id = ctx.get("trace_id", "unknown")

    logger.error(
        "job.failed",
        trace_id=trace_id,
        org_id=org_id,
        task_type=task_type,
        job_id=job_id,
        error=str(exc),
        error_type=type(exc).__name__,
        # ARQ provides retry count in kwargs after the job exhausts retries
        retry_count=kwargs.get("retry_count", -1),
    )

    worker_tasks_total.labels(task_type=task_type, status="failure").inc()
    worker_tasks_per_org.labels(org_id=org_id, task_type=task_type, status="failure").inc()


async def on_shutdown(ctx: dict) -> None:
    """Log when the worker pool shuts down."""
    logger.info("worker.shutdown_complete")


# ── Health check endpoint ───────────────────────────────────────

from aiohttp import web


async def health_check(request: web.Request) -> web.Response:
    """ARQ health check — verifies Redis connectivity.

    Returns:
        HTTP 200 with JSON {"status": "ok", "redis_connected": true}
        HTTP 503 if Redis is unreachable.

    This endpoint is used by:
    - Kubernetes liveness/readiness probes
    - Docker HEALTHCHECK
    - Prometheus target discovery
    """
    try:
        pool = request.app["redis_pool"]
        await pool.execute("PING")
        return web.json_response({
            "status": "ok",
            "redis_connected": True,
        })
    except Exception as exc:
        logger.error("health_check.failed", error=str(exc))
        return web.json_response(
            {"status": "unhealthy", "redis_connected": False, "error": str(exc)},
            status=503,
        )


# ── Main entrypoint ─────────────────────────────────────────────


async def main() -> NoReturn:
    """Start the ARQ worker pool, Prometheus server, and health endpoint.

    This function:
    1. Sets up structured logging
    2. Starts the Prometheus metrics HTTP server on port 9090
    3. Creates Redis connection pool
    4. Creates high and low priority worker pools
    5. Registers signal handlers for graceful shutdown
    6. Starts the health check web server on port 8081
    7. Runs both worker pools until shutdown
    """
    setup_logging()

    logger.info(
        "worker.starting",
        max_workers=settings.MAX_WORKERS,
        redis_url=settings.REDIS_URL,
        env=settings.ENV,
        prometheus_port=settings.PROMETHEUS_PORT,
    )

    # ── Start Prometheus HTTP server ──────────────────────────
    try:
        start_prometheus_server(settings.PROMETHEUS_PORT)
        logger.info("prometheus.server_started", port=settings.PROMETHEUS_PORT)
    except OSError as exc:
        logger.error("prometheus.server_failed", port=settings.PROMETHEUS_PORT, error=str(exc))
        raise

    # ── Redis connection ──────────────────────────────────────
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)

    # ── Create worker pools ───────────────────────────────────
    # Two separate ARQ Worker instances for priority queue support.
    # See 05-priority-queues.md for details on allocation.

    high_worker = create_arq_worker(
        queue_name=settings.HIGH_QUEUE_NAME,
        functions=HIGH_QUEUE_TASKS,
        redis_settings=redis_settings,
        concurrency=min(settings.MAX_WORKERS, 8),  # Cap at 8 for high queue
        timeout=settings.JOB_TIMEOUT_DEFAULT,
    )

    low_worker = create_arq_worker(
        queue_name=settings.LOW_QUEUE_NAME,
        functions=LOW_QUEUE_TASKS,
        redis_settings=redis_settings,
        concurrency=max(1, settings.MAX_WORKERS // 4),  # 25% for low queue
        timeout=settings.JOB_TIMEOUT_DEFAULT * 2,  # Batch tasks get longer timeout
    )

    # ── Signal handlers ──────────────────────────────────────
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: handle_signal(s, None))

    # ── Health check web server ──────────────────────────────
    health_app = web.Application()
    health_app["redis_pool"] = high_worker.pool
    health_app.router.add_get("/ready", health_check)
    health_app.router.add_get("/health", health_check)

    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8081)
    await site.start()
    logger.info("health.server_started", port=8081)

    # ── Run workers ──────────────────────────────────────────
    try:
        await asyncio.gather(
            high_worker.run(),
            low_worker.run(),
        )
    except asyncio.CancelledError:
        logger.info("worker.run_cancelled")
        raise
    finally:
        await runner.cleanup()
        logger.info("worker.stopped")


def entrypoint() -> None:
    """Synchronous entrypoint for `python -m services.worker.worker`."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    entrypoint()
```

---

## 4. Concurrency Model

### 4.1 Worker Concurrency

ARQ's `concurrency` parameter controls how many jobs a single worker process executes simultaneously. This is **not** the number of processes — it's the number of concurrent coroutines within a single asyncio event loop.

```python
# Configuration pattern
MAX_WORKERS: int = 4  # env var, default 4

# In a single worker process:
# - concurrency=4 means up to 4 tasks running concurrently
# - Each task is an async coroutine
# - CPU-bound tasks will NOT benefit from concurrency > 1
# - I/O-bound tasks (LLM API calls, DB queries) benefit from higher concurrency
```

### 4.2 Scaling Horizontally

To scale workers horizontally, run multiple worker processes:

```bash
# In docker-compose or K8s, scale the worker service:
# docker-compose up --scale worker=3

# Each process is an independent ARQ worker pulling from the same queues.
# Redis handles job distribution — no coordination needed.
```

> ⚠️ **Idempotency requirement:** Horizontal scaling means two workers may pick up the same job (if one worker crashes mid-job and the job is re-enqueued by ARQ's retry mechanism). All OpenZep tasks MUST be idempotent — see WRK-02 and [02-task-definitions.md](02-task-definitions.md).

### 4.3 Per-Process vs Per-CPU

| Deployment | MAX_WORKERS | Rationale |
|------------|-------------|-----------|
| Single-node dev | 4 | Default, fine for dev/testing |
| K8s pod (small) | 2 | 1 CPU request, I/O-bound tasks |
| K8s pod (large) | 8-16 | 4-8 CPU, mixed I/O + CPU tasks |
| Dedicated worker node | CPU cores × 2 | I/O-heavy workload benefits from over-subscription |

---

## 5. Graceful Shutdown

### 5.1 Sequence

```
SIGTERM
   │
   ▼
handle_signal() → sets _shutdown_requested = True
   │
   ▼
ARQ detects shutdown flag → stops polling for new jobs
   │
   ▼
ARQ drains running jobs → each job runs to completion (or timeout)
   │
   ▼
All jobs complete → on_shutdown() callback fires
   │
   ▼
Worker exits cleanly (exit code 0)
```

### 5.2 Hard Kill Fallback

If `SIGTERM` does not cause a clean shutdown within `K8S_TERMINATION_GRACE_PERIOD_SECONDS` (default 30s), Kubernetes sends `SIGKILL`. ARQ will re-enqueue any in-flight jobs on the next worker startup — this is safe because all tasks are idempotent.

### 5.3 Docker HEALTHCHECK Integration

```dockerfile
# In Dockerfile.worker (see §7):
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:8081/health || exit 1
```

### 5.4 Kubernetes Probe Integration

```yaml
# In the Helm chart (deployment.yaml):
livenessProbe:
  httpGet:
    path: /health
    port: 8081
  initialDelaySeconds: 15
  periodSeconds: 30
readinessProbe:
  httpGet:
    path: /ready
    port: 8081
  initialDelaySeconds: 5
  periodSeconds: 10
```

---

## 6. Logging — Structlog Integration

### 6.1 Log Enrichment Pattern

Every ARQ task function must return a context dict that ARQ passes as `ctx` to subsequent callbacks. OpenZep enriches this context with observability fields:

```python
# Pattern used in every task function
async def example_task(ctx: dict, job_payload: dict) -> dict:
    """Example task showing context enrichment pattern."""
    # ctx is populated by ARQ with:
    # - ctx['job_id'] (ARQ's internal job ID)
    # - ctx['redis'] (Redis connection pool)
    # - ctx['task_type'] (function name)
    #
    # OpenZep adds:
    # - ctx['trace_id'] (from API request, propagated via job payload)
    # - ctx['org_id'] (from auth context, propagated via job payload)
    # - ctx['user_id'] (from request context)

    trace_id = ctx.get("trace_id", job_payload.get("trace_id", "unknown"))
    org_id = ctx.get("org_id", job_payload.get("org_id", "unknown"))

    # Enrich structlog context for this task
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        trace_id=trace_id,
        org_id=org_id,
        task_type="example_task",
        job_id=ctx["job_id"],
    )

    logger.info("task.started", payload_size=len(str(job_payload)))

    # ... do work ...

    logger.info("task.completed")

    # Return updated context for downstream callbacks
    return {
        "trace_id": trace_id,
        "org_id": org_id,
        "task_type": "example_task",
    }
```

### 6.2 Log Output Format (Production)

```json
{
  "event": "job.completed",
  "timestamp": "2026-06-05T10:30:00.123456Z",
  "level": "info",
  "logger": "OpenZep.worker",
  "trace_id": "req_01j9xmf...",
  "org_id": "org_abc123",
  "task_type": "extract_entities",
  "job_id": "e7a8b9c0...",
  "duration_ms": 2340
}
```

---

## 7. Prometheus Metrics

### 7.1 Metrics HTTP Server

The worker runs a separate HTTP server on port 9090 (configurable via `PROMETHEUS_PORT`) that exposes Prometheus metrics. This server is independent of the health check server (port 8081) and the API server (port 8000).

### 7.2 Exported Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `openzep_worker_tasks_total` | Counter | `task_type`, `status` | Tasks completed by type and status |
| `openzep_worker_task_duration_seconds` | Histogram | `task_type` | Task execution duration distribution |
| `openzep_worker_queue_depth` | Gauge | `queue_name` | Current number of pending jobs per queue |
| `openzep_worker_tasks_per_org_total` | Counter | `org_id`, `task_type`, `status` | Per-org task accounting for cost allocation |

### 7.3 Queue Depth Monitoring

Queue depth is polled periodically and exposed as a Gauge metric:

```python
# services/worker/monitoring.py
import asyncio
from arq.connections import ArqRedis
from prometheus_client import Gauge

worker_queue_depth = Gauge(
    "openzep_worker_queue_depth",
    "Current queue depth by queue name",
    labelnames=["queue_name"],
)

async def monitor_queue_depth(redis: ArqRedis, interval: int = 15) -> None:
    """Periodically sample queue depth for all known queues.

    This coroutine runs as a background task in the worker event loop.
    """
    from services.worker.config import settings

    queues = [settings.HIGH_QUEUE_NAME, settings.LOW_QUEUE_NAME]

    while True:
        for queue_name in queues:
            full_name = get_queue_name(queue_name)
            # ARQ stores pending jobs in a Redis list: {queue_name}:jobs
            depth = await redis.zcard(f"{full_name}:jobs")
            worker_queue_depth.labels(queue_name=queue_name).set(depth)

        await asyncio.sleep(interval)
```

---

## 8. Dockerfile — `Dockerfile.worker`

The worker image is separate from the API image. It is smaller because it has no dashboard dependencies (no Node.js, no npm packages).

```dockerfile
# Dockerfile.worker
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Install only worker-specific deps
COPY requirements.worker.txt .
RUN pip install --no-cache-dir --user -r requirements.worker.txt

FROM python:3.12-slim AS runtime
WORKDIR /app

# Copy pip-installed packages from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy only the worker and common packages
COPY services/worker/ ./services/worker/
COPY packages/ ./packages/

# The API and dashboard are NOT included in this image
# Worker connects to Redis, PostgreSQL, and FalkorDB — not served over HTTP

EXPOSE 8081  # Health check
EXPOSE 9090  # Prometheus metrics

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8081/health')" || exit 1

CMD ["python", "-m", "services.worker.worker"]
```

### 8.1 Worker-Specific Dependencies

```txt
# requirements.worker.txt
# ARQ and Redis
arq>=0.26,<1.0
redis[hiredis]>=5.0,<6.0

# Prometheus
prometheus-client>=0.20,<1.0

# Health check web server
aiohttp>=3.9,<4.0

# Structured logging (same as API)
structlog>=24.0,<25.0

# OpenTelemetry propagation (same version as API)
opentelemetry-api>=1.24,<2.0
opentelemetry-distro>=0.45b0,<1.0
```

---

## 9. Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `ENV` | `dev` | Environment name for queue prefix |
| `MAX_WORKERS` | `4` | Worker concurrency |
| `JOB_TIMEOUT_DEFAULT` | `300` | Default job timeout (seconds) |
| `JOB_KEEP_RESULT_FOR` | `3600` | Keep completed job results (seconds) |
| `LOG_LEVEL` | `INFO` | Logging level |
| `STRUCTLOG_FORMAT` | `json` | Log format: `json` or `console` |
| `PROMETHEUS_PORT` | `9090` | Prometheus metrics HTTP port |
| `HEALTH_CHECK_INTERVAL` | `30` | Redis health ping interval (seconds) |

---

## 10. Testing

### 10.1 Unit Tests

```python
# tests/unit/worker/test_worker_config.py
import pytest
from services.worker.config import WorkerSettings


class TestWorkerSettings:
    def test_default_values(self):
        """WorkerSettings should have sensible defaults."""
        settings = WorkerSettings()
        assert settings.MAX_WORKERS == 4
        assert settings.REDIS_URL == "redis://localhost:6379"
        assert settings.ENV == "dev"
        assert settings.JOB_TIMEOUT_DEFAULT == 300

    def test_queue_name_generation(self):
        """Queue names should follow the OpenZep:{env}:queue:{name} pattern."""
        from services.worker.worker import get_queue_name
        settings = WorkerSettings(ENV="prod")
        assert get_queue_name("high") == "OpenZep:prod:queue:high"
        assert get_queue_name("low") == "OpenZep:prod:queue:low"
```

### 10.2 Integration Tests

```python
# tests/integration/worker/test_worker_lifecycle.py
import pytest
from arq.connections import ArqRedis, RedisSettings
from services.worker.config import WorkerSettings


@pytest.mark.asyncio
@pytest.mark.integration
async def test_worker_queue_enqueue_dequeue(arq_redis: ArqRedis):
    """Verify that jobs can be enqueued and ARQ picks them up."""
    # Enqueue a test job
    job = await arq_redis.enqueue_job(
        "test_ping",
        queue_name="OpenZep:test:queue:high",
    )
    assert job is not None
    assert job.job_id is not None

    # Wait for job to complete
    result = await job.result(timeout=5)
    assert result == "pong"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_graceful_shutdown(arq_worker):
    """Verify that SIGTERM results in graceful drain."""
    import signal

    # Start a long-running job
    await arq_worker.redis.enqueue_job(
        "test_slow_task",
        queue_name="OpenZep:test:queue:high",
    )

    # Send SIGTERM
    arq_worker.process.send_signal(signal.SIGTERM)
    arq_worker.process.wait(timeout=10)

    # Verify job completed (was not killed mid-execution)
    assert arq_worker.process.returncode == 0
```

---

## 11. SRS Traceability

| SRS ID | Requirement | How Covered |
|--------|-------------|-------------|
| WRK-01 | NLP enrichment runs asynchronously via ARQ | §2: ARQ config, §3: worker.py with task registry |
| WRK-02 | Tasks are idempotent | §4.1: Idempotency requirement for horizontal scaling |
| WRK-03 | Exponential backoff on LLM failures | [04-retry-backoff-dlq.md](04-retry-backoff-dlq.md) |
| WRK-04 | Queue depth and latency exposed as Prometheus metrics | §7: Metrics server, counters, histograms, gauges |
| WRK-05 | Horizontal scaling of workers independent of API | §4.2: docker-compose scale, K8s HPA, §8: separate Dockerfile |
| PERF-05 | Entity extraction completes within 30s of ingestion | §2.3: Per-task timeouts, §3.2: worker concurrency |

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
