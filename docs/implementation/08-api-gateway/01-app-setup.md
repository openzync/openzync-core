# FastAPI Application Setup Guide

> **Phase:** Phase 0 — Foundation (Week 1–2)
> **Priority:** P0
> **Requirements:** AUTH-01, MT-01, PERF-06, AVAIL-01, AVAIL-02
> **Handoff from:** Architect (ADR-001: FastAPI Gateway Structure)
> **SRS Reference:** §4 System Architecture Overview, §8 API Specification, §9 Technology Stack

---

## 1. Overview

This document describes the FastAPI application setup for the OpenZep API gateway. The gateway is the single entry point for all client traffic: REST API requests, health checks, WebSocket connections (MCP SSE), and OpenAPI documentation.

The gateway follows the company-standard separation of concerns:
- `main.py` — Application factory, lifespan hooks, middleware registration, router includes
- `routers/` — HTTP adapters only (no business logic)
- `middleware/` — Cross-cutting concerns (request ID, CORS, auth, rate limiting, tracing, logging)
- `dependencies/` — FastAPI dependency injection (DB session, auth, services)
- `core/` — Configuration, DB engine, Redis client, Graphiti client, ARQ pool
- `schemas/` — Pydantic request/response models

### 1.1 Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| ASGI framework | FastAPI | Async-native, auto-OpenAPI, team standard at TheLinkAI |
| Lifespan pattern | `@asynccontextmanager` | Proper startup/shutdown hooks, no globals |
| Middleware order | RequestID → CORS → TrustedHost → GZip → Auth → RateLimit → Tracing → Logging | Each layer depends on the previous (see §5.1) |
| API versioning | URL prefix `/v1/` | Simple, unambiguous, widely adopted |
| Health checks | `/health` (liveness) + `/ready` (readiness) | Kubernetes-native: separate liveness and readiness |
| Auth handling | Middleware + dependency injection | Middleware validates token presence; DI checks scopes |

---

## 2. File Structure

```
services/api/
├── main.py                      # App factory, lifespan, middleware, router includes
├── asgi.py                      # ASGI entry point
├── routers/
│   ├── __init__.py
│   ├── health.py                # GET /health, GET /ready
│   ├── users.py                 # User CRUD endpoints
│   ├── sessions.py              # Session CRUD endpoints
│   ├── memory.py                # Message ingestion
│   ├── facts.py                 # Business data facts
│   ├── graph.py                 # Graph query endpoints
│   ├── search.py                # Hybrid search
│   ├── context.py               # Context assembly
│   └── admin.py                 # Organization and API key management
├── middleware/
│   ├── __init__.py
│   ├── request_id.py            # X-Request-ID injection
│   ├── logging.py               # Structured logging context
│   ├── auth.py                  # API key / JWT authentication
│   ├── rate_limit.py            # Token bucket rate limiting
│   └── tracing.py               # OpenTelemetry tracing
├── dependencies/
│   ├── __init__.py
│   ├── auth.py                  # get_current_organization, get_current_user
│   ├── db.py                    # get_db (AsyncSession)
│   └── services.py              # Service DI factories
├── core/
│   ├── __init__.py
│   ├── config.py                # pydantic-settings BaseSettings
│   ├── db.py                    # AsyncEngine + AsyncSession factory
│   ├── redis.py                 # Redis connection pool
│   ├── graphiti.py              # Graphiti client init/close
│   ├── arq.py                   # ARQ pool init/close
│   └── exceptions.py            # Exception hierarchy + handlers
└── schemas/
    ├── __init__.py
    ├── common.py                # Shared schemas (PaginatedResponse, etc.)
    ├── users.py                 # CreateUserRequest, UserResponse, etc.
    ├── sessions.py              # Session schemas
    ├── memory.py                # Message ingestion schemas
    ├── facts.py                 # Fact triple schemas
    ├── graph.py                 # Graph node/edge schemas
    ├── context.py               # Context response schemas
    └── health.py                # Health check response schemas
```

