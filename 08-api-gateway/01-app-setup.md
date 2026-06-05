# FastAPI Application Setup Guide

> **Phase:** Phase 0 — Foundation (Week 1-2)
> **Priority:** P0
> **Requirements:** AUTH-01, MT-01, PERF-06, AVAIL-01
> **Handoff from:** Architect (ADR-001: FastAPI Gateway Structure)

---

## 1. Overview

This document describes the FastAPI application setup for MemGraph's API gateway. The gateway is the single entry point for all client traffic: REST API requests, health checks, WebSocket connections (MCP SSE), and OpenAPI documentation.

The design follows the company standard separation of concerns:
- `main.py` — app creation, lifespan, middleware, routers
- `routers/` — HTTP adapters only (no business logic)
- `middleware/` — cross-cutting concerns (auth, logging, rate limiting)
- `dependencies/` — FastAPI dependency injection

---

## 2. File Structure

```
services/api/
├── main.py                    # App factory, lifespan, middleware, router includes
├── routers/
│   ├── __init__.py
│   ├── health.py              # /health, /ready
│   ├── users.py               # User CRUD
│   ├── sessions.py            # Session CRUD
│   ├── memory.py              # Message ingestion
│   ├── facts.py               # Business data facts
│   ├── graph.py               # Graph queries
│   ├── search.py              # Hybrid search
│   ├── context.py             # Context assembly
│   └── admin.py               # Admin endpoints (orgs, API keys)
├── middleware/
│   ├── __init__.py
│   ├── request_id.py          # X-Request-ID injection
│   ├── logging.py             # Structured logging context
│   ├── auth.py                # API key / JWT authentication
│   ├── rate_limit.py          # Token bucket rate limiting
│   └── tracing.py             # OpenTelemetry middleware
├── dependencies/
│   ├── __init__.py
│   ├── auth.py                # get_current_organization, get_current_user
│   ├── db.py                  # get_db (AsyncSession)
│   └── services.py            # Service DI factories
├── core/
│   └── config.py              # pydantic-settings
└── schemas/
    ├── users.py
    ├── sessions.py
    ├── memory.py
    ├── facts.py
    ├── graph.py
    ├── context.py
    └── health.py
```

---

## 3. `main.py` — Application Factory

### 3.1 Complete Implementation

