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
from redis.asyncio import BlockingConnectionPool

from core.arq import close_arq, init_arq
from core.config import get_settings
from core.db import close_db_engine, get_async_session, init_db_engine
from core.exceptions import register_exception_handlers
from core.graph_backend import init_dispatcher
from core.logging import setup_logging
from core.redis import close_redis, init_redis
from middleware.audit import AuditMiddleware
from middleware.auth import AuthMiddleware
from middleware.ext_null_origin_cors import (
    EXT_CORS_ALLOW_HEADERS,
    EXT_CORS_ALLOW_METHODS,
    ExtNullOriginCorsMiddleware,
)
from middleware.logging import LoggingMiddleware
from middleware.metrics import MetricsMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.request_id import RequestIDMiddleware
from middleware.tracing import TracingMiddleware
from routers import (
    admin_addons,
    admin_invites,
    admin_llm_usage,
    admin_metrics,
    admin_org_config,
    admin_schemas,
    admin_organizations,
    admin_quick_actions,
    admin_schemas,
    admin_stats,
    admin_system,
    admin_webhooks,
    api_key_self,
    audit_log,
    auth,
    classifications,
    context,
    facts,
    global_search,
    graph,
    health,
    me,
    memory,
    metrics,
    observations,
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
    settings = get_settings()
    setup_logging(settings.ENVIRONMENT, settings.LOG_LEVEL)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # ── Startup ───────────────────────────────────────────────────────
        db_engine = init_db_engine(str(settings.DATABASE_URL))
        redis_client = init_redis(str(settings.REDIS_URL))
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

        # Aux-outage ledger — components that fail init record a reason
        # here instead of aborting boot.  /ready reports them as degraded;
        # only Postgres/Redis (above) stay fail-fast.
        app.state.degraded: dict[str, str] = {}

        # Init SurrealDB per-org connection pool (optional — requires surrealdb).
        from core.surreal_pool import SurrealConnectionPool

        app.state.surreal_connection_pool = SurrealConnectionPool()
        logger.info("surreal_pool.initialised")

        # Init FalkorDB client — required (default graph backend is falkordb).
        # Only skips when FALKORDB_URL is explicitly unset (graph_backend=none).
        if not getattr(settings, "FALKORDB_URL", None):
            logger.warning("falkordb_pool.skipped — FALKORDB_URL not set, falkordb backends will 503")
            app.state.falkordb_client = None
        else:
            try:
                from falkordb.asyncio import FalkorDB

                falkordb_pool = BlockingConnectionPool.from_url(
                    settings.FALKORDB_URL,
                    max_connections=settings.FALKORDB_MAX_CONNECTIONS,
                    socket_timeout=settings.FALKORDB_SOCKET_TIMEOUT,
                    socket_connect_timeout=5,  # hung TCP must fail fast
                    socket_keepalive=True,
                    decode_responses=True,
                )
                app.state.falkordb_client = FalkorDB(connection_pool=falkordb_pool)
                logger.info("falkordb_pool.initialised", extra={"url": settings.FALKORDB_URL})
            except Exception as exc:
                # Aux outage must not kill the API — degrade loudly and
                # serve.  FalkorDB-backed graph calls 503 until it
                # recovers; /ready reports the degradation.
                app.state.falkordb_client = None
                app.state.degraded["falkordb"] = str(exc)
                logger.error(
                    "falkordb_pool.init_failed — degraded, backends 503",
                    exc_info=True,
                    extra={"url": settings.FALKORDB_URL, "error": str(exc)},
                )

        # ── Verify local crypto helper (degraded, not fatal) ────────────
        # Webhook signing encrypts at create/rotate time and decrypts at
        # every emit via core.crypto (Fernet subkeys from OZ_SECRET_KEY).
        # A missing key fails fast in get_settings() at import; an unusable
        # helper degrades loudly instead of aborting boot — /ready reports
        # it until crypto recovers.
        try:
            from core import crypto as _crypto

            _probe = _crypto.encrypt("webhook-secret", "startup", "probe")
            _crypto.decrypt("webhook-secret", "startup", _probe)
        except Exception as exc:
            app.state.degraded["crypto"] = str(exc)
            logger.error(
                "crypto.self_check_failed — degraded, webhooks 502",
                exc_info=True,
            )
        else:
            logger.info("crypto.self_check_ok")

        # ⚠️ BREAKING: boot-time platform seeding removed — the root user is
        # created exactly once via public POST /v1/auth/setup, and /ready
        # reports needs_setup until then.  No Redis leader lock, no seed.

        # ── Prewarm the frozen embedder (degraded, not fatal) ───────────
        # DB/Redis/graph backends are up; prove Ollama /api/embed is
        # reachable and run one warm inference before serving traffic,
        # with jittered retry for the Ollama cold-start window.
        # An unreachable Ollama degrades loudly (embedding calls 503
        # until it recovers) instead of aborting boot — /ready reports it.
        from core.embeddings import prewarm_with_retry

        try:
            await prewarm_with_retry()
        except Exception as exc:
            app.state.degraded["embeddings"] = str(exc)
            logger.error(
                "embeddings.prewarm_failed — degraded, embedding calls may 503",
                exc_info=True,
            )
        else:
            logger.info("embeddings.prewarmed")

        # ── Addon runtime: hot-load enabled addons ─────────────────────
        # Bypass protection is structural now: addon contexts hold ONLY
        # AddonSession (core/addons/sessions.py), which core.rls refuses
        # to bypass — no monkey-patching, no frame-walking.
        app.state.addons = {}
        try:
            from core.addons.installer import ADDON_ROOT
            from core.addons.loader import load_addon
            from core.addons.manifest import parse_manifest
            from core.addons.mount import mount_addon
            from core.addons.registry import (
                AddonRecord,
                next_generation,
                publish_registry,
            )
            from models.addon_state import AddonState
            from sqlalchemy import select

            async with app.state.db_session_factory() as _s:
                rows = (await _s.execute(select(AddonState))).scalars().all()
            records: dict = {}
            paths: dict = {}
            for _row in rows:
                if not _row.enabled:
                    continue
                _slug = (
                    _row.source_ref.replace("/", "__")
                    if _row.source_type == "github"
                    else _row.id
                )
                _src = ADDON_ROOT / _slug
                try:
                    _manifest = parse_manifest(_src)
                    _module = load_addon(_manifest, _src, _row.version)
                    from core.addons.hooks import (
                        resolve_addon_router as _resolve_router,
                    )
                    from core.addons.hooks import (
                        resolve_worker_ops as _resolve_ops,
                    )

                    _router = _resolve_router(_module)
                    if _router is not None and getattr(_module, "router", None) is None:
                        _module.router = _router
                    records[_row.id] = AddonRecord(
                        manifest=_manifest,
                        module=_module,
                        version=_row.version,
                        enabled=True,
                        generation=next_generation(),
                        service_overrides=dict(
                            getattr(_module, "SERVICE_OVERRIDES", {})
                        ),
                        router=_router,
                        worker_ops=_resolve_ops(_module),
                    )
                    paths[_row.id] = _src
                except Exception as exc:
                    logger.error(
                        "addon.boot_load_failed",
                        extra={"addon": _row.id, "error": str(exc)},
                    )
            publish_registry(app, records)
            for _aid, _rec in records.items():
                try:
                    mount_addon(app, _rec, paths[_aid])
                except Exception as exc:
                    logger.error(
                        "addon.boot_mount_failed",
                        extra={"addon": _aid, "error": str(exc)},
                    )
        except Exception as exc:
            # Boot must not fail on addon errors — degrade loudly, serve core.
            app.state.degraded["addons"] = str(exc)
            logger.error("addons.boot_failed — degraded", exc_info=True)

        yield

        # ── Shutdown (reverse order of initialisation) ────────────────────
        await admin_metrics.close_prom_client()
        if getattr(app.state, "falkordb_client", None) is not None:
            await app.state.falkordb_client.connection.aclose()
        await app.state.surreal_connection_pool.close_all()
        await close_arq()
        await close_redis(redis_client)
        await close_db_engine(db_engine)

    from core._version import __version__

    app = FastAPI(
        title="OpenZync API",
        version=__version__,
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
    #   0. ExtNullOriginCorsMiddleware (null-origin shim, /v1/ext/* only)
    #   1. CORSMiddleware
    #   1. LoggingMiddleware
    #   2. TracingMiddleware
    #   3. AuthMiddleware       — sets user_id
    #   4. MetricsMiddleware    — reads user_id (moved inside Auth to be org-scoped)
    #   5. RateLimitMiddleware  — reads user_id
    #   6. AuditMiddleware
    #   7. GZipMiddleware
    #   8. TrustedHostMiddleware
    #   9. RequestIDMiddleware

    # Runtime 9 (innermost) — Request ID: spans every downstream component.
    app.add_middleware(RequestIDMiddleware)

    # Runtime 8 — Trusted Host: prevent host-header attacks in production.
    # Non-prod allows anything (local/dev convenience); prod fails CLOSED —
    # an empty HOSTS_ALLOWED is a startup error, never a silent allow-all.
    if settings.ENVIRONMENT == "production":
        allowed_hosts = [
            o.strip() for o in settings.HOSTS_ALLOWED.split(",") if o.strip()
        ]
        if not allowed_hosts:
            raise RuntimeError(
                "HOSTS_ALLOWED must not be empty in production — "
                "TrustedHostMiddleware would reject every request"
            )
        cors_origins = [
            o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()
        ]
        if not cors_origins:
            raise RuntimeError(
                "CORS_ORIGINS must not be empty in production — "
                "fail-closed: no origins means no browser access"
            )
    else:
        allowed_hosts = ["*"]
        cors_origins = [
            o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()
        ]
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
    )

    # Runtime 7 — GZip compression
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Runtime 6 — Audit: record request to audit_logs (post-response, never blocks).
    app.add_middleware(AuditMiddleware)

    # Runtime 5 — Rate limiting: registered BEFORE Auth so it runs AFTER Auth,
    # giving it access to scope["state"]["user_id"] set by AuthMiddleware.
    # (Registered later = executes earlier; see LIFO note above.)
    app.add_middleware(RateLimitMiddleware)

    # Runtime 4 — Metrics: registered BEFORE Auth so it runs AFTER Auth,
    # giving it access to user_id for org-scoped RED metrics.
    # (Moved inside Auth — previously outermost, could not see user_id.)
    app.add_middleware(MetricsMiddleware)

    # Runtime 3 — Auth: extract/validate JWT & API key, set request state.
    app.add_middleware(AuthMiddleware)

    # Runtime 2 — Tracing
    app.add_middleware(TracingMiddleware)

    # Runtime 1 — Structured logging
    app.add_middleware(LoggingMiddleware)

    # Runtime 1/0 (outermost pair) — CORS: CORSMiddleware intercepts OPTIONS
    # preflight BEFORE AuthMiddleware rejects them. Methods/headers are an
    # explicit allowlist (frontend sends Authorization + Content-Type;
    # X-Request-ID is accepted for tracing) — never "*" with
    # allow_credentials=True. The method/header lists live in
    # middleware.ext_null_origin_cors so the null-origin shim mirrors them.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=EXT_CORS_ALLOW_METHODS,
        allow_headers=EXT_CORS_ALLOW_HEADERS,
    )

    # Runtime 0 (outermost) — null-origin shim for opaque-origin addon
    # panels (sandbox="allow-scripts" iframes send `Origin: null`) on
    # /v1/ext/* only; bearer-only auth, no cookies, so no credentials flag.
    # Never extend beyond /v1/ext/*.
    app.add_middleware(ExtNullOriginCorsMiddleware)

    # ── Routers ──────────────────────────────────────────────────────────
    # Health must live at ROOT (not /v1) — Helm/NGINX probes target /health
    # and /ready; the /v1 prefix made them 404 on deployed instances.
    app.include_router(health.router, tags=["Health"])
    app.include_router(admin_addons.router)
    app.include_router(admin_llm_usage.router)
    app.include_router(admin_metrics.router)
    app.include_router(admin_schemas.router)
    app.include_router(admin_stats.router)
    app.include_router(admin_quick_actions.router)
    app.include_router(admin_webhooks.router)
    app.include_router(admin_organizations.router)
    app.include_router(admin_org_config.router)
    app.include_router(admin_invites.router)
    app.include_router(admin_system.router)
    app.include_router(audit_log.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(users.router)
    app.include_router(sessions.router)
    app.include_router(classifications.router)
    app.include_router(structured_extractions.router)
    app.include_router(observations.router)
    app.include_router(memory.router)
    app.include_router(context.router)
    app.include_router(search.router)
    app.include_router(graph.router)
    app.include_router(global_search.router)
    app.include_router(facts.router)
    app.include_router(projects.router)
    app.include_router(project_api_keys.router)
    app.include_router(api_key_self.router)

    # Metrics: intentionally registered last and outside /v1 so it responds
    # at ``/metrics`` (not ``/v1/metrics``) for standard Prometheus scraping.
    app.include_router(metrics.router)

    return app


# Module-level ASGI application — uvicorn imports this directly.
app = create_app()