---

## 3. `create_app()` Factory Function

### 3.1 Complete Implementation

```python
# services/api/main.py

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from core.config import Settings
from core.exceptions import register_exception_handlers
from core.db import init_db_engine, close_db_engine
from core.redis import init_redis, close_redis
from core.graphiti import init_graphiti, close_graphiti
from core.arq import init_arq_pool, close_arq_pool
from middleware.request_id import RequestIDMiddleware
from middleware.logging import LoggingMiddleware
from middleware.auth import AuthMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.tracing import TracingMiddleware
from routers import (
    health,
    users,
    sessions,
    memory,
    facts,
    graph,
    search,
    context,
    admin,
)


def create_app() -> FastAPI:
    """Application factory for the OpenZep API gateway.

    Returns a fully configured FastAPI instance ready to serve traffic.
    Call this from the ASGI entry point (asgi.py).

    The factory pattern ensures:
    - Testability: each test creates a fresh app with overridden dependencies
    - Configuration isolation: settings are loaded once at creation time
    - Clean shutdown: lifespan hooks close connections in order
    """
    settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator:
        """Application lifespan: startup and shutdown hooks.

        Startup order:
            1. Load settings into app.state
            2. Initialize PostgreSQL async engine
            3. Initialize Redis connection pool
            4. Initialize Graphiti (FalkorDB/Neo4j)
            5. Initialize ARQ Redis pool for background jobs

        Shutdown order (reverse of startup):
            1. Close ARQ pool
            2. Close Graphiti client
            3. Close Redis connections
            4. Dispose DB engine
        """
        # ── Startup ────────────────────────────────────────────────────
        await _startup(app, settings)
        yield
        # ── Shutdown ───────────────────────────────────────────────────
        await _shutdown(app)

    app = FastAPI(
        title="OpenZep API",
        version=settings.API_VERSION,
        summary="Open-source temporal knowledge graph agent memory platform.",
        description=(
            "OpenZep stores, retrieves, and queries LLM agent memory "
            "across sessions using a temporal knowledge graph engine. "
            "It provides hybrid retrieval (vector + BM25 + graph), "
            "async NLP enrichment, and multi-tenant data isolation."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        # Disable default 422 — we use our own RFC 7807 handler
        validation_error_response=None,
        swagger_ui_parameters={
            "defaultModelsExpandDepth": -1,
            "displayRequestDuration": True,
            "filter": True,
        },
        contact={
            "name": "TheLinkAI",
            "url": "https://thelink.ai",
            "email": "engineering@thelink.ai",
        },
        license_info={
            "name": "Apache 2.0",
            "identifier": "Apache-2.0",
            "url": "https://www.apache.org/licenses/LICENSE-2.0",
        },
        servers=[
            {
                "url": "http://localhost:8000",
                "description": "Local development",
            },
            {
                "url": "https://api.OpenZep.dev",
                "description": "Production",
            },
        ],
    )

    # ── Middleware ──────────────────────────────────────────────────────
    # Order matters: outermost middleware is registered first.
    # Each middleware wraps the next, so the first in the list
    # is the outermost (first to receive the request, last to receive the response).
    _register_middleware(app, settings)

    # ── Exception handlers ──────────────────────────────────────────────
    register_exception_handlers(app)

    # ── Routers ─────────────────────────────────────────────────────────
    _register_routers(app)

    return app


async def _startup(app: FastAPI, settings: Settings) -> None:
    """Initialize all external service connections on startup."""
    app.state.settings = settings

    # 1. Database engine — must succeed or app fails to start
    app.state.db_engine = await init_db_engine(settings)
    app.state.db_session_factory = app.state.db_engine.session_factory

    # 2. Redis — cache + rate limiter backing store
    app.state.redis = await init_redis(settings)

    # 3. Graphiti — temporal knowledge graph engine
    app.state.graphiti = await init_graphiti(settings)

    # 4. ARQ pool — for enqueuing background jobs from API layer
    app.state.arq_pool = await init_arq_pool(settings)


async def _shutdown(app: FastAPI) -> None:
    """Gracefully close all external service connections on shutdown.

    Uses hasattr guards so partial startup failures don't cause
    cascading errors during shutdown.
    """
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
# services/api/asgi.py

"""ASGI entry point for the OpenZep API gateway.

Usage:
    uvicorn services.api.asgi:app --host 0.0.0.0 --port 8000
"""

from services.api.main import create_app

app = create_app()
```