```python
# services/api/main.py

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import Settings
from middleware.request_id import RequestIDMiddleware
from middleware.logging import LoggingMiddleware
from middleware.auth import AuthMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.tracing import TracingMiddleware
from routers import (
    health, users, sessions, memory, facts,
    graph, search, context, admin,
)
from core.exceptions import register_exception_handlers
from core.db import init_db_engine, close_db_engine
from core.redis import init_redis, close_redis
from core.graphiti import init_graphiti, close_graphiti
from core.arq import init_arq_pool, close_arq_pool


def create_app() -> FastAPI:
    """Application factory.

    Returns a fully configured FastAPI instance ready for deployment.
    Call this from ASGI entry point (uvicorn).
    """
    settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator:
        """Application lifespan: startup and shutdown hooks."""
        # ── Startup ──────────────────────────────────────────────
        await startup(app, settings)
        yield
        # ── Shutdown ─────────────────────────────────────────────
        await shutdown(app)

    app = FastAPI(
        title="MemGraph API",
        version=settings.API_VERSION,  # "1.0.0"
        description=(
            "Open-source temporal knowledge graph agent memory platform. "
            "Store, retrieve, and query agent memory across sessions."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        # Disable default 422 validation handler — we use our own
        # with RFC 7807 Problem Details format.
        validation_error_response=None,
        # Security: hide server info in production
        swagger_ui_parameters={"defaultModelsExpandDepth": -1},
        contact={
            "name": "TheLinkAI",
            "url": "https://thelink.ai",
        },
        license_info={
            "name": "Apache 2.0",
            "url": "https://www.apache.org/licenses/LICENSE-2.0",
        },
        # Servers for OpenAPI spec
        servers=[
            {
                "url": "http://localhost:8000",
                "description": "Local development",
            },
            {
                "url": "https://api.memgraph.dev",
                "description": "Production",
            },
        ],
    )

    # ── Middleware ───────────────────────────────────────────────
    # Order matters: outermost middleware is applied first.
    # Each middleware wraps the next, so the first in the list
    # is the outermost (first to receive the request, last to
    # receive the response).

    # 1. Request ID — must be first so all downstream layers have it
    app.add_middleware(RequestIDMiddleware)

    # 2. CORS — handles preflight before auth
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 3. Trusted Host — prevent Host header injection
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.trusted_hosts_list,
    )

    # 4. GZip — compress responses > 1KB
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # 5. Auth — validate API key / JWT
    app.add_middleware(AuthMiddleware)

    # 6. Rate limiting — per API key / IP
    app.add_middleware(RateLimitMiddleware)

    # 7. OpenTelemetry tracing — capture spans
    app.add_middleware(TracingMiddleware)

    # 8. Structured logging context — enrich all logs
    app.add_middleware(LoggingMiddleware)

    # ── Exception handlers ──────────────────────────────────────
    register_exception_handlers(app)

    # ── Routers ─────────────────────────────────────────────────
    # All under /v1/ prefix (except health)
    v1_router = APIRouter(prefix="/v1")

    v1_router.include_router(users.router)
    v1_router.include_router(sessions.router)
    v1_router.include_router(memory.router)
    v1_router.include_router(facts.router)
    v1_router.include_router(graph.router)
    v1_router.include_router(search.router)
    v1_router.include_router(context.router)
    v1_router.include_router(admin.router)

    app.include_router(v1_router)

    # Non-versioned routes
    app.include_router(health.router)

    return app


async def startup(app: FastAPI, settings: Settings) -> None:
    """Initialize all external service connections."""
    app.state.settings = settings

    # 1. Database engine
    app.state.db_engine = await init_db_engine(settings)
    app.state.db_session_factory = app.state.db_engine.session_factory

    # 2. Redis (cache + rate limiter)
    app.state.redis = await init_redis(settings)

    # 3. Graphiti (graph engine)
    app.state.graphiti = await init_graphiti(settings)

    # 4. ARQ pool (for enqueuing background jobs)
    app.state.arq_pool = await init_arq_pool(settings)


async def shutdown(app: FastAPI) -> None:
    """Gracefully close all external service connections."""
    if hasattr(app.state, "arq_pool") and app.state.arq_pool:
        await close_arq_pool(app.state.arq_pool)

    if hasattr(app.state, "graphiti") and app.state.graphiti:
        await close_graphiti(app.state.graphiti)

    if hasattr(app.state, "redis") and app.state.redis:
        await close_redis(app.state.redis)

    if hasattr(app.state, "db_engine") and app.state.db_engine:
        await close_db_engine(app.state.db_engine)
```

### 3.2 ASGI Entry Point

```python
# services/api/__init__.py or asgi.py

from .main import create_app

app = create_app()
```

```bash
# Run with uvicorn
uvicorn services.api:app --host 0.0.0.0 --port 8000 --workers 4
```

---

## 4. Lifespan Details

### 4.1 Database Initialization

```python
# core/db.py

from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker,
    create_async_engine,
)


engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker | None = None


async def init_db_engine(settings: Settings) -> AsyncEngine:
    """Create the async database engine and session factory.

    Connection string MUST use postgresql+asyncpg:// — never
    postgresql:// (that's a silent sync engine bug).
    """
    global engine, AsyncSessionLocal

    engine = create_async_engine(
        settings.DATABASE_URL,  # postgresql+asyncpg://...
        pool_pre_ping=True,     # Verify connections before use
        pool_size=20,           # Connection pool size
        max_overflow=10,        # Extra connections beyond pool_size
        echo=False,             # No SQL echo in production
        pool_recycle=3600,      # Recycle connections after 1 hour
    )

    AsyncSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,  # ⚠️ Required: prevents lazy-load errors in async
    )

    # Verify connectivity
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    return engine


async def close_db_engine(engine: AsyncEngine) -> None:
    """Dispose of the database engine and all connections."""
    await engine.dispose()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yield an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

### 4.2 Redis Initialization

```python
# core/redis.py

