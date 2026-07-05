"""OpenZync API — FastAPI application factory.

Creates a configured FastAPI instance with:
- Lifespan-managed DB engine, Redis, ARQ pool, and Graphiti client
- Structured logging via structlog
- CORS, GZip, TrustedHost, Auth, RateLimit, Tracing, and RequestID middleware
- RFC 7807-compliant exception handlers
- Health-check router

Usage:
    uvicorn services.api.main:app --host 0.0.0.0 --port 8000

Or via the ASGI entry point:
    uvicorn services.api.asgi:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from core.arq import close_arq, init_arq
from core.config import Settings
from core.db import close_db_engine, get_async_session, init_db_engine
from core.exceptions import register_exception_handlers
from core.graph_backend import init_dispatcher
from core.logging import setup_logging
from core.redis import close_redis, init_redis
from redis.asyncio import BlockingConnectionPool
from middleware.audit import AuditMiddleware
from middleware.auth import AuthMiddleware
from middleware.logging import LoggingMiddleware
from middleware.metrics import MetricsMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.request_id import RequestIDMiddleware
from middleware.tracing import TracingMiddleware
from routers import (
    admin,
    admin_metrics,
    admin_org_config,
    admin_organizations,
    admin_schemas,
    admin_stats,
    admin_webhooks,
    audit_log,
    auth,
    classifications,
    context,
    facts,
    graph,
    health,
    memory,
    metrics,
    project_api_keys,
    projects,
    search,
    sessions,
    structured_extractions,
    users,
)


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build and return a fully configured FastAPI application.

    Call once at module level — the returned ``app`` is the ASGI application.

    Returns:
        A configured :class:`FastAPI` instance ready for uvicorn.
    """
    settings = Settings()
    setup_logging(settings.ENVIRONMENT, settings.LOG_LEVEL)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # ── Startup ───────────────────────────────────────────────────────
        db_engine = init_db_engine(str(settings.DATABASE_URL))
        redis_client = await init_redis(str(settings.REDIS_URL))
        app.state.db_engine = db_engine
        app.state.redis = redis_client
        app.state.db_session_factory = get_async_session(db_engine)

        # Init ARQ (async Redis queue) for background jobs
        arq_pool = await init_arq(str(settings.REDIS_URL))
        app.state.arq_pool = arq_pool

        # Init graph-backend dispatcher — a singleton registry of backend
        # classes.  Actual backend instances are created per-request by
        # dependencies using ``resolve_and_create(org_config, db)``.
        app.state.graph_backend_dispatcher = init_dispatcher()

        # Init SurrealDB per-org connection pool (optional — requires surrealdb).
        from core.surreal_pool import SurrealConnectionPool

        app.state.surreal_connection_pool = SurrealConnectionPool()
        logger.info("surreal_pool.initialised")

        # Init FalkorDB client (optional — requires falkordb server running).
        try:
            from falkordb.asyncio import FalkorDB

            falkordb_pool = BlockingConnectionPool.from_url(
                settings.FALKORDB_URL,
                max_connections=settings.FALKORDB_MAX_CONNECTIONS,
                socket_timeout=settings.FALKORDB_SOCKET_TIMEOUT,
                socket_keepalive=True,
                decode_responses=True,
            )
            app.state.falkordb_client = FalkorDB(connection_pool=falkordb_pool)
            logger.info("falkordb_pool.initialised")
        except Exception:
            logger.warning("falkordb_pool.initialisation_failed — FalkorDB is optional")
            app.state.falkordb_client = None

        yield

        # ── Shutdown (reverse order of initialisation) ────────────────────
        if getattr(app.state, "falkordb_client", None) is not None:
            await app.state.falkordb_client.aclose()
        await app.state.surreal_connection_pool.close_all()
        await close_arq()
        await close_redis(redis_client)
        await close_db_engine(db_engine)

    app = FastAPI(
        title="OpenZync API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Store settings in app.state for dependency-injection access
    app.state.settings = settings

    # ── Exception handlers (RFC 7807) ────────────────────────────────────
    register_exception_handlers(app)

    # ── Middleware stack (order matters!) ─────────────────────────────────
    # Starlette middleware is LIFO — the last `add_middleware()` call wraps
    # the outermost layer and runs first on every request.
    #
    # The numbered comments below show the RUNTIME execution order
    # (outermost → innermost).  Registration order is the reverse.
    #
    # Runtime order (outermost → innermost):
    #   0. MetricsMiddleware   — RED metrics (wraps everything including 404)
    #   1. CORSMiddleware       — intercept OPTIONS preflight immediately
    #   2. LoggingMiddleware    — log request/response lifecycle
    #   3. TracingMiddleware    — OpenTelemetry span management
    #   4. RateLimitMiddleware  — per-IP / per-token sliding window
    #   5. AuthMiddleware       — extract/validate JWT & API key
    #   6. AuditMiddleware      — record request to audit_logs (post-response)
    #   7. GZipMiddleware       — compress responses >= 1 KB
    #   8. TrustedHostMiddleware — prevent host-header attacks
    #   9. RequestIDMiddleware   — assign request_id (innermost, closest to router)

    # Runtime 0 (outermost) — Metrics: captures EVERY request including 404s.
    app.add_middleware(MetricsMiddleware)

    # Runtime 9 (innermost) — Request ID: spans every downstream component.
    app.add_middleware(RequestIDMiddleware)

    # Runtime 8 — Trusted Host: prevent host-header attacks in production.
    allowed_hosts = (
        settings.HOSTS_ALLOWED.split(",")
        if settings.ENVIRONMENT == "production"
        else ["*"]
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )

    # Runtime 7 — GZip compression
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Runtime 5 — Auth: extract/validate JWT & API key, set request state.
    app.add_middleware(AuthMiddleware)

    # Runtime 6 — Audit: record request to audit_logs (post-response, never blocks).
    app.add_middleware(AuditMiddleware)

    # Runtime 4 — Rate limiting
    app.add_middleware(RateLimitMiddleware)

    # Runtime 3 — Tracing
    app.add_middleware(TracingMiddleware)

    # Runtime 2 — Structured logging
    app.add_middleware(LoggingMiddleware)

    # Runtime 1 (outermost after Metrics) — CORS: intercepts OPTIONS preflight
    # BEFORE AuthMiddleware rejects them.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS.split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ──────────────────────────────────────────────────────────
    app.include_router(health.router, prefix="/v1", tags=["Health"])
    app.include_router(admin.router)
    app.include_router(admin_metrics.router)
    app.include_router(admin_schemas.router)
    app.include_router(admin_stats.router)
    app.include_router(admin_webhooks.router)
    app.include_router(admin_organizations.router)
    app.include_router(admin_org_config.router)
    app.include_router(audit_log.router)
    app.include_router(admin_metrics.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(sessions.router)
    app.include_router(classifications.router)
    app.include_router(structured_extractions.router)
    app.include_router(memory.router)
    app.include_router(context.router)
    app.include_router(search.router)
    app.include_router(graph.router)
    app.include_router(facts.router)
    app.include_router(projects.router)
    app.include_router(project_api_keys.router)

    # Metrics: intentionally registered last and outside /v1 so it responds
    # at ``/metrics`` (not ``/v1/metrics``) for standard Prometheus scraping.
    app.include_router(metrics.router)

    return app


# Module-level ASGI application — uvicorn imports this directly.
app = create_app()