---

## 4. Lifespan Hook Details

### 4.1 Database Initialization (`core/db.py`)

```python
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker | None = None


async def init_db_engine(settings: Settings) -> AsyncEngine:
    """Create the async database engine and session factory.

    ⚠️ Connection string MUST use postgresql+asyncpg:// — never
    postgresql:// (that silently creates a sync engine).

    Pool configuration:
        - pool_pre_ping=True: verify connections are alive before use
        - pool_size=20: base pool size
        - max_overflow=10: extra connections beyond pool_size
        - pool_recycle=3600: recycle connections after 1 hour
          to avoid stale connection issues behind NAT/LB
    """
    global engine, AsyncSessionLocal

    engine = create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=10,
        echo=False,
        pool_recycle=3600,
    )

    AsyncSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,  # ⚠️ Required for async: prevents lazy-load errors
    )

    # Verify connectivity immediately
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    return engine


async def close_db_engine(engine: AsyncEngine) -> None:
    """Dispose of the database engine, closing all connections."""
    await engine.dispose()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yield an async database session.

    Commits on success, rolls back on exception.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

### 4.2 Redis Initialization (`core/redis.py`)

```python
import redis.asyncio as redis


async def init_redis(settings: Settings) -> redis.Redis:
    """Create the Redis connection pool.

    Configuration:
        - decode_responses=True: auto-decode bytes to str
        - max_connections=50: pool size
        - health_check_interval=30: verify connection health
    """
    client = redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        max_connections=50,
        health_check_interval=30,
        socket_keepalive=True,
    )
    await client.ping()
    return client


async def close_redis(client: redis.Redis) -> None:
    """Close all Redis connections."""
    await client.aclose()
```

### 4.3 Graphiti Initialization (`core/graphiti.py`)

```python
from packages.graphiti_client import GraphitiClient


async def init_graphiti(settings: Settings) -> GraphitiClient:
    """Initialize the Graphiti temporal knowledge graph engine.

    Supports pluggable backends (FalkorDB or Neo4j) and
    pluggable LLM/embedding clients (OpenAI, Azure, Ollama).
    """
    client = GraphitiClient(
        backend=settings.GRAPH_BACKEND,       # "falkordb" or "neo4j"
        url=settings.FALKORDB_URL,
        llm_client=settings.get_llm_client(),
        embedding_client=settings.get_embedding_client(),
    )
    await client.initialize()
    return client


async def close_graphiti(client: GraphitiClient) -> None:
    """Close the Graphiti client and all graph DB connections."""
    await client.close()
```

### 4.4 ARQ Pool Initialization (`core/arq.py`)

```python
from arq.connections import ArqRedis, RedisSettings


async def init_arq_pool(settings: Settings) -> ArqRedis:
    """Create the ARQ Redis connection pool for enqueuing background jobs.

    Used by API endpoints to dispatch async enrichment tasks
    (entity extraction, embedding, fact extraction).
    """
    pool = await ArqRedis.from_settings(
        RedisSettings.from_dsn(settings.REDIS_URL),
    )
    return pool


async def close_arq_pool(pool: ArqRedis) -> None:
    """Close the ARQ pool and underlying Redis connections."""
    await pool.close()
