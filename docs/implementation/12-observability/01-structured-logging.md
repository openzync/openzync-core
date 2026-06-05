# Structured Logging Guide

## Overview

OpenZep uses **structlog** for structured JSON logging in production and coloured console output in development. Every log entry carries standard fields for correlation, debugging, and observability integration with Loki.

---

## Library

| Environment | Formatter | Library |
|---|---|---|
| Production | JSON (`structlog.processors.JSONRenderer`) | `structlog` |
| Development | Coloured console (`structlog.dev.ConsoleRenderer`) | `structlog` |

Install:

```bash
pip install structlog
```

---

## Configuration

### `core/logging.py`

```python
import structlog
import logging
from structlog.processors import JSONRenderer, TimeStamper, StackInfoRenderer
from structlog.stdlib import ProcessorFormatter, add_log_level
from structlog.contextvars import bind_contextvars, merge_contextvars

from app.core.config import settings


def setup_logging() -> None:
    """Configure structlog processors and integrate with stdlib logging.

    Call once at application startup in main.py.
    """
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        TimeStamper(fmt="iso", utc=True),
        StackInfoRenderer(),
        add_pii_redaction,  # custom processor — redacts PII before output
        structlog.processors.format_exc_info,
        structlog.processors.UnsafeConsoleRenderer() if settings.ENVIRONMENT == "development"
        else structlog.processors.JSONRenderer(),
    ]

    if settings.ENVIRONMENT == "production":
        # Production: JSON output via stdlib logging, shipped to Loki
        handler = logging.StreamHandler()
        handler.setFormatter(ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processor=JSONRenderer(),
        ))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(settings.LOG_LEVEL.upper())

        # Silence noisy libs
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    else:
        # Development: coloured console output
        structlog.configure(
            processors=shared_processors,
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
```

### Environment-driven config

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `ENVIRONMENT` | `development` | `development` or `production` — controls formatter selection |

---

## Standard Fields

### Global fields — every log entry

| Field | Type | Source | Example |
|---|---|---|---|
| `timestamp` | ISO-8601 string | `TimeStamper` | `2026-06-05T10:30:00Z` |
| `level` | string | `add_log_level` | `INFO` |
| `service` | string | env config | `api`, `worker`, `mcp` |
| `environment` | string | env config | `production`, `staging`, `development` |
| `trace_id` | string | OpenTelemetry | `e8f7c3a1b2...` |
| `span_id` | string | OpenTelemetry | `a1b2c3d4e5...` |
| `org_id` | string (optional) | request context | `org_abc123` |
| `user_id` | string (optional) | request context | `user_xyz789` |

### Request-scoped fields (api service)

| Field | Type | Example |
|---|---|---|
| `method` | string | `POST` |
| `path` | string | `/v1/users/{user_id}/memory` |
| `status_code` | int | `202` |
| `duration_ms` | float | `145.3` |
| `request_id` | string | `req_01j9xmf...` |

### Worker-scoped fields (worker service)

| Field | Type | Example |
|---|---|---|
| `task_type` | string | `extract_entities` |
| `job_id` | string | `f47ac10b-58cc-...` |
| `queue` | string | `high` |
| `attempt` | int | `2` |

---

## Request ID Injection

A FastAPI middleware injects a unique `request_id` and binds request-scoped context to structlog's context vars.

### `middleware/logging.py`

```python
import uuid
import time
from structlog.contextvars import bind_contextvars, clear_contextvars

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import logger


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Inject request_id and bind structlog context for every request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        start = time.monotonic()

        clear_contextvars()
        bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            service="api",
        )

        response: Response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 2)

        bind_contextvars(
            status_code=response.status_code,
            duration_ms=duration_ms,
        )

        # Remove path-specific path parameters from logged path for consistency
        # (e.g. /v1/users/{uuid}/memory instead of /v1/users/abc/memory)
        logger.info("request.completed")

        response.headers["X-Request-ID"] = request_id
        return response
```

### Register in `main.py`

```python
from app.middleware.logging import RequestLoggingMiddleware

app.add_middleware(RequestLoggingMiddleware)
```

---

## Log Levels Guide

| Level | Usage | Examples |
|---|---|---|
| **DEBUG** | Detailed tracing for development debugging | `LLM response received`, `Graphiti query result`, `Cache lookup key` |
| **INFO** | Business events — normal operations | `User created`, `Memory ingested`, `Context assembled`, `Worker task started` |
| **WARNING** | Recoverable issues — retry expected | `LLM API timeout (attempt 2/3)`, `Rate limit approaching`, `Cache miss` |
| **ERROR** | Exceptions, failed operations | `Entity extraction failed after 3 retries`, `DB connection error` |
| **CRITICAL** | System-level failure — manual intervention needed | `Database unreachable`, `Redis connection lost`, `Auth provider down` |

### Logging patterns

