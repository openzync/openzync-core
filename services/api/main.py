"""OpenZep API — FastAPI application factory.

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
from core.graph_backend import init_graph_backend
# graphiti is only used when GRAPH_BACKEND=graphiti (legacy)
# The factory handles the conditional import; this shutdown guard
# avoids importing the 361-line module on every startup.
from core.logging import setup_logging
from core.redis import close_redis, init_redis
from middleware.auth import AuthMiddleware
from middleware.logging import LoggingMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.request_id import RequestIDMiddleware
from middleware.tracing import TracingMiddleware
from routers import admin, context, facts, graph, health, memory, search, sessions, users


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

        # Init graph backend — selected by GRAPH_BACKEND config
        # Supports: postgres (default), graphiti (legacy FalkorDB), none
        # For the postgres backend, we create a dedicated session that stays
        # alive for the entire app lifetime (yield keeps the async with open).
        session_factory = get_async_session(db_engine)
        async with session_factory() as graph_session:
            try:
                app.state.graph_backend = await init_graph_backend(db=graph_session)
            except Exception:
                import structlog

                structlog.get_logger().warning(
                    "graph_backend.init_failed",
                    error="Graph backend could not be initialised. "
                    "Graph-backed memory features will be unavailable.",
                )
                app.state.graph_backend = None

            yield

        # ── Shutdown (reverse order of initialisation) ────────────────────
        # Close Graphiti client if it was initialized by the factory
        if hasattr(app.state, 'graph_backend'):
            from core.graphiti import close_graphiti
            try:
                await close_graphiti()
            except Exception:
                pass
        await close_arq()
        await close_redis(redis_client)
        await close_db_engine(db_engine)

    app = FastAPI(
        title="OpenZep API",
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
    # 1. Request ID — must be first so every downstream component has an ID.
    app.add_middleware(RequestIDMiddleware)

    # 2. CORS — validate origin headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS.split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 3. Trusted Host — prevent host-header attacks in production.
    allowed_hosts = (
        settings.CORS_ORIGINS.split(",")
        if settings.ENVIRONMENT == "production"
        else ["*"]
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )

    # 4. GZip compression — compress responses >= 1 KB.
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # 5. Auth — extract and validate JWT, set org_id/user_id on request state.
    app.add_middleware(AuthMiddleware)

    # 6. Rate limiting — per-IP / per-token sliding window.
    app.add_middleware(RateLimitMiddleware)

    # 7. Tracing — OpenTelemetry span management and propagation.
    app.add_middleware(TracingMiddleware)

    # 8. Structured logging — log request/response lifecycle.
    app.add_middleware(LoggingMiddleware)

    # ── Routers ──────────────────────────────────────────────────────────
    app.include_router(health.router, prefix="/v1", tags=["Health"])
    app.include_router(admin.router)
    app.include_router(users.router)
    app.include_router(sessions.router)
    app.include_router(memory.router)
    app.include_router(context.router)
    app.include_router(search.router)
    app.include_router(graph.router)
    app.include_router(facts.router)

    return app


# Module-level ASGI application — uvicorn imports this directly.
app = create_app()