```

---

## 5. Middleware Registration and Order

### 5.1 Middleware Order Rationale

```python
def _register_middleware(app: FastAPI, settings: Settings) -> None:
    """Register all middleware in the correct execution order.

    Middleware order is critical for correctness:
    - The first middleware added is the outermost (last to wrap, first to execute)
    - Each middleware depends on context set by prior middleware

    ┌──────────────────────────────────────────────────────┐
    │  1. RequestIDMiddleware   (outermost)                 │
    │    │                                                   │
    │  2. CORSMiddleware         (handle preflight)          │
    │    │                                                   │
    │  3. TrustedHostMiddleware  (security)                  │
    │    │                                                   │
    │  4. GZipMiddleware         (compress responses)        │
    │    │                                                   │
    │  5. AuthMiddleware         (validate credentials)      │
    │    │                                                   │
    │  6. RateLimitMiddleware    (enforce limits)            │
    │    │                                                   │
    │  7. TracingMiddleware      (OpenTelemetry spans)       │
    │    │                                                   │
    │  8. LoggingMiddleware      (structured logs)           │
    │    │                                                   │
    │  (handler)                   (innermost)               │
    └──────────────────────────────────────────────────────┘
    """

    # 1. Request ID — outermost. Must be first so every downstream
    #    layer (including error handlers) has access to a request_id.
    app.add_middleware(RequestIDMiddleware)

    # 2. CORS — must be before auth. Browsers send CORS preflight
    #    (OPTIONS) without auth headers. Rejecting preflight at auth
    #    middleware would break legitimate cross-origin requests.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 3. Trusted Host — security. Reject requests with unexpected
    #    Host headers before any app logic runs. Prevents Host header
    #    injection attacks.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.trusted_hosts_list,
    )

    # 4. GZip — compress responses >= 1KB. Placed after auth so we
    #    don't waste CPU compressing rejected requests.
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # 5. Auth — validate API key / JWT. Placed after CORS (preflight
    #    doesn't carry auth) and before rate limiting (we need to
    #    know the identity for per-key limits).
    app.add_middleware(AuthMiddleware)

    # 6. Rate limiting — per API key / IP. Must be after auth so we
    #    have the API key to use as the rate limit key. Falls back
    #    to IP for unauthenticated requests on public endpoints.
    app.add_middleware(RateLimitMiddleware)

    # 7. OpenTelemetry tracing — capture request spans. After auth
    #    so spans carry authenticated user/org attributes.
    app.add_middleware(TracingMiddleware)

    # 8. Logging — innermost. Enriches log entries with all context
    #    set by previous middleware (request_id, org_id, duration).
    app.add_middleware(LoggingMiddleware)
```

### 5.2 Middleware Order Summary

| Order | Middleware | Purpose | Why This Position |
|---|---|---|---|
| 1 | `RequestID` | Inject X-Request-ID | Must be outermost so every downstream layer has a request_id |
| 2 | `CORS` | Handle preflight OPTIONS | Must handle CORS before auth (browsers send preflight without auth) |
| 3 | `TrustedHost` | Validate Host header | Security: reject requests before they reach app logic |
| 4 | `GZip` | Compress responses | After auth but before app — no need to compress rejected requests |
| 5 | `Auth` | Validate API key / JWT | After CORS, before rate limiting — identify the tenant |
| 6 | `RateLimit` | Per-key rate limiting | After auth — we know the tenant/key for rate limit counters |
| 7 | `Tracing` | OpenTelemetry spans | Before logging but after auth — capture authenticated spans |
| 8 | `Logging` | Structured log context | Innermost — enrich logs with all context from previous middleware |

### 5.3 Request ID Middleware

```python
# middleware/request_id.py

import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Ensure every request has a unique X-Request-ID.

    If the client provides one via header, it is propagated for
    distributed tracing. If not, a new prefixed UUID is generated.

    The request_id is stored in request.state so all downstream
    middleware, dependencies, and error handlers can access it.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = f"req_{uuid.uuid4().hex[:22]}"

        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
