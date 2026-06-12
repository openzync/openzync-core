# Metrics Definitions & Alerting

## Overview

OpenZep exposes Prometheus metrics via a `/metrics` endpoint on the FastAPI gateway and a separate `/metrics` endpoint on the worker service. Metrics are scraped by Grafana Alloy and forwarded to Mimir for long-term storage and alerting.

---

## Metrics Registry

All metrics use the `openzep_` prefix. Define in `packages/core/metrics.py`.

### Installation

```bash
pip install prometheus-client
```

---

## SRS Section 11.1 Metrics

### HTTP Request Count

```python
from prometheus_client import Counter, Histogram, Gauge

http_requests_total = Counter(
    "openzep_http_requests_total",
    "Total HTTP requests",
    labelnames=["method", "path", "status", "org_id"],
)
```

| Label | Values | Source |
|---|---|---|
| `method` | `GET`, `POST`, `PATCH`, `DELETE` | Request method |
| `path` | `/v1/users/{user_id}/memory`, etc. | Route template (not raw path — avoids cardinality explosion) |
| `status` | `2xx`, `4xx`, `5xx` | Status code group |
| `org_id` | `org_abc`, `org_def`, `unknown` | From auth context |

### HTTP Request Duration

```python
http_request_duration_seconds = Histogram(
    "openzep_http_request_duration_seconds",
    "HTTP request latency in seconds",
    labelnames=["method", "path", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
```

**Buckets rationale:**

| Bucket | Purpose |
|---|---|
| `0.005` – `0.05` | Cache-hit context assembly (target p50 ≤ 50ms) |
| `0.1` – `0.25` | Typical API response with DB |
| `0.5` | Slow ingestion acknowledgment |
| `1.0` – `2.5` | Cold context assembly, full hybrid retrieval |

### Context Assembly Duration

```python
context_assembly_duration_seconds = Histogram(
    "openzep_context_assembly_duration_seconds",
    "Time to assemble a context block (hybrid retrieval + graph traversal)",
    labelnames=["cache_hit"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
```

| Label | Values |
|---|---|
| `cache_hit` | `true`, `false` |

### Worker Tasks Total

```python
worker_tasks_total = Counter(
    "openzep_worker_tasks_total",
    "Total worker tasks processed",
    labelnames=["task_type", "status"],
)
```

| Label | Values |
|---|---|
| `task_type` | `extract_entities`, `embed_episode`, `embed_entity`, `extract_facts`, `classify_dialog`, `extract_structured`, `summarise_community`, `ingest_business_data` |
| `status` | `success`, `failure`, `dead_letter` |

### Worker Queue Depth

```python
worker_queue_depth = Gauge(
    "openzep_worker_queue_depth",
    "Current number of jobs waiting in each ARQ queue",
    labelnames=["queue_name"],
)
```

| Label | Values |
|---|---|
| `queue_name` | `high`, `low` |

Updated every 15 seconds by a background gauge collector:

```python
async def collect_queue_metrics():
    """Background task to update queue depth gauges."""
    while True:
        for queue_name in ("high", "low"):
            queue = arq_create_queue(queue_name, redis)
            depth = await queue.count_queued()
            worker_queue_depth.labels(queue_name=queue_name).set(depth)
        await asyncio.sleep(15)
```

### Graph Nodes Total

```python
graph_nodes_total = Gauge(
    "openzep_graph_nodes_total",
    "Total graph entity nodes",
    labelnames=["org_id"],
)
```

Updated on a periodic schedule (every 5 minutes) via a SQL query:

```sql
SELECT organization_id AS org_id, COUNT(*) AS node_count
FROM graphiti_nodes
GROUP BY organization_id;
```

### Embedding Tokens Total

```python
embedding_tokens_total = Counter(
    "openzep_embedding_tokens_total",
    "Total tokens consumed for embedding generation",
    labelnames=["model", "org_id"],
)
```

| Label | Values |
|---|---|
| `model` | `text-embedding-3-small`, `nomic-embed-text`, etc. |
| `org_id` | Tenant identifier |

### LLM Tokens Total

```python
llm_tokens_total = Counter(
    "openzep_llm_tokens_total",
    "Total tokens consumed for LLM calls (prompt + completion)",
    labelnames=["model", "org_id", "operation"],
)
```

| Label | Values |
|---|---|
| `model` | `gpt-4o`, `gpt-4o-mini`, `ollama/llama3`, etc. |
| `org_id` | Tenant identifier |
| `operation` | `extract_entities`, `extract_facts`, `classify`, `summarise`, `context_assembly` |

---

## Audit-Added Metrics

These were identified during the observability audit as necessary gaps.