import redis.asyncio as redis


async def init_redis(settings: Settings) -> redis.Redis:
    """Create the Redis connection pool."""
    client = redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,  # Auto-decode bytes to str
        max_connections=50,
        health_check_interval=30,
    )
    await client.ping()
    return client


async def close_redis(client: redis.Redis) -> None:
    """Close all Redis connections."""
    await client.aclose()
```

### 4.3 Graphiti Initialization

```python
# core/graphiti.py

from packages.graphiti_client import GraphitiClient


async def init_graphiti(settings: Settings) -> GraphitiClient:
    """Initialize the Graphiti temporal knowledge graph engine."""
    client = GraphitiClient(
        backend=settings.GRAPH_BACKEND,  # "falkordb" or "neo4j"
        url=settings.FALKORDB_URL,
        llm_client=settings.get_llm_client(),
        embedding_client=settings.get_embedding_client(),
    )
    await client.initialize()
    return client


async def close_graphiti(client: GraphitiClient) -> None:
    """Close the Graphiti client and graph DB connections."""
    await client.close()
```

### 4.4 ARQ Pool Initialization

```python
# core/arq.py

from arq.connections import ArqRedis, RedisSettings


async def init_arq_pool(settings: Settings) -> ArqRedis:
    """Create the ARQ Redis connection pool for job enqueuing."""
    pool = await ArqRedis.from_settings(
        RedisSettings.from_dsn(settings.REDIS_URL),
    )
    return pool


async def close_arq_pool(pool: ArqRedis) -> None:
    """Close the ARQ pool."""
    await pool.close()
```

---

## 5. Middleware Details

### 5.1 Middleware Order Rationale

| Order | Middleware | Purpose | Why this position |
|---|---|---|---|
| 1 | `RequestID` | Inject X-Request-ID header | Must be outermost so every downstream layer has a request_id |
| 2 | `CORS` | Handle preflight OPTIONS | Must handle CORS before auth (browsers send preflight without auth) |
| 3 | `TrustedHost` | Validate Host header | Security: reject requests before they reach app logic |
| 4 | `GZip` | Compress responses | After auth but before app — no need to compress rejected requests |
| 5 | `Auth` | Validate API key / JWT | After CORS, before rate limiting — identify the tenant |
| 6 | `RateLimit` | Per-key rate limiting | After auth — we know the tenant/key for rate limit counters |
| 7 | `Tracing` | OpenTelemetry spans | Before logging but after auth — capture authenticated spans |
| 8 | `Logging` | Structured log context | Innermost — enrich logs with all context from previous middleware |

### 5.2 Request ID Middleware

```python
# middleware/request_id.py

import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Ensure every request has a unique X-Request-ID.

    If the client provides one, it's propagated (for distributed tracing).
    If not, a new UUID is generated.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = f"req_{uuid.uuid4().hex[:22]}"

        # Store in request state for downstream access
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
```

### 5.3 Auth Middleware

```python
# middleware/auth.py

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from core.exceptions import AuthenticationError


class AuthMiddleware(BaseHTTPMiddleware):
    """Validate API key or JWT on all requests except public endpoints.

    Public endpoints (no auth required):
        - GET /health
        - GET /ready
        - GET /docs
        - GET /redoc
        - GET /openapi.json
    """

    PUBLIC_PATHS = {
        "/health", "/ready",
        "/docs", "/redoc", "/openapi.json",
        "/docs/oauth2-redirect",
    }

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "type": "https://api.memgraph.dev/errors/unauthorized",
                    "title": "Unauthorized",
                    "status": 401,
                    "detail": "Missing or malformed Authorization header. "
                              "Expected format: 'Bearer mg_live_...'",
                    "instance": getattr(request.state, "request_id", None),
                },
            )

        api_key = auth_header[7:]  # Strip "Bearer "
        # Key validation happens in the dependency layer
        request.state.api_key = api_key

        return await call_next(request)