```

### 5.4 Auth Middleware

```python
# middleware/auth.py

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from core.exceptions import AuthenticationError


class AuthMiddleware(BaseHTTPMiddleware):
    """Validate API key or JWT on all requests except public endpoints.

    Public endpoints (no auth required):
        - GET /health          (liveness probe)
        - GET /ready           (readiness probe)
        - GET /docs            (Swagger UI)
        - GET /redoc           (ReDoc)
        - GET /openapi.json    (OpenAPI spec)

    Authentication flow:
        1. Check if path is public → skip
        2. Extract Bearer token from Authorization header
        3. Store raw token in request.state for dependency validation
        4. Detailed validation (key lookup, expiry, scopes) happens
           in the auth dependency layer, not here
    """

    PUBLIC_PATHS = frozenset({
        "/health", "/ready",
        "/docs", "/redoc", "/openapi.json",
        "/docs/oauth2-redirect",
    })

    async def dispatch(self, request: Request, call_next):
        # Allow public paths without authentication
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "type": "https://api.OpenZep.dev/errors/unauthorized",
                    "title": "Unauthorized",
                    "status": 401,
                    "detail": (
                        "Missing or malformed Authorization header. "
                        "Expected format: 'Bearer mg_live_...'"
                    ),
                    "instance": getattr(request.state, "request_id", None),
                },
            )

        api_key = auth_header.removeprefix("Bearer ")
        request.state.api_key = api_key

        return await call_next(request)
```

### 5.5 Logging Middleware

```python
# middleware/logging.py

import time
import structlog
from starlette.middleware.base import BaseHTTPMiddleware

logger = structlog.get_logger("OpenZep.api")


class LoggingMiddleware(BaseHTTPMiddleware):
    """Enrich all log entries with request context.

    Uses structlog for structured JSON logging. All logs include:
    - request_id, method, path, status_code, duration_ms
    - org_id (when authenticated)
    - user_agent

    Slow requests (> 1s) are logged at WARNING level for alerting.
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
            "user_agent": request.headers.get("User-Agent", "<unknown>"),
        }

        if duration_ms > 1000:
            logger.warning("http.slow_request", **log_context)
        elif response.status_code >= 500:
            logger.error("http.server_error", **log_context)
        elif response.status_code >= 400:
            logger.warning("http.client_error", **log_context)
        else:
            logger.info("http.request", **log_context)

        return response
```

---

## 6. Router Registration

### 6.1 All Routers Under `/v1/` Prefix

```python
def _register_routers(app: FastAPI) -> None:
    """Register all domain routers with the application.

    Domain routers are registered under the /v1/ URL prefix.
    Health/readiness endpoints are at the root (no version prefix).

    Each domain router uses its own prefix relative to /v1:
        /v1/users
        /v1/users/{user_id}/sessions
        /v1/users/{user_id}/memory
        /v1/users/{user_id}/facts
        /v1/users/{user_id}/graph/...
        /v1/users/{user_id}/search
        /v1/users/{user_id}/context
        /v1/admin/...
    """
    # Versioned routers (v1)
    v1_router = APIRouter(prefix="/v1")

    v1_router.include_router(users.router)        # → /v1/users
    v1_router.include_router(sessions.router)      # → /v1/users/{user_id}/sessions
    v1_router.include_router(memory.router)        # → /v1/users/{user_id}/memory
    v1_router.include_router(facts.router)         # → /v1/users/{user_id}/facts
    v1_router.include_router(graph.router)         # → /v1/users/{user_id}/graph
    v1_router.include_router(search.router)        # → /v1/users/{user_id}/search
    v1_router.include_router(context.router)       # → /v1/users/{user_id}/context
    v1_router.include_router(admin.router)         # → /v1/admin

    app.include_router(v1_router)

    # Non-versioned routes (health checks, docs)
    app.include_router(health.router)              # → /health, /ready
```

### 6.2 Router Pattern (Example: Users)

```python
# routers/users.py
from fastapi import APIRouter

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    request: CreateUserRequest,
    service: UserService = Depends(get_user_service),
    org: Organization = Depends(get_current_organization),
) -> UserResponse:
    """Create a new user within the authenticated organization."""
    return await service.create_user(organization_id=org.id, request=request)
```

---

## 7. Health and Readiness Endpoints

### 7.1 `/health` — Liveness Probe

Returns 200 if the process is alive. Does **not** check dependencies — this is for Kubernetes liveness (if the process is hung, the endpoint won't respond).

```python
# routers/health.py

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """Liveness probe.

    Returns 200 if the process is alive. Does NOT check database
    or Redis connectivity — that is the readiness probe's job.

    Kubernetes: if this endpoint fails, the pod is restarted.
    """
    return {
        "status": "healthy",
        "service": "OpenZep-api",
        "version": "1.0.0",
    }
```

### 7.2 `/ready` — Readiness Probe

Returns 200 only when **all** external dependencies are reachable. Used by Kubernetes to determine if the pod should receive traffic.

```python
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
      - FalkorDB / Neo4j (executes PING via Graphiti)

    Used by Kubernetes to determine if the pod should receive traffic.
    If dependencies are down, returns 503 and the pod is removed from
    the load balancer until it recovers.
    """
    checks = {}

    # PostgreSQL
    try:
        await db.execute(text("SELECT 1"))
        checks["postgresql"] = "ok"
    except Exception as e:
        checks["postgresql"] = f"error: {e}"

    # Redis
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    # Graph DB
    try:
        await graphiti.ping()
        checks["graph_db"] = "ok"
    except Exception as e:
        checks["graph_db"] = f"error: {e}"

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