### Context Cache Hit / Miss

```python
context_cache_hit_total = Counter(
    "openzep_context_cache_hit_total",
    "Context block served from Redis cache",
    labelnames=["org_id"],
)

context_cache_miss_total = Counter(
    "openzep_context_cache_miss_total",
    "Context block assembled from scratch (cache miss)",
    labelnames=["org_id"],
)
```

**Purpose**: Track cache effectiveness. If miss rate > 50%, increase cache TTL or review eviction policy.

### DB Connection Pool Metrics

```python
db_connections_active = Gauge(
    "openzep_db_connections_active",
    "Active database connections in the pool",
    labelnames=["db_name"],
)

db_connections_idle = Gauge(
    "openzep_db_connections_idle",
    "Idle database connections in the pool",
    labelnames=["db_name"],
)
```

| Label | Values |
|---|---|
| `db_name` | `postgres`, `falkordb`, `redis` |

Updated every 30 seconds via pool status queries:

```python
async def collect_pool_metrics():
    while True:
        pg_pool = engine.pool
        db_connections_active.labels(db_name="postgres").set(pg_pool.size() - pg_pool.checkedin())
        db_connections_idle.labels(db_name="postgres").set(pg_pool.checkedin())
        await asyncio.sleep(30)
```

### Worker Task Duration

```python
worker_task_duration_seconds = Histogram(
    "openzep_worker_task_duration_seconds",
    "Duration of worker tasks in seconds",
    labelnames=["task_type", "status"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0),
)
```

**Purpose**: Identify slow workers. If `extract_entities` p99 exceeds 30s, investigate LLM latency or batching.

### Error Code Breakdown

```python
error_code_total = Counter(
    "openzep_error_code_total",
    "Total errors by error code",
    labelnames=["error_code", "service"],
)
```

| Label | Values |
|---|---|
| `error_code` | `RATE_LIMITED`, `NOT_FOUND`, `VALIDATION_ERROR`, `INSUFFICIENT_CREDITS`, `LLM_TIMEOUT`, `LLM_RATE_LIMITED`, `DB_CONNECTION_ERROR`, `WORKER_TIMEOUT`, `INTERNAL_ERROR` |
| `service` | `api`, `worker`, `mcp` |

---

## Metrics Endpoint Exposure

### FastAPI (`main.py`)

```python
from prometheus_client import make_asgi_app
from starlette.routing import Mount

metrics_app = make_asgi_app()

app.mount("/metrics", metrics_app)
```

**Important**: The `/metrics` endpoint is excluded from auth middleware. It must NOT require authentication.

### Worker Service

```python
from prometheus_client import start_http_server

# Start in worker main
start_http_server(9101)  # Worker metrics on port 9101
```

---

## Scrape Configuration (Alloy / Prometheus)

### `alloy/config.alloy`

```river
prometheus.scrape "openzep_api" {
  targets    = [{"__address__" = "api:8000"}]
  metrics_path = "/metrics"
  scrape_interval = "15s"
  forward_to = [prometheus.remote_write.mimir.receiver]
}

prometheus.scrape "openzep_worker" {
  targets    = [{"__address__" = "worker:9101"}]
  scrape_interval = "15s"
  forward_to = [prometheus.remote_write.mimir.receiver]
}

prometheus.remote_write "mimir" {
  endpoint {
    url = env("MIMIR_ENDPOINT")
  }
}
```

---

## Alerting Rules

### Prometheus Alert Rules (`infra/mimir/rules/OpenZep.yml`)