```python
# INFO — business event
logger.info("memory.ingested", extra={
    "org_id": str(org_id),
    "user_id": str(user_id),
    "session_id": str(session_id),
    "message_count": len(messages),
})

# WARNING — LLM retry
logger.warning("llm.timeout", extra={
    "model": "gpt-4o",
    "attempt": attempt,
    "max_retries": 3,
    "backoff_seconds": backoff,
})

# ERROR — task failure
logger.error("worker.task_failed", extra={
    "task_type": "extract_entities",
    "job_id": str(job_id),
    "queue": "high",
    "attempt": attempt,
    "error": str(exc),
    "traceback": traceback.format_exc(),
})

# CRITICAL — DB unreachable
logger.critical("database.unreachable", extra={
    "host": settings.DATABASE_URL.host,
    "port": settings.DATABASE_URL.port,
    "error": str(exc),
})
```

---

## PII Redaction

**Never log**: auth headers, API keys, message content, personal data (names, emails, phone numbers).

The PII redaction processor runs as a structlog processor:

```python
SENSITIVE_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie"}
SENSITIVE_FIELDS = {
    "content",         # message text
    "email",           # user email
    "api_key",         # raw API key
    "password",        # any password field
    "secret",          # any secret
    "token",           # JWT or refresh token
    "key_hash",        # stored key hash
    "raw_key",         # newly generated key (shown once at creation)
}
LLM_INPUT_FIELDS = {
    "prompt",          # LLM prompt content
    "messages",        # LLM message array
    "completion",      # LLM response content
}


def add_pii_redaction(logger, method_name, event_dict):
    """Redact sensitive field values — log the field name but not the value."""
    for field in SENSITIVE_FIELDS:
        if field in event_dict:
            event_dict[field] = f"[REDACTED:{field}]"

    # Redact entire LLM payloads — log metadata only
    for field in LLM_INPUT_FIELDS:
        if field in event_dict:
            if isinstance(event_dict[field], str) and len(event_dict[field]) > 50:
                event_dict[field] = f"[REDACTED:LLM_{field.upper()}:{len(original)}_chars]"

    return event_dict
```

### What gets logged for PII events

```python
# DO NOT log
logger.info("user.created", extra={"email": "user@example.com", "name": "John Doe"})

# INSTEAD log
logger.info("user.created", extra={"user_id": str(user_id), "org_id": str(org_id)})

# DO NOT log API keys
logger.info("auth.authenticated", extra={"api_key": "mg_live_abc123..."})

# INSTEAD log
logger.info("auth.authenticated", extra={"key_prefix": "mg_live_", "org_id": str(org_id)})
```

---

## Worker Logging Setup

### `services/worker/logging_setup.py`

```python
import structlog
from structlog.contextvars import bind_contextvars


def setup_worker_logging() -> None:
    """Configure structlog for ARQ worker processes."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            TimeStamper(fmt="iso", utc=True),
            add_pii_redaction,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def bind_worker_context(task_type: str, job_id: str, queue: str, attempt: int) -> None:
    """Bind worker-scoped context vars before task execution."""
    bind_contextvars(
        service="worker",
        task_type=task_type,
        job_id=job_id,
        queue=queue,
        attempt=attempt,
    )
```

Called at the start of every worker task:

```python
async def extract_entities_task(ctx, episode_id: str, org_id: str, user_id: str):
    bind_worker_context("extract_entities", str(ctx["job"].id), "high", ctx["job"].attempt)
    bind_contextvars(org_id=org_id, user_id=user_id)

    logger.info("worker.task_started")
    try:
        # ... extraction logic ...
        logger.info("worker.task_completed", extra={"entities_found": len(entities)})
    except Exception:
        logger.exception("worker.task_failed")
        raise
```

---

## Log Shipping to Loki

Grafana Alloy collects container logs via Docker log driver and ships them to Loki.

### Docker Compose log config (production)

```yaml
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"
    tag: "{{.Name}}"
```

### Alloy Loki configuration

```river
local.file_match "memgraph_logs" {
  path_targets = [{"__path__" = "/var/lib/docker/containers/*/*.json"}]
}

loki.source.file "OpenZep" {
  targets    = local.file_match.memgraph_logs.targets
  forward_to = [loki.write.default.receiver]
}

loki.write "default" {
  endpoint {
    url = env("LOKI_ENDPOINT")
  }
}
```

---

## Log Query Examples (Loki)

```logql
# All errors in the last hour
{service="api"} |= `"level":"ERROR"`

# Worker task failures for entity extraction
{service="worker", task_type="extract_entities"} |= `"level":"ERROR"`

# All logs for a specific trace
{trace_id="e8f7c3a1b2..."}

# Slow requests (> 1s)
{service="api"} | json | duration_ms > 1000

# Rate of memory ingestion by org
sum by(org_id) (rate({service="api", path="/v1/users/{user_id}/memory"}[5m]))
```