## 8. CORS Configuration

### 8.1 Settings

```python
# core/config.py

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ...

    CORS_ORIGINS: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        description=(
            "Comma-separated list of allowed CORS origins. "
            "NEVER use '*' in production — it allows any website "
            "to make authenticated requests to this API."
        ),
    )
    TRUSTED_HOSTS: str = Field(
        default="localhost,127.0.0.1,api.OpenZep.dev",
        description=(
            "Comma-separated list of allowed Host header values. "
            "Prevents Host header injection attacks."
        ),
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def trusted_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.TRUSTED_HOSTS.split(",") if h.strip()]
```

### 8.2 Production Origin Allowlist

```bash
# .env (production)
CORS_ORIGINS=https://dashboard.OpenZep.dev,https://app.OpenZep.dev
TRUSTED_HOSTS=api.OpenZep.dev,dashboard.OpenZep.dev
```

### 8.3 Security Note

`allow_origins=["*"]` is **never** used in production. In development, the dashboard runs on `http://localhost:3000` and the API on `http://localhost:8000`, so the dev config is:

```
CORS_ORIGINS=http://localhost:3000,http://localhost:8000
```

---

## 9. API Versioning Strategy

### 9.1 URL Path Versioning

```
/v1/...      → Current stable API (breaking changes allowed only in new versions)
/v2/...      → Future breaking changes (when needed)
```

### 9.2 When to Create a New Version

A new major version (`/v2/`) is warranted when:

| Change Type | Example | Requires v2? |
|---|---|---|
| Adding a new endpoint | `POST /v1/users/{id}/export` | No — backward compatible |
| Adding an optional field to response | New `email_verified` field | No — old clients ignore |
| Adding a required field to request | New `email` becomes required | Yes — breaks old clients |
| Removing a field from response | Remove `name` from user response | Yes — breaks old clients |
| Changing response format | Change date format from ISO to epoch | Yes — breaks old clients |
| Changing auth mechanism | API key → OAuth2 | Yes — fundamental change |

### 9.3 Deprecation Policy