```yaml
groups:
  - name: OpenZep-api
    interval: 30s
    rules:
      - alert: HighErrorRate
        expr: |
          (
            sum(rate(openzep_http_requests_total{status="5xx"}[5m]))
            /
            sum(rate(openzep_http_requests_total[5m]))
          ) > 0.01
        for: 5m
        labels:
          severity: critical
          service: api
        annotations:
          summary: "API error rate above 1%"
          description: "Error rate is {{ $value | humanizePercentage }} over the last 5 minutes."

      - alert: HighContextLatency
        expr: |
          histogram_quantile(
            0.99,
            sum(rate(openzep_context_assembly_duration_seconds_bucket[5m])) by (le, cache_hit)
          ) > 2.0
        for: 5m
        labels:
          severity: critical
          service: api
        annotations:
          summary: "p99 context assembly latency above 2000ms"
          description: "p99 context latency is {{ $value }}s over the last 5 minutes."

      - alert: ElevatedContextLatency
        expr: |
          histogram_quantile(
            0.99,
            sum(rate(openzep_context_assembly_duration_seconds_bucket[5m])) by (le, cache_hit)
          ) > 1.0
        for: 5m
        labels:
          severity: warning
          service: api
        annotations:
          summary: "p99 context assembly latency above 1000ms"
          description: "p99 context latency is {{ $value }}s over the last 5 minutes."

  - name: OpenZep-worker
    interval: 30s
    rules:
      - alert: WorkerQueueDepthCritical
        expr: openzep_worker_queue_depth > 1000
        for: 5m
        labels:
          severity: critical
          service: worker
        annotations:
          summary: "Worker queue depth above 1000"
          description: "Queue {{ $labels.queue_name }} depth is {{ $value }}."

      - alert: HighFailedJobRate
        expr: |
          (
            sum(rate(openzep_worker_tasks_total{status="failure"}[5m]))
            /
            sum(rate(openzep_worker_tasks_total[5m]))
          ) > 0.05
        for: 5m
        labels:
          severity: critical
          service: worker
        annotations:
          summary: "Worker failed job rate above 5%"
          description: "Failed job rate is {{ $value | humanizePercentage }} over the last 5 minutes."

      - alert: SlowWorkerTask
        expr: |
          histogram_quantile(
            0.99,
            sum(rate(openzep_worker_task_duration_seconds_bucket[5m])) by (le, task_type)
          ) > 30.0
        for: 5m
        labels:
          severity: warning
          service: worker
        annotations:
          summary: "Worker task p99 latency above 30s"
          description: "Task {{ $labels.task_type }} p99 is {{ $value }}s."

  - name: OpenZep-infra
    interval: 30s
    rules:
      - alert: DBConnectionPoolHigh
        expr: |
          (
            openzep_db_connections_active{db_name="postgres"}
            /
            (openzep_db_connections_active{db_name="postgres"} + openzep_db_connections_idle{db_name="postgres"})
          ) > 0.8
        for: 5m
        labels:
          severity: warning
          service: infra
        annotations:
          summary: "PostgreSQL connection pool above 80% utilization"
          description: "Pool utilization is {{ $value | humanizePercentage }}."

      - alert: WorkerQueueDepthWarning
        expr: openzep_worker_queue_depth > 500
        for: 10m
        labels:
          severity: warning
          service: worker
        annotations:
          summary: "Worker queue depth above 500"
          description: "Queue {{ $labels.queue_name }} depth is {{ $value }}."
```

### Alert Threshold Summary

| Alert | Condition | For | Severity |
|---|---|---|---|
| HighErrorRate | Error rate > 1% | 5m | critical |
| HighContextLatency | p99 > 2000ms | 5m | critical |
| ElevatedContextLatency | p99 > 1000ms | 5m | warning |
| WorkerQueueDepthCritical | Depth > 1000 | 5m | critical |
| HighFailedJobRate | Failure rate > 5% | 5m | critical |
| SlowWorkerTask | p99 > 30s | 5m | warning |
| DBConnectionPoolHigh | Pool utilization > 80% | 5m | warning |
| WorkerQueueDepthWarning | Depth > 500 | 10m | warning |

---

## Instrumentation in Code

### FastAPI Middleware (`middleware/metrics.py`)

```python
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp
from app.core.metrics import http_requests_total, http_request_duration_seconds


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record HTTP request count and duration."""

    # Paths to exclude from metrics (health checks, metrics endpoint)
    EXCLUDED_PATHS = {"/health", "/ready", "/metrics"}

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.EXCLUDED_PATHS:
            return await call_next(request)

        method = request.method
        # Use route template, not raw path — e.g. /v1/users/{user_id}/memory
        path = request.scope.get("route_path", request.url.path)
        org_id = getattr(request.state, "org_id", "unknown")

        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start

        status_group = f"{response.status_code // 100}xx"
        http_requests_total.labels(
            method=method, path=path, status=status_group, org_id=org_id
        ).inc()
        http_request_duration_seconds.labels(
            method=method, path=path, status=status_group
        ).observe(duration)

        return response
```

### Worker Task Wrapper (`workers/tasks/base.py`)

```python
import time
from app.core.metrics import worker_tasks_total, worker_task_duration_seconds


def observe_task(task_type: str):
    """Decorator to record worker task metrics."""
    def decorator(func):
        async def wrapper(ctx, *args, **kwargs):
            start = time.monotonic()
            try:
                result = await func(ctx, *args, **kwargs)
                status = "success"
                return result
            except Exception:
                status = "failure"
                raise
            finally:
                duration = time.monotonic() - start
                worker_tasks_total.labels(task_type=task_type, status=status).inc()
                worker_task_duration_seconds.labels(
                    task_type=task_type, status=status
                ).observe(duration)
        return wrapper
    return decorator
```
