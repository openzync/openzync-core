# OpenTelemetry Tracing Guide

## Overview

OpenZep uses OpenTelemetry for distributed tracing. Traces are exported via OTLP gRPC to Grafana Alloy, which forwards to Tempo. All services (API, worker, MCP) are instrumented to produce a unified trace view from HTTP request → worker task → LLM call → database query.

---

## Setup

### Installation

```bash
pip install opentelemetry-distro \
            opentelemetry-instrumentation-fastapi \
            opentelemetry-instrumentation-redis \
            opentelemetry-instrumentation-httpx \
            opentelemetry-instrumentation-sqlalchemy \
            opentelemetry-instrumentation-asyncpg \
            opentelemetry-exporter-otlp \
            opentelemetry-sdk
```

### Base Configuration (`core/tracing.py`)

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

from app.core.config import settings


def setup_tracing(app=None, engine=None, service_name: str = "api"):
    """Configure OpenTelemetry tracing for a service.

    Args:
        app: FastAPI application (if service is api or mcp).
        engine: SQLAlchemy async engine (for DB tracing).
        service_name: One of 'api', 'worker', 'mcp'.
    """
    resource = Resource.create({
        "service.name": f"OpenZep-{service_name}",
        "service.version": settings.VERSION,
        "deployment.environment": settings.ENVIRONMENT,
    })

    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(
        OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, insecure=True),
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    # Instrumentation
    if app is not None:
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)

    RedisInstrumentor().instrument(tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)

    if engine is not None:
        SQLAlchemyInstrumentor().instrument(
            tracer_provider=provider,
            engine=engine.sync_engine,
        )

    return provider
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://alloy:4317` | OTLP gRPC endpoint |
| `OTEL_SERVICE_NAME` | `OpenZep-api` | Override service name |
| `OTEL_SAMPLER` | `parentbased_always_on` | Sampler type |
| `OTEL_SAMPLER_ARG` | — | Sampler configuration |

---

## Trace Propagation

### W3C TraceContext Format

All services propagate traces using the W3C TraceContext format via the `traceparent` HTTP header:

```
traceparent: 00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01
```

| Part | Length | Description |
|---|---|---|
| Version | 2 hex | `00` |
| Trace ID | 32 hex | Global trace identifier |
| Span ID | 16 hex | Parent span identifier |
| Flags | 2 hex | `01` = sampled |

### FastAPI → Worker Propagation

When an HTTP request enqueues an ARQ job, the current trace context is serialised into the job metadata:

```python
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


async def enqueue_with_trace(job_class, *args, **kwargs):
    """Enqueue an ARQ job with OpenTelemetry trace context."""
    carrier = {}
    TraceContextTextMapPropagator().inject(carrier)
    trace_id = carrier.get("traceparent", "")

    await redis.enqueue_job(
        job_class.__name__,
        *args,
        **kwargs,
        _job_id_extra={"traceparent": trace_id},  # stored in ARQ job metadata
    )
```

Worker extracts the trace context and creates a child span:

```python
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def extract_trace_context(job) -> trace.Span | None:
    """Extract parent trace context from ARQ job metadata.

    Returns the current span to use as parent, or None if no context.
    """
    carrier = job.metadata.get("traceparent")
    if not carrier:
        return None

    ctx = TraceContextTextMapPropagator().extract(carrier={"traceparent": carrier})
    tracer = trace.get_tracer(__name__)
    return tracer.start_span(
        f"worker.{job.metadata.get('task_type', 'unknown')}",
        context=ctx,
        kind=trace.SpanKind.CONSUMER,
    )
```

---

## Span Definitions

### HTTP → Worker Trace Flow

```
TRACE: POST /v1/users/{user_id}/memory
├── SPAN: FastAPI request handler (api)
│   ├── SPAN: validate request body
│   ├── SPAN: store messages in DB
│   ├── SPAN: enqueue worker jobs
│   └── SPAN: return 202 Accepted
│
├── SPAN: extract_entities worker task (worker)
│   ├── SPAN: LLM API call (extract entities)
│   ├── SPAN: upsert entities to graph (Graphiti)
│   └── SPAN: enqueue embed_episode
│
├── SPAN: embed_episode worker task (worker)
│   ├── SPAN: embedding API call
│   └── SPAN: store embedding in DB
│
└── SPAN: extract_facts worker task (worker)
    ├── SPAN: LLM API call (extract facts)
    └── SPAN: store facts in DB
```

### Span Attributes

#### API Span

| Attribute | Type | Example |
|---|---|---|
| `http.method` | string | `POST` |
| `http.route` | string | `/v1/users/{user_id}/memory` |
| `http.status_code` | int | `202` |
| `http.request_id` | string | `req_01j9xmf...` |
| `enduser.id` | string | `org_abc` |
| `OpenZep.org_id` | string | `org_abc` |
| `OpenZep.user_id` | string | `user_xyz` |

#### LLM API Call Span

| Attribute | Type | Example |
|---|---|---|
| `llm.model` | string | `gpt-4o` |
| `llm.prompt_tokens` | int | `1245` |
| `llm.completion_tokens` | int | `342` |
| `llm.temperature` | float | `0.3` |
| `llm.operation` | string | `extract_entities` |
| `llm.provider` | string | `openai`, `ollama` |

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

async def call_llm_for_extraction(messages: list, model: str, temperature: float = 0.3):
    with tracer.start_as_current_span("llm.extract_entities") as span:
        span.set_attribute("llm.model", model)
        span.set_attribute("llm.temperature", temperature)
        span.set_attribute("llm.operation", "extract_entities")
        span.set_attribute("llm.provider", settings.LLM_BACKEND)

        response = await openai_client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
        )

        span.set_attribute("llm.prompt_tokens", response.usage.prompt_tokens)
        span.set_attribute("llm.completion_tokens", response.usage.completion_tokens)

        return response