```python
# decorators/deprecation.py

from fastapi import APIRouter
import warnings


def deprecated(sunset_date: str, migration_path: str):
    """Decorator: mark an endpoint as deprecated.

    The endpoint continues to work but:
      1. Adds a 'Deprecation: true' and 'Sunset: <date>' header
      2. Marks it as deprecated in the OpenAPI schema
      3. Logs a warning when called
    """
    def decorator(endpoint):
        endpoint.openapi_extra = endpoint.openapi_extra or {}
        endpoint.openapi_extra["deprecated"] = True
        endpoint._sunset_date = sunset_date
        endpoint._migration_path = migration_path
        return endpoint
    return decorator


# Usage:
@router.get("/v1/users/{user_id}")
@deprecated(sunset_date="2026-09-01", migration_path="Use GET /v2/users/{user_id}")
async def get_user_legacy(user_id: str):
    ...
```

---

## 10. Environment Configuration

```python
# core/config.py — complete pydantic-settings configuration

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class Settings(BaseSettings):
    """Application configuration loaded from environment variables.

    All configuration is loaded from environment variables with
    sensible defaults for local development. In production, all
    values must be explicitly set.

    Naming convention: UPPER_SNAKE_CASE matching env var names.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── API ──────────────────────────────────────────────────────────
    API_VERSION: str = "1.0.0"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"  # Used for JWT signing

    # ── Database ──────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://OpenZep:OpenZep@localhost:5432/OpenZep",
        description="PostgreSQL DSN. MUST use postgresql+asyncpg:// scheme.",
    )

    # ── Redis ─────────────────────────────────────────────────────────
    REDIS_URL: str = Field(
        default="redis://localhost:6379",
        description="Redis connection string for cache, rate limiting, and ARQ.",
    )

    # ── Graph DB ──────────────────────────────────────────────────────
    GRAPH_BACKEND: str = Field(
        default="falkordb",
        description="Graph backend: 'falkordb' or 'neo4j'.",
    )
    FALKORDB_URL: str = Field(
        default="redis://localhost:6380",
        description="FalkorDB connection string (Redis protocol).",
    )
    NEO4J_URL: Optional[str] = None
    NEO4J_USER: Optional[str] = None
    NEO4J_PASSWORD: Optional[str] = None

    # ── LLM ───────────────────────────────────────────────────────────
    LLM_BACKEND: str = Field(
        default="openai",
        description="LLM backend: 'openai', 'azure', or 'ollama'.",
    )
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: Optional[str] = None
    OLLAMA_BASE_URL: Optional[str] = "http://localhost:11434"
    LLM_MODEL: str = "gpt-4o-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIM: int = 1536

    # ── CORS & Security ───────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000"
    TRUSTED_HOSTS: str = "localhost,127.0.0.1"

    # ── Rate Limiting ─────────────────────────────────────────────────
    RATE_LIMIT_DEFAULT: int = Field(
        default=100,
        description="Default rate limit: requests per minute per API key.",
    )
    RATE_LIMIT_AUTH_FAIL: int = Field(
        default=10,
        description="Failed auth attempts per IP per minute (SEC-06).",
    )

    # ── Observability ──────────────────────────────────────────────────
    OTLP_ENDPOINT: Optional[str] = None
    LOG_LEVEL: str = "INFO"
    SERVICE_NAME: str = "OpenZep-api"
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

CMD ["uvicorn", "services.api.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

---

## 12. Testing the App Setup

### 12.1 Conftest Fixtures

```python
# tests/conftest.py

import pytest
from httpx import AsyncClient, ASGITransport
from services.api.main import create_app


@pytest.fixture
def app():
    """Create a fresh app instance for each test."""
    return create_app()


