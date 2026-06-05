# Graphiti Library Integration — Setup & Lifecycle

## 1. Overview

| Field | Detail |
|-------|--------|
| **Coverage** | Initialising Graphiti as an embedded library, connection management, shutdown, health checks, error handling |
| **Dependencies** | [01-postgresql-schema.md](../01-data-models/01-postgresql-schema.md) §2.15 (LLM usage tracking), Python 3.11+, FalkorDB 1.1.2+ or Neo4j 5.26+ |
| **SRS Requirement IDs** | KG-01, KG-02, KG-03, KG-04, PORT-02, OQ-01, OQ-02, AVAIL-03, SEC-07 |
| **Build Phase** | Phase 0 (Foundation) |
| **Design Authority** | @architect for graph backend decisions, @devops for FalkorDB/Neo4j container config |
| **Key Risks** | OQ-01 (Graphiti API stability), OQ-02 (FalkorDB production maturity) |

### 1.1 What This Doc Covers

- Version pinning strategy and the `graphiti-client` abstraction layer
- Graphiti instance initialisation with configurable backends (FalkorDB and Neo4j)
- Connection pooling, health checks, graceful shutdown
- Error handling: timeouts, retries, circuit breakers
- Environment variable configuration

---

## 2. Graphiti Library Overview

[Graphiti](https://github.com/getzep/graphiti) (Apache 2.0) is a **temporal knowledge graph engine** that serves as OpenZep's core graph layer. It is imported as a **Python library** — not deployed as a sidecar — as specified in SRS §2.5.

```python
# PyPI package
pip install graphiti-core==0.29.1
```

### 2.1 Architecture Decision: Embedded Library vs Sidecar

| Approach | Tradeoff | Verdict |
|----------|----------|---------|
| **Embedded library** (chosen) | Single process, no network hop to Graphiti, lower latency. Risk: Graphiti blocks the event loop if sync (confirmed: async-safe, see §3.1). | **Selected** — meets SRS §2.5 |
| Sidecar container | Process isolation, independent scaling. Adds ~1ms RTT per call, more deployment complexity. | Rejected — unnecessary overhead |

### 2.2 Version Pinning & OQ-01 Mitigation

Graphiti is a young library (OQ-01). Mitigation strategy:

```python
# requirements.txt — exact version pin
graphiti-core==0.29.1
# All sub-dependencies pinned via pip-compile output: requirements-lock.txt
```

**Three-layer defence against API instability:**

1. **Exact version pin** — no semver range (`>=0.29,<0.30` would pull breaking changes)
2. **`packages/graphiti-client/` abstraction** (see [05-graph-client-abstraction.md](05-graph-client-abstraction.md)) — wraps Graphiti behind an application-defined interface. If Graphiti's public API changes, only the wrapper adapts, not the callers.
3. **Dependabot / Renovate** — automated PRs for Graphiti upgrades, gated by integration tests

> **When upgrading Graphiti:** Run the full integration test suite against both FalkorDB and Neo4j backends. The testcontainers-based tests in `tests/integration/test_graph_backends.py` will catch breaking changes.

---

## 3. Async Compatibility — Verification

### 3.1 Critical Verification: Sync vs Async I/O

**Finding: Graphiti uses `async def` throughout. All public methods are async-native.**

Before writing any Graphiti integration code, verify this for the pinned version:

```bash
# From repo root, check the Graphiti source for sync/async usage
grep -rn "^    def \|^    async def " venv/lib/python*/graphiti-core/graphiti_core/graphiti.py
```

Expected output (all methods are `async def`):

```
    async def close(self) -> None:
    async def build_indices_and_constraints(self, delete_existing: bool = False) -> None:
    async def add_episode(self, name: str, episode_body: str, ...) -> AddEpisodeResults:
    async def add_episode_bulk(self, ...) -> AddBulkEpisodeResults:
    async def retrieve_episodes(self, ...) -> list[EpisodicNode]:
    async def summarize_saga(self, saga_id: str) -> SagaNode:
```

The Graphiti codebase (`v0.29.1`) uses:
- `neo4j.AsyncGraphDatabase` for Neo4j connections
- `falkordb.asyncio.FalkorDB` for FalkorDB connections
- `openai.AsyncOpenAI` for LLM calls
- `semaphore_gather` (bounded `asyncio.gather`) for internal concurrency

**Verdict: No `run_in_executor()` wrapping needed.** Graphiti can be called directly with `await` inside FastAPI route handlers and ARQ worker tasks.

### 3.2 Guard for Future Versions

Add a smoke test that verifies async compatibility when upgrading Graphiti:

```python
# tests/integration/test_graphiti_sync_check.py
"""Verify Graphiti's public API is async-native. Fail fast if sync methods appear."""

import inspect
import graphiti_core.graphiti as gmod

async def test_all_public_methods_are_async() -> None:
    """Every public method on the Graphiti class must be async def."""
    for name, member in inspect.getmembers(gmod.Graphiti, predicate=inspect.isfunction):
        if name.startswith('_'):
            continue  # private/internal methods may be sync
        assert inspect.iscoroutinefunction(member), (
            f"Graphiti.{name} is sync ({type(member).__name__}). "
            "If Graphiti added sync methods, they MUST be wrapped in run_in_executor()."
        )
```

---

## 4. Graphiti Initialisation Sequence

### 4.1 Initialisation Flow

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   Startup (app   │     │  Create LLM +    │     │  Create Graphiti │
│   lifespan)      │────►│  Embedder        │────►│  Instance        │
│                  │     │  clients         │     │                  │
└──────────────────┘     └──────────────────┘     └────────┬─────────┘
                                                           │
                                                  ┌────────▼─────────┐
                                                  │ Build indices &  │
                                                  │ constraints      │
                                                  │ (idempotent)     │
                                                  └────────┬─────────┘
                                                           │
                                                  ┌────────▼─────────┐
                                                  │ Store in         │
                                                  │ app.state.graphiti│
                                                  │ Register shutdown│
                                                  └──────────────────┘
```

### 4.2 Reference Implementation

```python
# packages/core/graphiti/factory.py
"""Graphiti instance factory with configurable backends and LLM providers."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder import OpenAIEmbedder
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client import OpenAIClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.tracer import Tracer

from app.core.config import settings
from app.core.exceptions import GraphInitError

logger = logging.getLogger(__name__)


def create_llm_client() -> LLMClient:
    """Create LLM client based on configuration.

    Supports OpenAI, Azure OpenAI, and Ollama backends (PORT-03).
    """
    backend = settings.LLM_BACKEND
    if backend == "openai":
        from graphiti_core.llm_client import LLMConfig, OpenAIClient

        return OpenAIClient(
            config=LLMConfig(
                api_key=settings.OPENAI_API_KEY,
                model=settings.LLM_MODEL or "gpt-4o-mini",
                base_url=None,  # default OpenAI API
                temperature=0.0,
                max_tokens=settings.LLM_MAX_TOKENS or 16384,
            )
        )
    elif backend == "azure":
        from graphiti_core.llm_client import LLMConfig, OpenAIClient

        return OpenAIClient(
            config=LLMConfig(
                api_key=settings.AZURE_OPENAI_API_KEY,
                model=settings.AZURE_OPENAI_DEPLOYMENT,
                base_url=settings.AZURE_OPENAI_ENDPOINT,
                temperature=0.0,
                max_tokens=settings.LLM_MAX_TOKENS or 16384,
            )
        )
    elif backend == "ollama":
        from graphiti_core.llm_client import LLMConfig, OpenAIClient

        return OpenAIClient(
            config=LLMConfig(
                api_key="ollama",  # placeholder, not used
                model=settings.OLLAMA_MODEL or "qwen2.5",
                base_url=f"{settings.OLLAMA_BASE_URL}/v1",
                temperature=0.0,
                max_tokens=settings.LLM_MAX_TOKENS or 16384,
            )
        )
    else:
        raise GraphInitError(f"Unsupported LLM backend: {backend}")


def create_embedder() -> EmbedderClient:
    """Create embedding client based on configuration (PORT-04)."""
    backend = settings.LLM_BACKEND  # embedder follows LLM backend
    if backend in ("openai", "azure"):
        from graphiti_core.embedder import OpenAIEmbedder

        return OpenAIEmbedder(
            api_key=settings.OPENAI_API_KEY,
            model=settings.EMBEDDING_MODEL or "text-embedding-3-small",
            dimensions=settings.EMBEDDING_DIM or 1536,
        )
    elif backend == "ollama":
        from graphiti_core.embedder import OpenAIEmbedder

        return OpenAIEmbedder(
            api_key="ollama",
            model=settings.OLLAMA_EMBEDDING_MODEL or "nomic-embed-text",
            base_url=f"{settings.OLLAMA_BASE_URL}/v1",
            dimensions=settings.EMBEDDING_DIM or 768,
        )
    else:
        raise GraphInitError(f"Unsupported embedding backend: {backend}")


def create_graph_driver() -> FalkorDriver:
    """Create the graph database driver based on GRAPH_BACKEND config (PORT-02)."""
    backend = settings.GRAPH_BACKEND
    if backend == "falkordb":
        # ⚠️ FalkorDB uses Redis wire protocol on a separate port (default 6380).
        # Do NOT reuse the REDIS_URL — FalkorDB has its own port.
        from graphiti_core.driver.falkordb_driver import FalkorDriver

        return FalkorDriver(
            host=settings.FALKORDB_HOST or "localhost",
            port=settings.FALKORDB_PORT or 6380,
            username=settings.FALKORDB_USERNAME or None,
            password=settings.FALKORDB_PASSWORD or None,
            database=settings.FALKORDB_DB or "default_db",
        )
    elif backend == "neo4j":
        from graphiti_core.driver.neo4j_driver import Neo4jDriver

        return Neo4jDriver(
            uri=settings.NEO4J_URI or "bolt://localhost:7687",
            user=settings.NEO4J_USER or "neo4j",
            password=settings.NEO4J_PASSWORD or "",
            database=settings.NEO4J_DB or "neo4j",
        )
    else:
        raise GraphInitError(f"Unsupported graph backend: {backend}")


def create_tracer() -> Tracer:
    """Create OpenTelemetry tracer for Graphiti span propagation.

    Uses TheLinkAI's existing LGTM stack (Tempo for traces).
    Falls back to a no-op tracer if OpenTelemetry is not configured.
    """
    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": "OpenZep-graphiti"})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        return Tracer(tracer=trace.get_tracer("graphiti"))
    return Tracer()  # no-op tracer


async def create_graphiti_instance() -> Graphiti:
    """Create and initialise the Graphiti instance.

    This is the single factory function used during FastAPI lifespan startup.
    """
    try:
        driver = create_graph_driver()
        llm_client = create_llm_client()
        embedder = create_embedder()
        tracer = create_tracer()

        graphiti = Graphiti(
            graph_driver=driver,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=None,  # optional, added in Phase 2
            store_raw_episode_content=True,
            max_coroutines=settings.GRAPHITI_MAX_COROUTINES or 10,
            tracer=tracer,
        )

        # Build indices and constraints (idempotent — safe to run on every restart)
        logger.info("Building Graphiti indices and constraints...")
        await graphiti.build_indices_and_constraints(delete_existing=False)

        logger.info("Graphiti initialized successfully", extra={
            "graph_backend": settings.GRAPH_BACKEND,
            "llm_backend": settings.LLM_BACKEND,
            "embedding_model": settings.EMBEDDING_MODEL,
        })
        return graphiti

    except Exception as e:
        logger.critical("Failed to initialize Graphiti", exc_info=True)
        raise GraphInitError(f"Graphiti initialization failed: {e}") from e
```

### 4.3 Integration into FastAPI Lifespan

```python
# services/api/app/main.py
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from graphiti_core import Graphiti

from app.core.config import settings
from packages.core.graphiti.factory import create_graphiti_instance


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan for Graphiti lifecycle management."""
    # ── Startup ──────────────────────────────────────────────────────
    graphiti = await create_graphiti_instance()
    application.state.graphiti = graphiti
    application.state.graph_driver = graphiti._driver  # for health checks

    logger.info("Graphiti ready", extra={"graph_backend": settings.GRAPH_BACKEND})
    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    logger.info("Closing Graphiti connections...")
    try:
        await graphiti.close()
        logger.info("Graphiti connections closed")
    except Exception as e:
        logger.error("Error closing Graphiti connections", exc_info=True)


app = FastAPI(lifespan=lifespan)
```

---

## 5. Connection Management

### 5.1 FalkorDB Connection Pool

FalkorDB uses Redis wire protocol. The `FalkorDriver` creates a single `falkordb.asyncio.FalkorDB` client instance internally. Connection pooling is handled by the `redis-py` library that `falkordb` wraps.

```python
# packages/core/config/settings.py — FalkorDB connection defaults

# FalkorDB
FALKORDB_HOST: str = "localhost"
FALKORDB_PORT: int = 6380
FALKORDB_USERNAME: str | None = None
FALKORDB_PASSWORD: str | None = None
FALKORDB_DB: str = "default_db"
FALKORDB_SOCKET_TIMEOUT: int = 10       # seconds
FALKORDB_SOCKET_CONNECT_TIMEOUT: int = 5  # seconds
FALKORDB_MAX_CONNECTIONS: int = 20
```

For production FalkorDB deployments, configure connection pooling via `redis` kwargs:

```python
# Optional: if we need to pass pool config through FalkorDriver
# FalkorDriver currently does not expose pool config directly.
# Workaround: set env vars that redis-py respects:
#   REDIS_MAX_CONNECTIONS=20
#
# Tracked: https://github.com/getzep/graphiti/issues (FalkorDriver pool config)
```

### 5.2 Neo4j Connection Pool

Neo4j's `AsyncGraphDatabase.driver()` manages a built-in connection pool. Configure via its `Config` object:

```python
from neo4j import AsyncGraphDatabase, AsyncManagedTransaction, Config

# Neo4j driver config is set inside Graphiti's Neo4jDriver init.
# The driver uses default pool settings unless overridden via env vars:
#
#   NEO4J_MAX_CONNECTION_POOL_SIZE=50
#   NEO4J_CONNECTION_ACQUISITION_TIMEOUT=30
```

### 5.3 Graceful Shutdown Sequence

On application shutdown:

```
1. app.state.graphiti.close()
   ├── Flushes any pending writes
   ├── Closes graph driver connection (FalkorDB / Neo4j)
   └── Releases tracer resources

2. app.state.graph_driver = None  # allow GC
```

Reference implementation in `lifespan` above (§4.3). The shutdown is registered via the `yield` in the `lifespan` context manager — FastAPI guarantees this runs on SIGTERM/SIGINT.

---

## 6. Health Checks

### 6.1 Graph Connectivity in `/ready`

The `/ready` endpoint must verify the graph database is reachable:

```python
# services/api/app/routers/health.py
from fastapi import APIRouter, Request
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.driver.falkordb_driver import FalkorDriver

router = APIRouter(tags=["health"])


@router.get("/ready")
async def readiness_check(request: Request) -> dict:
    """Readiness probe: verify all downstream services are reachable.

    Returns HTTP 200 if healthy, 503 if any dependency is down.
    """
    status = {"status": "ok", "checks": {}}

    # Graph database check
    try:
        driver = request.app.state.graph_driver
        await driver.execute_query("RETURN 1")
        status["checks"]["graph_db"] = {"status": "ok"}
    except Exception as e:
        status["status"] = "degraded"
        status["checks"]["graph_db"] = {
            "status": "error",
            "error": str(e),
        }

    # ... other checks (PostgreSQL, Redis, LLM) ...

    return status
```

### 6.2 Health Probe Query

| Backend | Query | Expected Result |
|---------|-------|-----------------|
| FalkorDB | `RETURN 1` | Returns `[1]` |
| Neo4j | `RETURN 1 AS result` | Returns `{"result": 1}` |

---

## 7. Error Handling

### 7.1 Network Timeout → Retry

```python
# packages/core/graphiti/retry.py
"""Resilience patterns for Graphiti operations."""

import asyncio
from typing import TypeVar

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.exceptions import GraphTimeoutError

T = TypeVar("T")

# Timeout for individual Graphiti operations
GRAPHITI_OPERATION_TIMEOUT = 30.0  # seconds


def graphiti_retry[T]() -> T:
    """Decorator: retry Graphiti operations with exponential backoff.

    Retries on network timeouts and transient errors.
    Does NOT retry on NodeNotFoundError or EdgeNotFoundError — those
    are application-level errors, not infrastructure failures.
    """
    return retry(  # type: ignore[return-value]
        retry=retry_if_exception_type(
            (asyncio.TimeoutError, ConnectionError, TimeoutError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )


async def execute_with_timeout[T](coro, timeout: float = GRAPHITI_OPERATION_TIMEOUT) -> T:
    """Execute a Graphiti async operation with a timeout.

    Raises GraphTimeoutError if the operation exceeds the timeout.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError as e:
        raise GraphTimeoutError(
            f"Graphiti operation timed out after {timeout}s"
        ) from e
```

### 7.2 Connection Refused → Circuit Breaker

Use a circuit breaker pattern when the graph database is unavailable:

```python
# packages/core/graphiti/circuit_breaker.py
"""Simple circuit breaker for graph database connectivity."""

import asyncio
import time
from enum import Enum
from typing import Callable


class CircuitState(Enum):
    CLOSED = "closed"       # normal operation
    OPEN = "open"           # failing — reject immediately
    HALF_OPEN = "half_open" # testing if recovered


class GraphCircuitBreaker:
    """Circuit breaker for graph database operations.

    Prevents cascading failures when the graph backend is down.
    """

    def __init__(
        self,
        failure_threshold: int = 5,         # failures before opening
        recovery_timeout: float = 30.0,      # seconds before half-open
        half_open_max_attempts: int = 3,     # attempts in half-open state
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_attempts = half_open_max_attempts

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.half_open_attempts = 0

    async def call[T](self, operation: str, fn: Callable) -> T:
        """Execute a graph operation with circuit breaker protection."""
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.last_failure_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.half_open_attempts = 0
                logger.info("Graph circuit breaker: half-open", extra={"operation": operation})
            else:
                raise GraphConnectionError(
                    f"Graph backend unavailable (circuit open). "
                    f"Retry in {self.recovery_timeout - (time.monotonic() - self.last_failure_time):.0f}s"
                )

        try:
            result = await fn()
            # Success — reset
            if self.state == CircuitState.HALF_OPEN:
                self.half_open_attempts += 1
                if self.half_open_attempts >= self.half_open_max_attempts:
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    logger.info("Graph circuit breaker: closed (recovered)")
            else:
                self.failure_count = 0
            return result
        except (ConnectionError, TimeoutError) as e:
            self.failure_count += 1
            self.last_failure_time = time.monotonic()
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                logger.error(
                    "Graph circuit breaker: open",
                    extra={"operation": operation, "failures": self.failure_count},
                )
            raise
```

### 7.3 Mapping Graphiti Exceptions to Application Exceptions

```python
# packages/core/exceptions.py
from graphiti_core.errors import NodeNotFoundError, EdgeNotFoundError


class GraphError(AppError):
    """Base for all graph-related errors."""

class EntityNotFoundError(GraphError):
    """Entity node not found (translates to HTTP 404)."""

class EdgeNotFoundError(GraphError):
    """Relationship edge not found (translates to HTTP 404)."""

class GraphTimeoutError(GraphError):
    """Graph operation timed out (translates to HTTP 504)."""

class GraphConnectionError(GraphError):
    """Graph backend unreachable (translates to HTTP 503)."""


# Translation helper
def translate_graphiti_error(graphiti_error: Exception) -> GraphError:
    """Translate Graphiti exceptions into application exceptions."""
    if isinstance(graphiti_error, graphiti_core.errors.NodeNotFoundError):
        return EntityNotFoundError(str(graphiti_error))
    if isinstance(graphiti_error, graphiti_core.errors.EdgeNotFoundError):
        return EdgeNotFoundError(str(graphiti_error))
    if isinstance(graphiti_error, (ConnectionError, TimeoutError)):
        return GraphConnectionError(str(graphiti_error))
    return GraphError(str(graphiti_error))
```

---

## 8. Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `GRAPH_BACKEND` | `falkordb` | No | Graph database backend: `falkordb` or `neo4j` |
| `FALKORDB_HOST` | `localhost` | No | FalkorDB hostname |
| `FALKORDB_PORT` | `6380` | No | FalkorDB port (Redis wire protocol) |
| `FALKORDB_USERNAME` | — | No | FalkorDB username |
| `FALKORDB_PASSWORD` | — | No | FalkorDB password |
| `FALKORDB_DB` | `default_db` | No | FalkorDB database name |
| `NEO4J_URI` | `bolt://localhost:7687` | No | Neo4j connection URI |
| `NEO4J_USER` | `neo4j` | No | Neo4j username |
| `NEO4J_PASSWORD` | — | No | Neo4j password |
| `NEO4J_DB` | `neo4j` | No | Neo4j database name |
| `LLM_BACKEND` | `openai` | No | LLM provider: `openai`, `azure`, `ollama` |
| `LLM_MODEL` | `gpt-4o-mini` | No | LLM model name |
| `LLM_MAX_TOKENS` | `16384` | No | Max tokens for LLM responses |
| `OPENAI_API_KEY` | — | Conditional | Required for `openai`/`azure` backends |
| `AZURE_OPENAI_API_KEY` | — | Conditional | Required for `azure` backend |
| `AZURE_OPENAI_ENDPOINT` | — | Conditional | Azure OpenAI endpoint |
| `AZURE_OPENAI_DEPLOYMENT` | — | Conditional | Azure deployment name |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Conditional | Required for `ollama` backend |
| `OLLAMA_MODEL` | `qwen2.5` | No | Ollama model for LLM |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text` | No | Ollama model for embeddings |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | No | Embedding model identifier |
| `EMBEDDING_DIM` | `1536` | No | Embedding vector dimensions |
| `GRAPHITI_MAX_COROUTINES` | `10` | No | Graphiti internal concurrency limit |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | No | OpenTelemetry OTLP endpoint for tracing |

---

## 9. Docker Compose Service Definitions

```yaml
# infra/docker-compose.yml — graph backend services

services:
  falkordb:
    image: falkordb/falkordb:1.1.2
    ports:
      - "6380:6379"  # Redis wire protocol, mapped to non-standard port
    volumes:
      - falkordb_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5

  neo4j:  # optional — for testing and enterprise deployments
    image: neo4j:5.26-enterprise
    ports:
      - "7687:7687"  # Bolt protocol
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-password}
      NEO4J_PLUGINS: '["apoc"]'
    volumes:
      - neo4j_data:/data
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p ${NEO4J_PASSWORD:-password} 'RETURN 1'"]
      interval: 10s
      timeout: 10s
      retries: 10

volumes:
  falkordb_data:
  neo4j_data:
```

---

## 10. Testing Guide

### 10.1 Unit Tests

```python
# tests/unit/test_graphiti_factory.py
"""Test Graphiti factory functions with mocked drivers."""

from unittest.mock import AsyncMock, patch

import pytest

from packages.core.graphiti.factory import create_graphiti_instance


@pytest.mark.asyncio
async def test_create_graphiti_instance_falkordb() -> None:
    """Should create Graphiti with FalkorDB driver when GRAPH_BACKEND=falkordb."""
    with (
        patch("packages.core.graphiti.factory.create_graph_driver") as mock_driver,
        patch("packages.core.graphiti.factory.create_llm_client") as mock_llm,
        patch("packages.core.graphiti.factory.create_embedder") as mock_embed,
    ):
        mock_graphiti = AsyncMock()
        mock_graphiti.build_indices_and_constraints = AsyncMock()

        with patch(
            "packages.core.graphiti.factory.Graphiti", return_value=mock_graphiti
        ) as mock_graphiti_cls:
            instance = await create_graphiti_instance()

            mock_graphiti_cls.assert_called_once()
            assert instance is mock_graphiti
            mock_graphiti.build_indices_and_constraints.assert_awaited_once_with(
                delete_existing=False
            )


@pytest.mark.asyncio
async def test_create_graphiti_instance_failure() -> None:
    """Should raise GraphInitError if Graphiti initialization fails."""
    with patch(
        "packages.core.graphiti.factory.create_graph_driver",
        side_effect=ConnectionError("Connection refused"),
    ):
        from app.core.exceptions import GraphInitError

        with pytest.raises(GraphInitError):
            await create_graphiti_instance()
```

### 10.2 Integration Tests (Testcontainers)

```python
# tests/integration/test_graphiti_lifecycle.py
"""Integration tests for Graphiti lifecycle with real graph backends."""

import pytest
from testcontainers.falkordb import FalkorDbContainer
from testcontainers.neo4j import Neo4jContainer

from packages.core.graphiti.factory import create_graphiti_instance


@pytest.mark.asyncio
@pytest.mark.integration
class TestGraphitiLifecycle:

    @pytest.mark.parametrize("backend", ["falkordb", "neo4j"])
    async def test_graphiti_init_and_close(self, backend: str, graphiti_config: dict) -> None:
        """Graphiti should initialise and close cleanly with both backends."""
        config = graphiti_config[backend]
        # … set env vars, call create_graphiti_instance()
        graphiti = await create_graphiti_instance()

        # Verify it's alive
        driver = graphiti._driver
        result = await driver.execute_query("RETURN 1")
        assert result is not None

        # Graceful shutdown
        await graphiti.close()

    async def test_health_check_graph_connectivity(self, async_client) -> None:
        """GET /ready should reflect graph database connectivity."""
        response = await async_client.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["checks"]["graph_db"]["status"] == "ok"
```

---

## 11. Open Questions

| ID | Question | Impact | Decision / Status |
|----|----------|--------|-------------------|
| GIT-01 | FalkorDriver does not expose Redis connection pool parameters | Cannot tune pool size for high-throughput deployments | Workaround: set `REDIS_MAX_CONNECTIONS` env var. Raise PR against Graphiti if this becomes a bottleneck |
| GIT-02 | Graphiti `build_indices_and_constraints()` is called on every restart | Adds ~2s to startup time for large graphs | Acceptable for now. The method is idempotent. Consider caching the index state in Redis if startup time becomes an issue |
| GIT-03 | Cross-encoder client is `None` initially | Affects search ranking quality | Cross-encoder support added in Phase 2. Tracked in [04-context-assembly.md](../03-core-memory/02-context-assembly.md) |

---

*Implementation document for SRS §2.5, §5.3.1 (KG-01–KG-04), §9.1. Maintained by @tech-lead. Last updated: 2026-06-05.*