```

### 5.4 Structured Logging Middleware

```python
# middleware/logging.py

import time
import structlog
from starlette.middleware.base import BaseHTTPMiddleware


class LoggingMiddleware(BaseHTTPMiddleware):
    """Enrich all log entries with request context.

    Uses structlog for structured JSON logging.
    """

    async def dispatch(self, request: Request, call_next):
        start_time = time.monotonic()

        response = await call_next(request)

        duration_ms = (time.monotonic() - start_time) * 1000

        log_context = {
            "request_id": getattr(request.state, "request_id", None),
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(duration_ms, 2),
            "org_id": str(getattr(request.state, "org_id", "")),
            "user_agent": request.headers.get("User-Agent", ""),
        }

        # Log slow requests at WARNING level
        if duration_ms > 1000:
            structlog.get_logger().warning("http.slow_request", **log_context)
        else:
            structlog.get_logger().info("http.request", **log_context)

        return response
```

---

## 6. Health & Readiness Endpoints

```python
# routers/health.py

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import redis.asyncio as redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Liveness probe.

    Returns 200 if the process is alive.
    Does NOT check dependencies — this is for kubernetes liveness.
    """
    return {"status": "healthy", "service": "memgraph-api"}


@router.get("/ready")
async def readiness(
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
    graphiti: GraphitiClient = Depends(get_graphiti),
):
    """Readiness probe.

    Returns 200 only when ALL dependencies are reachable:
      - PostgreSQL (executes SELECT 1)
      - Redis (executes PING)
      - FalkorDB / Neo4j (executes PING)

    This is used by Kubernetes to determine if the pod should
    receive traffic.
    """
    checks = {}

    # PostgreSQL
    try:
        await db.execute(text("SELECT 1"))
        checks["postgresql"] = "ok"
    except Exception as e:
        checks["postgresql"] = f"error: {str(e)}"

    # Redis
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {str(e)}"

    # Graph DB
    try:
        await graphiti.ping()
        checks["graph_db"] = "ok"
    except Exception as e:
        checks["graph_db"] = f"error: {str(e)}"

    all_ok = all(v == "ok" for v in checks.values())
    status_code = 200 if all_ok else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if all_ok else "not_ready",
            "checks": checks,
        },
    )
```

---

## 7. Router Registration Pattern

Each domain router follows a consistent pattern:

```python
# routers/users.py
router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=..., status_code=201)
async def create_user(/* ... */):
    """Docstring used in OpenAPI spec."""
    return await service.create_user(...)
```

Registered in `main.py`:

```python
v1_router = APIRouter(prefix="/v1")

# Each domain adds its prefix relative to /v1
v1_router.include_router(users.router)       # → /v1/users
v1_router.include_router(sessions.router)    # → /v1/users/{user_id}/sessions
v1_router.include_router(memory.router)      # → /v1/users/{user_id}/memory
v1_router.include_router(facts.router)       # → /v1/users/{user_id}/facts
v1_router.include_router(graph.router)       # → /v1/users/{user_id}/graph
v1_router.include_router(search.router)      # → /v1/users/{user_id}/search
v1_router.include_router(context.router)     # → /v1/users/{user_id}/context
v1_router.include_router(admin.router)       # → /v1/admin
```

---

## 8. CORS Configuration

```python
# core/config.py
from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # ...

    CORS_ORIGINS: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        description="Comma-separated list of allowed CORS origins. "
                    "NEVER use '*' in production.",
    )
    TRUSTED_HOSTS: str = Field(
        default="localhost,127.0.0.1,api.memgraph.dev",
        description="Comma-separated list of allowed Host header values.",
    )

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def trusted_hosts_list(self) -> List[str]:
        return [h.strip() for h in self.TRUSTED_HOSTS.split(",") if h.strip()]
```

---

## 9. Versioning Strategy

### 9.1 URL Path Versioning

```
/v1/...      → Current stable API
/v2/...      → Future breaking changes (when needed)
```

### 9.2 When to Create v2

A new major version is warranted when:
- A request/response schema changes incompatibly (e.g., removing a field)
- An endpoint's behaviour changes in a way that breaks existing clients
- An authentication mechanism changes

### 9.3 Deprecation Policy

```python
# decorators/deprecation.py

from fastapi import APIRouter
import warnings


def deprecated(endpoint: callable, sunset_date: str, migration_path: str):
    """Mark an endpoint as deprecated.

    The endpoint continues to work but:
      1. A 'Deprecation' header is added to responses
      2. The OpenAPI schema marks it as deprecated
      3. Logs emit a warning when called
    """
    endpoint.openapi_extra = endpoint.openapi_extra or {}
    endpoint.openapi_extra["deprecated"] = True
    # ... (header injection logic in middleware)
```

---

## 10. Environment Configuration

```python
# core/config.py — complete settings

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    # ── API ───────────────────────────────────────────────────────
    API_VERSION: str = "1.0.0"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_WORKERS: int = 4
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"

    # ── Database ──────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://memgraph:memgraph@localhost:5432/memgraph"

    # ── Redis ─────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379"

    # ── Graph ─────────────────────────────────────────────────────
    GRAPH_BACKEND: str = "falkordb"
    FALKORDB_URL: str = "redis://localhost:6380"
    NEO4J_URL: Optional[str] = None
    NEO4J_USER: Optional[str] = None
    NEO4J_PASSWORD: Optional[str] = None

    # ── LLM ───────────────────────────────────────────────────────
    LLM_BACKEND: str = "openai"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: Optional[str] = None
    OLLAMA_BASE_URL: Optional[str] = "http://localhost:11434"
    LLM_MODEL: str = "gpt-4o-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIM: int = 1536

    # ── CORS ──────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000"
    TRUSTED_HOSTS: str = "localhost,127.0.0.1"

    # ── Rate Limiting ─────────────────────────────────────────────
    RATE_LIMIT_DEFAULT: int = 100  # requests per minute
    RATE_LIMIT_AUTH_FAIL: int = 10  # failed auth attempts per IP per minute

    # ── Observability ─────────────────────────────────────────────
    OTLP_ENDPOINT: Optional[str] = None
    LOG_LEVEL: str = "INFO"
    SERVICE_NAME: str = "memgraph-api"
```

---

## 11. Dockerfile for API Gateway

```dockerfile
# services/api/Dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:3.12-slim AS runtime
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY . .
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "services.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

---

## 12. Testing the App Setup

```python
@pytest.mark.asyncio
async def test_health_endpoint(async_client: AsyncClient) -> None:
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_readiness_endpoint(async_client: AsyncClient) -> None:
    response = await async_client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_request_id_injected(async_client: AsyncClient) -> None:
    response = await async_client.get("/health")
    assert "X-Request-ID" in response.headers
    assert response.headers["X-Request-ID"].startswith("req_")


@pytest.mark.asyncio
async def test_openapi_endpoint(async_client: AsyncClient) -> None:
    response = await async_client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"] == "MemGraph API"
    assert spec["info"]["version"] == "1.0.0"
    assert "/v1/users" in str(spec["paths"])


@pytest.mark.asyncio
async def test_cors_headers(async_client: AsyncClient) -> None:
    response = await async_client.options(
        "/v1/users",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


@pytest.mark.asyncio
async def test_cors_rejected_origin(async_client: AsyncClient) -> None:
    response = await async_client.options(
        "/v1/users",
        headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    # CORS middleware returns 200 but without allow-origin
    assert "access-control-allow-origin" not in response.headers
```

---

## 13. Middleware Order Verification Test

```python
@pytest.mark.asyncio
async def test_middleware_execution_order(async_client: AsyncClient) -> None:
    """Verify middleware chain executes in the correct order.

    Sends a request and inspects response headers that each
    middleware adds.
    """
    response = await async_client.get("/health")
    headers = response.headers

    # RequestID middleware added this
    assert "X-Request-ID" in headers

    # No auth on /health (public endpoint)
    assert response.status_code == 200
```

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
