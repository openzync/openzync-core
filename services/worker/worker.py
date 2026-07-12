"""ARQ worker entrypoint — starts the worker pool, registers tasks, handles signals.

Usage:

    python -m services.worker.worker

Environment variables are loaded via :mod:`pydantic-settings` from
:class:`services.worker.worker_settings.WorkerSettings`.

Architecture
------------
Two separate ARQ worker pools run in a single process:

* **High-priority queue** — real-time ingestion tasks
  (entity extraction, embedding, classification, graph sync).
* **Low-priority queue** — batch / scheduled tasks
  (community summarisation, data ingestion, entity merge dedup).

Each pool has independent concurrency and timeout settings.  The worker also
exposes a Prometheus metrics endpoint and an aiohttp health-check server for
Kubernetes liveness / readiness probes.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

import structlog
from aiohttp import web
from arq.connections import ArqRedis, RedisSettings
from arq.cron import CronJob, cron
from arq.worker import Worker as ArqWorker
from prometheus_client import Counter, Gauge, Histogram
from prometheus_client import start_http_server as start_prometheus_server

from services.worker.worker_settings import (
    get_queue_name,
    init_worker_settings_from_bao,
    settings,
)

from core.config import BootstrapSettings, init_settings
from core.openbao import OpenBaoClient

# ═════════════════════════════════════════════════════════════════════════════
# Structlog setup
# ═════════════════════════════════════════════════════════════════════════════


def setup_logging() -> None:
    """Configure structlog for ARQ worker logging.

    In production (``STRUCTLOG_FORMAT=json``) logs are emitted as JSON for
    ingestion by Loki.  In development, human-readable console output.

    Every log entry is automatically enriched with:
    * ``timestamp`` (ISO-8601)
    * ``level``
    * ``logger`` (``OpenZync.worker``)
    * ``trace_id``, ``org_id``, ``task_type``, ``job_id`` — bound per-task
      via :func:`structlog.contextvars.bind_contextvars`.
    """
    # Set root logger level so structlog's filter_by_level has something to read
    logging.getLogger().setLevel(logging.getLevelName(settings.LOG_LEVEL))

    shared_processors: list[structlog.types.Processor] = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.set_exc_info,
        structlog.contextvars.merge_contextvars,
    ]

    if settings.STRUCTLOG_FORMAT == "json":
        processors = [
            *shared_processors,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


logger: structlog.stdlib.BoundLogger = structlog.get_logger("OpenZync.worker")


# ═════════════════════════════════════════════════════════════════════════════
# Task registry
# ═════════════════════════════════════════════════════════════════════════════

from workers.tasks.classify_dialog import classify_dialog
from workers.tasks.embed_episode import embed_episode
from workers.tasks.embed_fact import embed_fact
from workers.tasks.extract_entities import extract_entities
from workers.tasks.extract_facts import extract_facts
from workers.tasks.extract_structured import extract_structured
from services.worker.tasks.audit_log import write_audit_log
from workers.tasks.merge_duplicate_entities import merge_duplicate_entities
from workers.tasks.summarise_community import summarise_community
from workers.tasks.link_entities_to_episode import link_entities_to_episode
from workers.tasks.compute_observations import compute_observations
from services.worker.tasks.deliver_webhook import deliver_webhook
from workers.tasks.generate_user_summary import generate_user_summary
from workers.tasks.reconcile_enrichment import reconcile_enrichment

HIGH_QUEUE_TASKS: list[Callable[..., Awaitable[Any]]] = [
    classify_dialog,
    extract_entities,
    embed_episode,
    extract_facts,
    embed_fact,
    extract_structured,
]
"""Tasks assigned to the high-priority queue (real-time ingestion)."""

LOW_QUEUE_TASKS: list[Callable[..., Awaitable[Any]]] = [
    link_entities_to_episode,
    compute_observations,
    summarise_community,
    merge_duplicate_entities,
    write_audit_log,
    deliver_webhook,
    generate_user_summary,
    reconcile_enrichment,
]
"""Tasks assigned to the low-priority queue (scheduled batch)."""


# ═════════════════════════════════════════════════════════════════════════════
# Signal handling
# ═════════════════════════════════════════════════════════════════════════════


_shutdown_requested: bool = False
"""Global flag — set by :func:`handle_signal`; checked by the worker loop."""


def handle_signal(signum: int, _frame: object | None = None) -> None:
    """Handle SIGTERM/SIGINT for graceful shutdown.

    Sets a global flag; the worker loop checks this between jobs.  The current
    job completes; no new jobs are accepted.  A second signal forces an exit.

    Args:
        signum: Signal number (e.g. ``signal.SIGTERM``).
        _frame: Current stack frame (ignored — present for signal handler API
            compatibility).
    """
    global _shutdown_requested  # noqa: PLW0603  — intentional module-level flag

    if _shutdown_requested:
        # Second signal received while already shutting down — force exit.
        logger.warning(
            "shutdown.force_exit",
            signal=signal.Signals(signum).name,
        )
        sys.exit(1)

    _shutdown_requested = True
    logger.info(
        "shutdown.signal_received",
        signal=signal.Signals(signum).name,
        message="Finishing current jobs, not accepting new ones.",
    )


# ═════════════════════════════════════════════════════════════════════════════
# Prometheus metrics
# ═════════════════════════════════════════════════════════════════════════════

worker_tasks_total = Counter(
    "openzync_worker_tasks_total",
    "Tasks completed by type and status",
    labelnames=["task_type", "status"],
)

worker_task_duration_seconds = Histogram(
    "openzync_worker_task_duration_seconds",
    "Task execution duration in seconds",
    labelnames=["task_type"],
    buckets=(1, 2.5, 5, 10, 15, 30, 60, 120, 300, 600),
)

worker_queue_depth = Gauge(
    "openzync_worker_queue_depth",
    "Current queue depth by queue name",
    labelnames=["queue_name"],
)

worker_tasks_per_org = Counter(
    "openzync_worker_tasks_per_org_total",
    "Tasks by org, type, and status for cost tracking",
    labelnames=["org_id", "task_type", "status"],
)


# ═════════════════════════════════════════════════════════════════════════════
# Queue depth monitoring (background task)
# ═════════════════════════════════════════════════════════════════════════════


async def monitor_queue_depth(redis: ArqRedis, interval: int = 15) -> None:
    """Periodically sample queue depth for all known queues.

    Runs as a background :class:`asyncio.Task` in the worker event loop.
    Exposes queue depth as a Prometheus Gauge metric.

    Args:
        redis: Connected :class:`ArqRedis` instance (from the high-priority
            worker pool).
        interval: Polling interval in seconds.
    """
    queue_names = [
        settings.high_queue_full,
        settings.low_queue_full,
    ]

    while not _shutdown_requested:
        for queue_name in queue_names:
            try:
                # ARQ stores pending jobs in a Redis sorted set: {queue_name}:jobs
                depth = await redis.zcard(f"{queue_name}:jobs")  # type: ignore[arg-type]
            except Exception:
                depth = None

            if depth is not None:
                worker_queue_depth.labels(queue_name=queue_name).set(depth)

        await asyncio.sleep(interval)


# ═════════════════════════════════════════════════════════════════════════════
# Helper: create worker pool
# ═════════════════════════════════════════════════════════════════════════════


def create_arq_worker(
    queue_name: str,
    functions: list[Callable[..., Awaitable[Any]]],
    redis_settings: RedisSettings,
    concurrency: int,
    timeout: int,
    cron_jobs: list[CronJob] | None = None,
    ctx: dict[str, Any] | None = None,
) -> ArqWorker:
    """Create a configured ARQ Worker instance for the given queue.

    Args:
        queue_name: Logical queue name (e.g. ``"high"`` or ``"low"``).  The
            fully qualified name is derived via :func:`get_queue_name`.
        functions: List of async task functions to register with this worker.
        redis_settings: ARQ :class:`RedisSettings` instance.
        concurrency: Number of concurrent tasks this worker processes.
        timeout: Default job timeout in seconds.
        cron_jobs: Optional list of :class:`CronJob` instances for scheduled
            tasks (e.g. nightly community detection).
        ctx: Shared context dict passed to every task invocation.  Used to
            inject the shared DB engine, session factory, and other resources
            so tasks don't create their own per-invocation connections.

    Returns:
        Configured :class:`ArqWorker` instance (not yet started).
    """
    return ArqWorker(
        ctx=ctx if ctx is not None else {},
        redis_settings=redis_settings,
        functions=functions,
        queue_name=get_queue_name(settings.ENV, queue_name),
        max_jobs=concurrency,
        job_timeout=timeout,
        keep_result=settings.JOB_KEEP_RESULT_FOR,
        keep_result_forever=False,
        poll_delay=settings.POLL_DELAY,
        on_job_end=on_job_end,
        on_shutdown=on_shutdown,
        cron_jobs=cron_jobs if cron_jobs is not None else [],
    )


# ═════════════════════════════════════════════════════════════════════════════
# Job lifecycle callbacks
# ═════════════════════════════════════════════════════════════════════════════


async def on_job_end(ctx: dict[str, Any]) -> None:
    """Log and record Prometheus metrics when a job ends.

    Called by ARQ after a job completes (success or failure).
    The ``ctx`` dict may contain ``job_id`` depending on ARQ version,
    but we always read it from the context.

    Args:
        ctx: ARQ worker context dict (includes ``task_type``, ``org_id``,
            ``trace_id`` — populated by each task function).
    """
    job_id = ctx.get("job_id", "unknown")
    task_type = ctx.get("task_type", "unknown")
    org_id = ctx.get("org_id", "unknown")
    trace_id = ctx.get("trace_id", "unknown")
    duration_s: float = ctx.get("runtime", 0.0)

    logger.info(
        "job.completed",
        trace_id=trace_id,
        org_id=org_id,
        task_type=task_type,
        job_id=job_id,
        duration_ms=round(duration_s * 1000),
    )

    worker_tasks_total.labels(task_type=task_type, status="success").inc()
    worker_task_duration_seconds.labels(task_type=task_type).observe(duration_s)
    worker_tasks_per_org.labels(
        org_id=org_id,
        task_type=task_type,
        status="success",
    ).inc()


async def on_shutdown(_ctx: dict[str, Any]) -> None:
    """Log when the worker pool shuts down and clean up resources.

    Args:
        _ctx: ARQ worker context dict (unused on shutdown).
    """
    bao = _ctx.get("openbao_client")
    if bao is not None:
        await bao.__aexit__(None, None, None)
    logger.info("worker.shutdown_complete")


# ═════════════════════════════════════════════════════════════════════════════
# Health check endpoint (aiohttp)
# ═════════════════════════════════════════════════════════════════════════════


async def health_check(request: web.Request) -> web.Response:
    """ARQ health check — verifies Redis connectivity.

    Used by Kubernetes liveness / readiness probes and Docker HEALTHCHECK.

    Returns:
        HTTP 200 with ``{"status": "ok", "redis_connected": true}``
        HTTP 503 if Redis is unreachable or no pool is configured.
    """
    pool: ArqRedis | None = request.app.get("redis_pool")
    if pool is None:
        return web.json_response(
            {
                "status": "unhealthy",
                "redis_connected": False,
                "error": "No Redis pool in application context",
            },
            status=503,
        )

    try:
        await pool.execute_command("PING")
        return web.json_response(
            {"status": "ok", "redis_connected": True},
        )
    except Exception as exc:
        logger.error("health_check.failed", error=str(exc))
        return web.json_response(
            {
                "status": "unhealthy",
                "redis_connected": False,
                "error": str(exc),
            },
            status=503,
        )


# ═════════════════════════════════════════════════════════════════════════════
# Main entrypoint
# ═════════════════════════════════════════════════════════════════════════════


async def main() -> NoReturn:
    """Start the ARQ worker pool, Prometheus server, and health endpoint.

    Startup sequence:

    1. Bootstrap from OpenBao — load secrets (DATABASE_URL, REDIS_URL) via
       worker-specific or main AppRole credentials (fail-fast).
    2. Configure structured logging
    3. Start the Prometheus metrics HTTP server on ``PROMETHEUS_PORT``
    4. Create ARQ Redis connection settings from ``REDIS_URL``
    5. Create high and low priority worker pools
    6. Register SIGTERM / SIGINT handlers for graceful shutdown
    7. Start aiohttp health check server on ``HEALTH_PORT`` (``/health``, ``/ready``)
    8. Start queue depth monitoring as a background :class:`asyncio.Task`
    9. Run both worker pools concurrently until a shutdown signal is received

    Returns:
        Never returns normally — always exits via signal handler.
    """
    # ═══════════════════════════════════════════════════════════════
    # Phase 1: Bootstrap from OpenBao (fail-fast)
    # ═══════════════════════════════════════════════════════════════
    # Use worker-specific AppRole credentials if provided, falling back
    # to the main OpenBao credentials.  This allows operators to scope
    # worker permissions independently from the API service.
    _bootstrap = BootstrapSettings()
    _role_id = _bootstrap.OPENBAO_WORKER_ROLE_ID or _bootstrap.OPENBAO_ROLE_ID
    _secret_id = _bootstrap.OPENBAO_WORKER_SECRET_ID or _bootstrap.OPENBAO_SECRET_ID
    async with OpenBaoClient(
        _bootstrap.OPENBAO_ADDR,
        _role_id,
        _secret_id,
        timeout=15.0,
    ) as _bao:
        await init_worker_settings_from_bao(_bao)
        await init_settings(_bao)

    # ── Persistent OpenBao client for org config resolution ──────────────
    # Used by _resolve_org_config in workers/backend.py to fetch per-org
    # config from OpenBao without creating a new client per task.
    _openbao_client = OpenBaoClient(
        _bootstrap.OPENBAO_ADDR,
        _role_id,
        _secret_id,
        timeout=15.0,
    )
    await _openbao_client.__aenter__()

    # ═══════════════════════════════════════════════════════════════
    # Phase 2: Normal startup (uses settings from OpenBao)
    # ═══════════════════════════════════════════════════════════════
    setup_logging()

    logger.info(
        "worker.starting",
        max_workers=settings.MAX_WORKERS,
        redis_url=str(settings.REDIS_URL),
        env=settings.ENV,
        prometheus_port=settings.PROMETHEUS_PORT,
        health_port=settings.HEALTH_PORT,
    )

    # ── Create shared DB engine ───────────────────────────────────────────
    # One engine per worker process, shared across all task invocations.
    # Eliminates per-task connection churn that would exhaust PostgreSQL's
    # max_connections at scale.
    from core.db import get_async_session, init_db_engine

    db_engine = init_db_engine(
        str(settings.DATABASE_URL),
        pool_size=10,
        max_overflow=5,
    )
    db_session_factory = get_async_session(db_engine)

    # ── Graph backend dispatcher ──────────────────────────────────────────
    # Single app-level registry of backend classes.  Per-org resolution
    # happens inside worker tasks via resolve_graph_backend().
    from core.graph_backend import init_dispatcher

    dispatcher = init_dispatcher()

    # ── SurrealDB per-org connection pool ────────────────────────────────
    # Created once per worker process, caches per-org AsyncSurreal
    # connections lazily.  SurrealDB is optional — the pool handles missing
    # URLs gracefully by raising GraphBackendUnavailableError.
    from core.surreal_pool import SurrealConnectionPool

    surreal_pool = SurrealConnectionPool()

    # ── FalkorDB client ──────────────────────────────────────────────────
    # Single app-level connection pool shared across all worker tasks.
    # FalkorDB is optional — only used when the per-org config selects it.
    falkordb_client: FalkorDB | None = None  # type: ignore[name-defined]
    try:
        from falkordb.asyncio import FalkorDB
        from redis.asyncio import BlockingConnectionPool

        falkordb_pool = BlockingConnectionPool.from_url(
            settings.FALKORDB_URL,
            max_connections=settings.FALKORDB_MAX_CONNECTIONS,
            socket_timeout=settings.FALKORDB_SOCKET_TIMEOUT,
            socket_keepalive=True,
            decode_responses=True,
        )
        falkordb_client = FalkorDB(connection_pool=falkordb_pool)
        logger.info(
            "falkordb_pool.initialised",
            url=settings.FALKORDB_URL,
            max_connections=settings.FALKORDB_MAX_CONNECTIONS,
        )
    except Exception:
        logger.warning(
            "falkordb_pool.init_failed",
            exc_info=True,
            message="FalkorDB is unavailable — graph backend will fall back to Postgres.",
        )

    # Build the shared context dict passed to all ARQ tasks.
    worker_ctx: dict[str, Any] = {
        "db_engine": db_engine,
        "db_session_factory": db_session_factory,
        "graph_backend_dispatcher": dispatcher,
        "surreal_connection_pool": surreal_pool,
        "falkordb_client": falkordb_client,
        "openbao_client": _openbao_client,
    }

    logger.info(
        "worker.db_engine_created",
        pool_size=10,
        max_overflow=5,
    )

    # ── Start Prometheus HTTP server ────────────────────────────────────
    try:
        start_prometheus_server(settings.PROMETHEUS_PORT)
        logger.info("prometheus.server_started", port=settings.PROMETHEUS_PORT)
    except OSError as exc:
        logger.error(
            "prometheus.server_failed",
            port=settings.PROMETHEUS_PORT,
            error=str(exc),
        )
        raise

    # ── Redis connection settings ───────────────────────────────────────
    redis_settings = RedisSettings.from_dsn(str(settings.REDIS_URL))

    # ── Create worker pools ─────────────────────────────────────────────
    # Two separate ARQ Worker instances for priority queue support.
    # See 05-priority-queues.md for details on allocation.

    high_worker = create_arq_worker(
        queue_name=settings.HIGH_QUEUE_NAME,
        functions=HIGH_QUEUE_TASKS,
        redis_settings=redis_settings,
        concurrency=min(settings.MAX_WORKERS, 8),
        timeout=settings.JOB_TIMEOUT_DEFAULT,
        ctx=worker_ctx,
    )

    # ── Enrichment reconciliation (every 5 min) ────────────────────
    # Safety net for worker crashes: re-enqueues enrichment tasks for
    # episodes that have been stale for >10 minutes.
    cron_jobs: list[CronJob] = [
        cron(
            reconcile_enrichment,
            minute=set(range(0, 60, 5)),
            unique=True,
            job_id="enrichment_reconciliation",
        ),
    ]

    # ── Community detection scheduling ─────────────────────────────
    # Two modes controlled by AUTO_RUN_COMMUNITY_DETECTION:
    #   true  → event-driven (chained after link_entities_to_episode, with dedup)
    #   false → nightly cron at 02:00 UTC (default)
    community_cron_jobs: list[CronJob] = []
    if not settings.AUTO_RUN_COMMUNITY_DETECTION:
        community_cron_jobs = [
            cron(
                summarise_community,
                hour=2,
                minute=0,
                unique=True,
                job_id="nightly_community_detection",
            ),
        ]
        logger.info(
            "worker.cron.community_detection_scheduled",
            schedule="daily at 02:00 UTC",
            mode="nightly",
            queue=settings.low_queue_full,
        )
    else:
        logger.info(
            "worker.cron.community_detection_enabled",
            mode="event-driven (after link_entities_to_episode)",
        )

    # Combine all cron jobs
    all_cron_jobs = cron_jobs + community_cron_jobs

    low_worker = create_arq_worker(
        queue_name=settings.LOW_QUEUE_NAME,
        functions=LOW_QUEUE_TASKS,
        redis_settings=redis_settings,
        concurrency=max(1, settings.MAX_WORKERS // 4),
        timeout=settings.JOB_TIMEOUT_DEFAULT * 2,
        cron_jobs=all_cron_jobs,
        ctx=worker_ctx,
    )

    logger.info(
        "worker.pools_created",
        high_queue=settings.high_queue_full,
        low_queue=settings.low_queue_full,
        high_concurrency=min(settings.MAX_WORKERS, 8),
        low_concurrency=max(1, settings.MAX_WORKERS // 4),
    )

    # ── Signal handlers ────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))

    logger.info("worker.signal_handlers_registered")

    # ── Health check web server ────────────────────────────────────────
    health_app = web.Application()
    health_app["redis_pool"] = high_worker.pool
    health_app.router.add_get("/ready", health_check)
    health_app.router.add_get("/health", health_check)

    runner = web.AppRunner(health_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.HEALTH_PORT)
    await site.start()
    logger.info("health.server_started", port=settings.HEALTH_PORT)

    # ── Queue depth monitoring ─────────────────────────────────────────
    monitor_task = asyncio.create_task(
        monitor_queue_depth(high_worker.pool),
    )

    # ── Run workers ────────────────────────────────────────────────────
    try:
        await asyncio.gather(
            high_worker.async_run(),
            low_worker.async_run(),
        )
    except asyncio.CancelledError:
        logger.info("worker.run_cancelled")
        raise
    finally:
        # Shutdown graph backends (reverse order of initialisation)
        if falkordb_client is not None:
            try:
                await falkordb_client.aclose()
                logger.debug("worker.falkordb_client_closed")
            except Exception:
                logger.warning("worker.falkordb_close_failed", exc_info=True)
        await surreal_pool.close_all()
        logger.debug("worker.surreal_pool_closed")
        await db_engine.dispose()
        logger.debug("worker.db_engine_disposed")
        monitor_task.cancel()
        await runner.cleanup()
        logger.info("worker.stopped")

    # NOTE: This line is never reached — asyncio.gather runs until a
    # shutdown signal is received.  The NoReturn return type is
    # intentionally unreachable.
    sys.exit(0)  # pragma: no cover


def entrypoint() -> None:
    """Synchronous entrypoint for ``python -m services.worker.worker``."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("worker.keyboard_interrupt")


if __name__ == "__main__":
    entrypoint()