```

#### Graphiti Query Span

| Attribute | Type | Example |
|---|---|---|
| `graph.backend` | string | `falkordb` |
| `graph.operation_type` | string | `upsert_entity`, `query_relations`, `bfs_traverse`, `get_community` |
| `graph.entity_count` | int | `5` |
| `graph.depth` | int | `2` |

```python
with tracer.start_as_current_span("graph.bfs_traverse") as span:
    span.set_attribute("graph.backend", settings.GRAPH_BACKEND)
    span.set_attribute("graph.operation_type", "bfs_traverse")
    span.set_attribute("graph.depth", depth)

    result = await graphiti_client.traverse(
        user_id=user_id,
        depth=depth,
    )

    span.set_attribute("graph.entity_count", len(result.nodes))
```

#### DB Query Span

Automatically captured by `SQLAlchemyInstrumentor`. No manual instrumentation needed.

---

## Sampling

### Strategy: Head-based sampling

| Service | Default rate | Rationale |
|---|---|---|
| API | 5% | High volume (target 500 req/s); 5% gives sufficient signal |
| Worker | 100% | Lower volume; every task is important for debugging |
| MCP | 10% | Moderate volume |

### Configuration

```python
from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio


def get_sampler(service_name: str):
    """Return sampler based on service type."""
    if service_name == "worker":
        # Always sample worker tasks
        return trace.sampling.ALWAYS_ON
    elif service_name == "mcp":
        # 10% sample for MCP
        return ParentBasedTraceIdRatio(0.1)
    else:
        # 5% sample for API
        return ParentBasedTraceIdRatio(0.05)
```

### Worker Override

Because workers always sample, any trace that reaches a worker will be fully captured — the parent-based sampler ensures the API span is also retained:

```python
# Worker uses ALWAYS_ON, which forces parent spans to be kept
provider = TracerProvider(
    resource=resource,
    sampler=trace.sampling.ALWAYS_ON,
)
```

---

## Export

### OTLP gRPC to Grafana Alloy

```python
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

exporter = OTLPSpanExporter(
    endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,  # http://alloy:4317
    insecure=True,  # internal network, no TLS needed
)
```

Alloy configuration to receive and forward traces:

```river
otelcol.receiver.otlp "default" {
  grpc {
    endpoint = "0.0.0.0:4317"
  }
  http {
    endpoint = "0.0.0.0:4318"
  }

  output {
    traces = [otelcol.processor.batch.default.input]
  }
}

otelcol.processor.batch "default" {
  output {
    traces = [otelcol.exporter.otlp.tempo.input]
  }
}

otelcol.exporter.otlp "tempo" {
  client {
    endpoint = env("TEMPO_ENDPOINT")
  }
}
```

### Environment Variables for Alloy

| Variable | Default | Description |
|---|---|---|
| `TEMPO_ENDPOINT` | — | Tempo gRPC endpoint (e.g. `tempo:4317`) |

---

## Correlation: Logs ↔ Traces

Every log entry includes `trace_id` and `span_id`. This enables jumping between logs in Loki and traces in Tempo.

### In Kubernetes / Alloy

Alloy's Loki processor enriches log entries with `trace_id` and `span_id` from OpenTelemetry context:

```river
loki.process "add_trace_context" {
  stage.stack {
    stage.otel_trace_context {
      source = "trace"
    }
  }
  forward_to = [loki.write.default.receiver]
}
```

### Querying

```logql
# Jump from log to trace — click trace_id in Loki to open Tempo
{service="OpenZep-worker"} |= `"level":"ERROR"`

# Jump from trace to logs — Tempo shows related Loki entries
```

### Manual Correlation

```python
# Log entry with trace context — automatically included by structlog
logger.error("llm.timeout", extra={
    "model": "gpt-4o",
    "org_id": str(org_id),
    # trace_id and span_id are automatically bound via contextvars
})
```

---

## FastAPI Integration (main.py)

```python
from app.core.tracing import setup_tracing
from app.core.db import engine

# At app startup
setup_tracing(
    app=app,
    engine=engine,
    service_name="api",
)
```

---

## Worker Integration (worker.py)

```python
from app.core.tracing import setup_tracing

# At worker startup
provider = setup_tracing(service_name="worker")

# Inside each task
def get_tracer():
    return trace.get_tracer("OpenZep.worker")

async def extract_entities_task(ctx, episode_id: str, org_id: str, user_id: str):
    tracer = get_tracer()
    parent_span = extract_trace_context(ctx["job"])

    with tracer.start_as_current_span(
        "worker.extract_entities",
        context=trace.set_span_in_context(parent_span) if parent_span else None,
    ) as span:
        span.set_attribute("episode_id", episode_id)
        span.set_attribute("org_id", org_id)
        span.set_attribute("user_id", user_id)
        # ... task logic ...
```

---

## Grafana Tempo Integration

### TraceQL Examples

```traceql
# All traces with LLM errors
{ span.llm.operation = "extract_entities" && status = error }

# Slow context assembly traces
{ span.http.route = "/v1/users/{user_id}/context" && span.http.status_code = 200 }
| duration > 1s

# Find traces by org
{ resource.deployment.environment = "production" && resource.OpenZep.org_id = "org_abc" }

# Trace waterfall for a specific request ID
{ span.http.request_id = "req_01j9xmf..." }
```