@pytest.fixture
async def async_client(app) -> AsyncClient:
    """Create an async HTTP client backed by the ASGI app.

    Uses ASGITransport directly — no need to run uvicorn.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def auth_headers() -> dict:
    """Dummy auth headers for tests that require authentication.

    ⚠️ In integration tests, a real API key should be created
    via the admin API. This fixture is for unit tests only.
    """
    return {"Authorization": "Bearer mg_test_testkey1234567890abc"}
```

### 12.2 Tests

```python
# tests/unit/test_health.py

@pytest.mark.asyncio
async def test_health_endpoint(async_client: AsyncClient) -> None:
    """Verify /health returns 200 with status 'healthy'."""
    response = await async_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "OpenZep-api"


@pytest.mark.asyncio
async def test_request_id_injected(async_client: AsyncClient) -> None:
    """Verify every response includes an X-Request-ID."""
    response = await async_client.get("/health")
    assert "X-Request-ID" in response.headers
    assert response.headers["X-Request-ID"].startswith("req_")


@pytest.mark.asyncio
async def test_openapi_spec_generated(async_client: AsyncClient) -> None:
    """Verify OpenAPI spec is valid and contains expected paths."""
    response = await async_client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"] == "OpenZep API"
    assert spec["info"]["version"] == "1.0.0"
    assert "/v1/users" in spec["paths"]


@pytest.mark.asyncio
async def test_cors_allowed_origin(async_client: AsyncClient) -> None:
    """Verify CORS headers for an allowed origin."""
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
    """Verify CORS rejects disallowed origins."""
    response = await async_client.options(
        "/v1/users",
        headers={
            "Origin": "https://evil.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_auth_required_on_v1_endpoints(async_client: AsyncClient) -> None:
    """Verify /v1/* endpoints require authentication."""
    response = await async_client.get("/v1/users")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_public_paths_accessible(async_client: AsyncClient) -> None:
    """Verify public paths are accessible without auth."""
    for path in ["/health", "/docs", "/redoc", "/openapi.json"]:
        response = await async_client.get(path)
        assert response.status_code in (200, 422), f"{path} returned {response.status_code}"
```

---

## 13. Docker Compose Integration

```yaml
# docker-compose.yml (relevant section)

services:
  api:
    build:
      context: .
      dockerfile: services/api/Dockerfile
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql+asyncpg://OpenZep:OpenZep@postgres:5432/OpenZep
      - REDIS_URL=redis://redis:6379
      - FALKORDB_URL=redis://falkordb:6380
      - CORS_ORIGINS=http://localhost:3000,http://localhost:8000
      - SECRET_KEY=dev-secret-key-not-for-production
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
      falkordb:
        condition: service_started
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
```

---

## 14. Deployment Checklist

- [ ] `SECRET_KEY` set to a cryptographically random 64-byte hex string
- [ ] `CORS_ORIGINS` set to exact list of dashboard domains, never `*`
- [ ] `TRUSTED_HOSTS` restricted to known domains
- [ ] `DATABASE_URL` uses `postgresql+asyncpg://` (not `postgresql://`)
- [ ] `OTLP_ENDPOINT` configured for trace export
- [ ] `LOG_LEVEL` set to `INFO` (not `DEBUG`) in production
- [ ] Health check configured in Docker Compose / Kubernetes
- [ ] Readiness probe configured in Kubernetes (checks PG + Redis + FalkorDB)
- [ ] Workers count matches CPU cores (not memory)

---

## 15. Common Pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| `postgresql://` instead of `postgresql+asyncpg://` | Sync engine under async — silent performance bug | Use `postgresql+asyncpg://` in config validation |
| `expire_on_commit=True` (default) | `DetachedInstanceError` in async handlers | Set `expire_on_commit=False` on session factory |
| CORS `*` in production | Any website can make authenticated requests | Set explicit `CORS_ORIGINS` list |
| Missing `pool_pre_ping=True` | Stale connections after idle period | Always set `pool_pre_ping=True` |
| Middleware order wrong: Auth before CORS | OPTIONS preflight returns 401 | Order: CORS → ... → Auth |

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
