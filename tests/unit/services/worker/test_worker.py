"""Unit tests for ARQ worker entrypoint — signal handling, metrics, health.

Tests cover:
- Module-level shutdown flag and signal handler
- Prometheus metric creation and recording on job end
- Queue depth monitoring loop
- aiohttp health-check endpoint
- ``create_arq_worker`` factory function
- Job lifecycle callbacks (``on_job_end``, ``on_shutdown``)
- Task registry completeness and deduplication
- ``main`` entrypoint startup / shutdown lifecycle
- ``entrypoint`` synchronous wrapper
- ``setup_logging`` structlog configuration
"""

from __future__ import annotations

import asyncio
import signal
import sys
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_shutdown_flag() -> None:
    """Reset the module-level shutdown flag before each test.

    Uses ``import services.worker.worker as wmod`` and accesses
    ``wmod._shutdown_requested`` so mutations via ``handle_signal()``
    are reflected in assertions (unlike ``from ... import`` which creates
    a local snapshot).
    """
    import services.worker.worker as _wmod

    _wmod._shutdown_requested = False


# ═══════════════════════════════════════════════════════════════════════════════
# Signal handling
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestHandleSignal:
    """``handle_signal`` — graceful / forced shutdown."""

    def test_first_signal_sets_flag(self) -> None:
        """First SIGTERM sets _shutdown_requested and logs."""
        import services.worker.worker as _wmod

        assert not _wmod._shutdown_requested
        _wmod.handle_signal(signal.SIGTERM)
        assert _wmod._shutdown_requested

    def test_second_signal_exits(self) -> None:
        """Second signal while flag is set calls sys.exit(1)."""
        import services.worker.worker as _wmod

        _wmod.handle_signal(signal.SIGTERM)  # first

        with patch.object(sys, "exit") as mock_exit:
            _wmod.handle_signal(signal.SIGTERM)
            mock_exit.assert_called_once_with(1)

    def test_sigint_behaves_same_as_sigterm(self) -> None:
        """SIGINT follows the same two-signal pattern as SIGTERM."""
        import services.worker.worker as _wmod

        _wmod.handle_signal(signal.SIGINT)
        assert _wmod._shutdown_requested

    def test_frame_arg_is_accepted(self) -> None:
        """Handler accepts the optional frame argument (signal.signal API)."""
        import services.worker.worker as _wmod

        _wmod.handle_signal(signal.SIGTERM, {"fake": "frame"})
        assert True  # no exception raised


# ═══════════════════════════════════════════════════════════════════════════════
# Prometheus metrics
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestPrometheusMetrics:
    """Metric definitions."""

    def test_metrics_are_correct_types(self) -> None:
        """All four Prometheus metrics are defined and correctly typed."""
        import prometheus_client as prom

        from services.worker.worker import (
            worker_queue_depth,
            worker_task_duration_seconds,
            worker_tasks_per_org,
            worker_tasks_total,
        )

        assert isinstance(worker_tasks_total, prom.Counter)
        assert isinstance(worker_task_duration_seconds, prom.Histogram)
        assert isinstance(worker_queue_depth, prom.Gauge)
        assert isinstance(worker_tasks_per_org, prom.Counter)

    def test_metric_labelnames(self) -> None:
        """Metric label names match expected registration."""
        from services.worker.worker import (
            worker_queue_depth,
            worker_task_duration_seconds,
            worker_tasks_per_org,
            worker_tasks_total,
        )

        assert worker_tasks_total._labelnames == ("task_type", "status")
        assert worker_task_duration_seconds._labelnames == ("task_type",)
        assert worker_queue_depth._labelnames == ("queue_name",)
        assert worker_tasks_per_org._labelnames == ("org_id", "task_type", "status")

    def test_histogram_buckets(self) -> None:
        """Task duration histogram uses expected bucket boundaries."""
        from services.worker.worker import worker_task_duration_seconds

        expected = (1, 2.5, 5, 10, 15, 30, 60, 120, 300, 600)
        assert worker_task_duration_seconds._upper_bounds == pytest.approx(
            list(expected) + [float("inf")],
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Job lifecycle: on_job_end
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestOnJobEnd:
    """``on_job_end`` — metrics recording after job completion."""

    @pytest.mark.asyncio
    async def test_records_metrics_with_all_context(self) -> None:
        """All ctx fields are mapped to metric labels."""
        from services.worker.worker import on_job_end

        ctx: dict = {
            "job_id": "j-1",
            "task_type": "enrich",
            "org_id": "org-1",
            "trace_id": "t-1",
            "runtime": 3.5,
        }

        with (
            patch("services.worker.worker.worker_tasks_total") as mock_total,
            patch("services.worker.worker.worker_task_duration_seconds") as mock_dur,
            patch("services.worker.worker.worker_tasks_per_org") as mock_org,
        ):
            await on_job_end(ctx)

            mock_total.labels.assert_called_once_with(task_type="enrich", status="success")
            mock_total.labels.return_value.inc.assert_called_once()

            mock_dur.labels.assert_called_once_with(task_type="enrich")
            mock_dur.labels.return_value.observe.assert_called_once_with(3.5)

            mock_org.labels.assert_called_once_with(
                org_id="org-1", task_type="enrich", status="success",
            )
            mock_org.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_defaults(self) -> None:
        """Missing ctx fields fall back to 'unknown'."""
        from services.worker.worker import on_job_end

        with (
            patch("services.worker.worker.worker_tasks_total") as mock_total,
            patch("services.worker.worker.worker_task_duration_seconds") as mock_dur,
            patch("services.worker.worker.worker_tasks_per_org") as mock_org,
        ):
            await on_job_end({})

            mock_total.labels.assert_called_once_with(task_type="unknown", status="success")
            mock_org.labels.assert_called_once_with(
                org_id="unknown", task_type="unknown", status="success",
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Job lifecycle: on_shutdown
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestOnShutdown:
    """``on_shutdown`` — worker pool cleanup."""

    @pytest.mark.asyncio
    async def test_closes_openbao_client_when_present(self) -> None:
        """OpenBao client is closed via __aexit__ on shutdown."""
        from services.worker.worker import on_shutdown

        mock_bao = AsyncMock()
        ctx = {"openbao_client": mock_bao}

        await on_shutdown(ctx)

        mock_bao.__aexit__.assert_called_once_with(None, None, None)

    @pytest.mark.asyncio
    async def test_skips_when_no_openbao(self) -> None:
        """Missing openbao_client does not raise."""
        from services.worker.worker import on_shutdown

        await on_shutdown({})  # should not raise


# ═══════════════════════════════════════════════════════════════════════════════
# create_arq_worker factory
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestCreateArqWorker:
    """``create_arq_worker`` factory function.

    Note: ``create_arq_worker`` internally calls
    ``get_queue_name(settings.ENV, queue_name)``, which goes through the
    ``_SettingsProxy`` → ``get_worker_settings()`` — a singleton that is
    not initialised in unit tests.  We patch ``get_queue_name`` to return
    a synthetic value, avoiding the singleton dependency.
    """

    @staticmethod
    def _default_patches():
        _seed_worker_settings()
        return patch(
            "services.worker.worker.get_queue_name",
            return_value="OpenZync:test:queue:high",
        )

    def test_creates_worker_with_queue_name(self) -> None:
        """Worker is created with a fully-qualified queue name."""
        from arq.connections import RedisSettings

        from services.worker.worker import create_arq_worker

        async def dummy_task(_ctx: object) -> str:
            return "done"

        with self._default_patches(), patch("services.worker.worker.ArqWorker") as mock_aw_cls:
            create_arq_worker(
                queue_name="high",
                functions=[dummy_task],
                redis_settings=RedisSettings(host="localhost", port=6379),
                concurrency=5,
                timeout=300,
            )

            mock_aw_cls.assert_called_once()
            kwargs = mock_aw_cls.call_args.kwargs
            assert "OpenZync" in kwargs["queue_name"]
            assert kwargs["max_jobs"] == 5
            assert kwargs["job_timeout"] == 300
            assert kwargs["keep_result_forever"] is False

    def test_passes_context_dict(self) -> None:
        """Shared context dict is forwarded to ARQ Worker."""
        from arq.connections import RedisSettings

        from services.worker.worker import create_arq_worker

        async def dummy_task(_ctx: object) -> str:
            return "done"

        shared_ctx = {"db_engine": "fake"}

        with self._default_patches(), patch("services.worker.worker.ArqWorker") as mock_aw_cls:
            create_arq_worker(
                queue_name="high",
                functions=[dummy_task],
                redis_settings=RedisSettings(host="localhost"),
                concurrency=4,
                timeout=300,
                ctx=shared_ctx,
            )

            assert mock_aw_cls.call_args.kwargs["ctx"] is shared_ctx

    def test_none_ctx_defaults_to_empty_dict(self) -> None:
        """When ctx is None, an empty dict is passed."""
        from arq.connections import RedisSettings

        from services.worker.worker import create_arq_worker

        async def dummy_task(_ctx: object) -> str:
            return "done"

        with self._default_patches(), patch("services.worker.worker.ArqWorker") as mock_aw_cls:
            create_arq_worker(
                queue_name="high",
                functions=[dummy_task],
                redis_settings=RedisSettings(host="localhost"),
                concurrency=4,
                timeout=300,
            )

            assert mock_aw_cls.call_args.kwargs["ctx"] == {}

    def test_cron_jobs_passed_through(self) -> None:
        """CronJob list is forwarded correctly."""
        from arq.connections import RedisSettings
        from arq.cron import cron

        from services.worker.worker import create_arq_worker

        async def dummy_task(_ctx: object) -> str:
            return "done"

        cron_job = cron(dummy_task, minute=0)

        with self._default_patches(), patch("services.worker.worker.ArqWorker") as mock_aw_cls:
            create_arq_worker(
                queue_name="low",
                functions=[dummy_task],
                redis_settings=RedisSettings(host="localhost"),
                concurrency=2,
                timeout=600,
                cron_jobs=[cron_job],
            )

            kwargs = mock_aw_cls.call_args.kwargs
            assert len(kwargs["cron_jobs"]) == 1
            assert kwargs["cron_jobs"][0] is cron_job

    def test_empty_cron_jobs_defaults_to_empty_list(self) -> None:
        """When cron_jobs is None, an empty list is passed."""
        from arq.connections import RedisSettings

        from services.worker.worker import create_arq_worker

        async def dummy_task(_ctx: object) -> str:
            return "done"

        with self._default_patches(), patch("services.worker.worker.ArqWorker") as mock_aw_cls:
            create_arq_worker(
                queue_name="high",
                functions=[dummy_task],
                redis_settings=RedisSettings(host="localhost"),
                concurrency=4,
                timeout=300,
            )

            assert mock_aw_cls.call_args.kwargs["cron_jobs"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# Queue depth monitoring
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestMonitorQueueDepth:
    """``monitor_queue_depth`` background coroutine."""

    # Helper: set shutdown=True after first loop iteration so the
    # coroutine returns normally.  _shutdown_requested starts False (run
    # the loop body) and patched asyncio.sleep flips it to True (exit).
    # ponyTail: mutable list avoids a dedicated flag class.
    @staticmethod
    async def _stop_after_first(*_: object) -> None:
        import services.worker.worker as _wmod

        _wmod._shutdown_requested = True

    @pytest.mark.asyncio
    async def test_sets_gauge_from_redis_zcard(self) -> None:
        """Queue depth from Redis zcard is written to Prometheus gauge."""
        _seed_worker_settings()
        mock_redis = AsyncMock()
        mock_redis.zcard.return_value = 42

        from services.worker.worker import monitor_queue_depth

        with (
            patch("services.worker.worker._shutdown_requested", False),
            patch("services.worker.worker.asyncio.sleep", self._stop_after_first),
            patch("services.worker.worker.worker_queue_depth.labels") as mock_labels,
        ):
            mock_gauge = MagicMock()
            mock_labels.return_value = mock_gauge

            await monitor_queue_depth(mock_redis, interval=999)

            mock_labels.assert_called()
            mock_gauge.set.assert_called_with(42)

    @pytest.mark.asyncio
    async def test_redis_error_returns_none_depth(self) -> None:
        """When zcard fails, the gauge is not updated but loop continues."""
        _seed_worker_settings()
        mock_redis = AsyncMock()
        mock_redis.zcard.side_effect = ConnectionError("Redis down")

        from services.worker.worker import monitor_queue_depth

        with (
            patch("services.worker.worker._shutdown_requested", False),
            patch("services.worker.worker.asyncio.sleep", self._stop_after_first),
            patch("services.worker.worker.worker_queue_depth.labels") as mock_labels,
        ):
            await monitor_queue_depth(mock_redis, interval=999)
            # labels are called (to get the label object), but .set() should
            # NOT be called because depth was None (error from zcard).
            mock_labels.return_value.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_checks_both_queue_names(self) -> None:
        """Both high and low queue names are monitored."""
        _seed_worker_settings()
        mock_redis = AsyncMock()
        mock_redis.zcard.return_value = 10

        from services.worker.worker import monitor_queue_depth

        with (
            patch("services.worker.worker._shutdown_requested", False),
            patch("services.worker.worker.asyncio.sleep", self._stop_after_first),
            patch("services.worker.worker.worker_queue_depth.labels") as mock_labels,
        ):
            await monitor_queue_depth(mock_redis, interval=999)

            assert mock_labels.call_count == 2

    @pytest.mark.asyncio
    async def test_loop_exits_when_shutdown_flag_set(self) -> None:
        """When _shutdown_requested is True, the loop body does not execute."""
        _seed_worker_settings()
        mock_redis = AsyncMock()

        from services.worker.worker import monitor_queue_depth

        with (
            patch("services.worker.worker._shutdown_requested", True),
            patch("services.worker.worker.worker_queue_depth"),
        ):
            await monitor_queue_depth(mock_redis, interval=999)

            # zcard should NOT be called since the loop never iterates
            mock_redis.zcard.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Health check endpoint
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestHealthCheck:
    """``health_check`` aiohttp endpoint."""

    @staticmethod
    def _make_app_with_pool(pool: object) -> "web.Application":
        """Create an aiohttp Application with a pre-configured redis_pool.

        aiohttp 3.x emits ``NotAppKeyWarning`` for string-keyed items.
        We suppress it since the production code also uses string keys
        (``request.app.get("redis_pool")``) and migrating to
        ``web.AppKey`` would break source-test consistency.
        """
        import warnings

        from aiohttp import web
        from aiohttp.web_exceptions import NotAppKeyWarning

        app = web.Application()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=NotAppKeyWarning)
            app["redis_pool"] = pool
        return app

    @pytest.mark.asyncio
    async def test_healthy_when_redis_pings(self) -> None:
        """Returns 200 with redis_connected=True."""
        from aiohttp import web

        from services.worker.worker import health_check

        pool = MagicMock()
        pool.execute_command = AsyncMock(return_value=b"PONG")

        app = self._make_app_with_pool(pool)

        request = MagicMock(spec=web.Request)
        request.app = app

        resp = await health_check(request)
        assert resp.status == 200
        body = resp.body if isinstance(resp.body, bytes) else str(resp.body).encode()
        assert b"ok" in body
        assert b"true" in body

    @pytest.mark.asyncio
    async def test_unhealthy_when_redis_fails(self) -> None:
        """Returns 503 when PING raises."""
        from aiohttp import web

        from services.worker.worker import health_check

        pool = MagicMock()
        pool.execute_command = AsyncMock(side_effect=OSError("connection refused"))

        app = self._make_app_with_pool(pool)

        request = MagicMock(spec=web.Request)
        request.app = app

        resp = await health_check(request)
        assert resp.status == 503
        assert b"unhealthy" in (resp.body if isinstance(resp.body, bytes) else str(resp.body).encode())

    @pytest.mark.asyncio
    async def test_unhealthy_when_no_redis_pool(self) -> None:
        """Returns 503 when redis_pool is missing from app context."""
        from aiohttp import web

        from services.worker.worker import health_check

        app = web.Application()
        request = MagicMock(spec=web.Request)
        request.app = app

        resp = await health_check(request)
        assert resp.status == 503
        assert b"No Redis pool" in resp.body


# ═══════════════════════════════════════════════════════════════════════════════
# Task registry
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestTaskRegistry:
    """HIGH_QUEUE_TASKS and LOW_QUEUE_TASKS registries."""

    def test_high_queue_has_expected_tasks(self) -> None:
        """High-priority queue contains real-time ingestion tasks."""
        from services.worker.worker import HIGH_QUEUE_TASKS

        names = {t.__name__ for t in HIGH_QUEUE_TASKS}
        # The 4 standalone LLM entry-points (classify_dialog, extract_entities,
        # extract_facts, extract_structured) were retired in favour of the
        # single enrich_episode pass.
        assert "enrich_episode" in names
        assert "embed_episode" in names
        assert "embed_fact" in names
        assert not ({"classify_dialog", "extract_entities",
                     "extract_facts", "extract_structured"} & names)

    def test_low_queue_has_expected_tasks(self) -> None:
        """Low-priority queue contains batch / scheduled tasks."""
        from services.worker.worker import LOW_QUEUE_TASKS

        names = {t.__name__ for t in LOW_QUEUE_TASKS}
        assert "link_entities_to_episode" in names
        assert "compute_observations" in names
        assert "summarise_community" in names
        assert "merge_duplicate_entities" in names
        assert "write_audit_log" in names
        assert "deliver_webhook" in names
        assert "generate_user_summary" in names
        assert "reconcile_enrichment" in names
        assert "expire_graph_edges" in names
        assert "reconcile_graph_edges" in names
        assert "cleanup_orphan_blobs" in names
        assert "extract_blob_text" in names

    def test_no_task_in_both_queues(self) -> None:
        """No task is registered in both high and low queues."""
        from services.worker.worker import HIGH_QUEUE_TASKS, LOW_QUEUE_TASKS

        high = {t.__name__ for t in HIGH_QUEUE_TASKS}
        low = {t.__name__ for t in LOW_QUEUE_TASKS}
        duplicates = high & low
        assert not duplicates, f"Tasks in both queues: {duplicates}"


# ═══════════════════════════════════════════════════════════════════════════════
# setup_logging
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestSetupLogging:
    """``setup_logging`` — structlog configuration."""

    def test_console_format_uses_console_renderer(self) -> None:
        """ConsoleRenderer is registered when STRUCTLOG_FORMAT=console."""
        _seed_worker_settings()
        from services.worker.worker import setup_logging

        with (
            patch("services.worker.worker.structlog.configure") as mock_cfg,
            patch("services.worker.worker.settings.STRUCTLOG_FORMAT", "console", create=True),
        ):
            setup_logging()

            processors = mock_cfg.call_args.kwargs["processors"]
            assert any("ConsoleRenderer" in str(p) for p in processors)

    def test_json_format_uses_json_renderer(self) -> None:
        """JSONRenderer is registered when STRUCTLOG_FORMAT=json."""
        _seed_worker_settings()
        from services.worker.worker import setup_logging

        with (
            patch("services.worker.worker.structlog.configure") as mock_cfg,
            patch("services.worker.worker.settings.STRUCTLOG_FORMAT", "json", create=True),
        ):
            setup_logging()

            processors = mock_cfg.call_args.kwargs["processors"]
            assert any("JSONRenderer" in str(p) for p in processors)


# ═══════════════════════════════════════════════════════════════════════════════
# main / entrypoint
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestMain:
    """``main`` async entrypoint — startup / shutdown lifecycle."""

    @pytest.mark.asyncio
    async def test_main_creates_two_worker_pools(self) -> None:
        """``main`` starts high and low priority workers."""
        from services.worker.worker import main

        _mocks = _patch_main_deps()
        try:
            with pytest.raises(asyncio.CancelledError):
                await main()

            assert _mocks["create_arq_worker"].call_count == 2
        finally:
            _unpatch_main_deps(_mocks)

    @pytest.mark.asyncio
    async def test_main_registers_signal_handlers(self) -> None:
        """SIGTERM and SIGINT handlers are registered."""
        from services.worker.worker import main

        _mocks = _patch_main_deps()
        try:
            with pytest.raises(asyncio.CancelledError):
                await main()

            add_handler = _mocks["loop"].add_signal_handler
            assert add_handler.call_count == 2
            sigs = [call[0][0] for call in add_handler.call_args_list]
            assert signal.SIGTERM in sigs
            assert signal.SIGINT in sigs
        finally:
            _unpatch_main_deps(_mocks)

    @pytest.mark.asyncio
    async def test_main_starts_prometheus_server(self) -> None:
        """Prometheus HTTP server is started on configured port."""
        from services.worker.worker import main

        _mocks = _patch_main_deps()
        try:
            with pytest.raises(asyncio.CancelledError):
                await main()

            _mocks["start_prometheus_server"].assert_called_once()
        finally:
            _unpatch_main_deps(_mocks)

    @pytest.mark.asyncio
    async def test_main_starts_health_server(self) -> None:
        """aiohttp health check server is started."""
        from services.worker.worker import main

        _mocks = _patch_main_deps()
        try:
            with pytest.raises(asyncio.CancelledError):
                await main()

            _mocks["site"].start.assert_awaited_once()
        finally:
            _unpatch_main_deps(_mocks)

    @pytest.mark.asyncio
    async def test_main_cleans_up_on_cancellation(self) -> None:
        """Resources are disposed in the finally block."""
        from services.worker.worker import main

        _mocks = _patch_main_deps()
        try:
            with pytest.raises(asyncio.CancelledError):
                await main()

            _mocks["db_engine"].dispose.assert_awaited_once()
            _mocks["surreal_pool"].close_all.assert_awaited_once()
            _mocks["runner"].cleanup.assert_awaited_once()
        finally:
            _unpatch_main_deps(_mocks)


@pytest.mark.unit
class TestEntrypoint:
    """``entrypoint`` synchronous wrapper."""

    def test_runs_main_via_asyncio_run(self) -> None:
        """``entrypoint`` calls ``asyncio.run(main())``."""
        from services.worker.worker import entrypoint

        with patch("services.worker.worker.asyncio.run") as mock_run:
            entrypoint()
            mock_run.assert_called_once()

    def test_handles_keyboard_interrupt(self) -> None:
        """KeyboardInterrupt in asyncio.run is caught and logged."""
        from services.worker.worker import entrypoint

        with (
            patch("services.worker.worker.asyncio.run", side_effect=KeyboardInterrupt),
            patch("services.worker.worker.logger.info") as mock_log,
        ):
            entrypoint()
            mock_log.assert_called_once_with("worker.keyboard_interrupt")


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _seed_worker_settings() -> None:
    """Initialise the ``WorkerSettings`` singleton so ``settings.X`` access works.

    ``worker.py`` imports ``from services.worker import worker_settings as settings``,
    and ``worker_settings`` has a module-level ``__getattr__`` that proxies to
    ``get_worker_settings()``, which raises if ``_settings`` is ``None``.

    Call this before any test that touches ``settings.*`` — including
    ``create_arq_worker``, ``monitor_queue_depth``, ``setup_logging``, and
    any code path that indirectly reads settings.
    """
    from services.worker import worker_settings as _ws
    from services.worker.worker_settings import WorkerSettings

    _ws._settings = WorkerSettings(
        DATABASE_URL="postgresql+asyncpg://localhost:5432/test",
        REDIS_URL="redis://localhost:6379/0",
        ENV="test",
    )


def _patch_main_deps() -> dict:
    """Patch all dependencies of ``main()`` and return mock objects keyed by name.

    Uses correct targets for lazy imports inside ``main()``:
    - ``core.db.init_db_engine`` / ``core.db.get_async_session`` (lazy, not in worker module)
    - ``core.graph_backend.init_dispatcher`` (lazy)
    - ``core.surreal_pool.SurrealConnectionPool`` (lazy)
    - ``services.worker.worker.BootstrapSettings`` / ``OpenBaoClient`` (module-level)
    """
    import core.db as _cdb
    import core.graph_backend as _cgb
    import core.surreal_pool as _csp

    # ── Seed WorkerSettings singleton ────────────────────────────────────
    # ``main()`` reads ``settings.MAX_WORKERS``, ``settings.REDIS_DSN``,
    # and other values directly.  Without this seed every ``settings.X``
    # access hits ``get_worker_settings()`` → ``RuntimeError``.
    _seed_worker_settings()

    mocks: dict = {}

    # Every patcher is appended here so ``_unpatch_main_deps`` can stop them.
    _patchers: list[patch] = []

    # ── BootstrapSettings (module-level import in worker.py) ─────────────
    _bs = MagicMock()
    _bs.OPENBAO_WORKER_ROLE_ID = None
    _bs.OPENBAO_ROLE_ID = "role"
    _bs.OPENBAO_WORKER_SECRET_ID = None
    _bs.OPENBAO_SECRET_ID = "secret"
    _bs.OPENBAO_ADDR = "http://localhost:8200"

    bs_patch = patch("services.worker.worker.BootstrapSettings", return_value=_bs)
    bs_patch.start()
    _patchers.append(bs_patch)
    mocks["BootstrapSettings"] = _bs

    # ── OpenBaoClient (module-level import) ──────────────────────────────
    _bao_cls = MagicMock()
    _bao = AsyncMock()
    _bao_cls.return_value.__aenter__.return_value = _bao
    bao_patch = patch("services.worker.worker.OpenBaoClient", _bao_cls)
    bao_patch.start()
    _patchers.append(bao_patch)
    mocks["bao_client"] = _bao
    mocks["OpenBaoClient"] = _bao_cls

    # ── init_worker_settings_from_bao / init_settings ────────────────────
    iws_patch = patch("services.worker.worker.init_worker_settings_from_bao")
    iws_patch.start()
    _patchers.append(iws_patch)
    is_patch = patch("services.worker.worker.init_settings")
    is_patch.start()
    _patchers.append(is_patch)

    # ── setup_logging ────────────────────────────────────────────────────
    sl_patch = patch("services.worker.worker.setup_logging")
    sl_patch.start()
    _patchers.append(sl_patch)

    # ── Prometheus server ────────────────────────────────────────────────
    _prom = MagicMock()
    ps_patch = patch("services.worker.worker.start_prometheus_server", _prom)
    ps_patch.start()
    _patchers.append(ps_patch)
    mocks["start_prometheus_server"] = _prom

    # ── RedisSettings.from_dsn ───────────────────────────────────────────
    rs_patch = patch("services.worker.worker.RedisSettings.from_dsn")
    rs_patch.start()
    _patchers.append(rs_patch)

    # ── Lazy imports inside main() — patch the SOURCE module, not worker ─
    # ``from core.db import init_db_engine, get_async_session`` inside main()
    _cdb_init_db_engine_orig = _cdb.init_db_engine
    _cdb_get_async_session_orig = _cdb.get_async_session
    _engine = MagicMock()
    _engine.dispose = AsyncMock()
    _cdb.init_db_engine = MagicMock(return_value=_engine)
    mocks["db_engine"] = _engine
    _cdb.get_async_session = MagicMock()

    # ``from core.graph_backend import init_dispatcher`` inside main()
    _cgb_init_dispatcher_orig = _cgb.init_dispatcher
    _cgb.init_dispatcher = MagicMock()

    # ``from core.surreal_pool import SurrealConnectionPool`` inside main()
    _csp_surreal_pool_orig = _csp.SurrealConnectionPool
    _sp = MagicMock()
    _sp.close_all = AsyncMock()
    _csp.SurrealConnectionPool = MagicMock(return_value=_sp)
    mocks["surreal_pool"] = _sp

    mocks["_cdb_restore"] = _cdb
    mocks["_cgb_restore"] = _cgb
    mocks["_csp_restore"] = _csp
    mocks["_cdb_init_db_engine_orig"] = _cdb_init_db_engine_orig
    mocks["_cdb_get_async_session_orig"] = _cdb_get_async_session_orig
    mocks["_cgb_init_dispatcher_orig"] = _cgb_init_dispatcher_orig
    mocks["_csp_surreal_pool_orig"] = _csp_surreal_pool_orig

    # ── FalkorDB / BlockingConnectionPool — lazy import in try/except ────
    # falkordb IS installed in the test env, so the lazy import succeeds
    # and ``BlockingConnectionPool.from_url(settings.FALKORDB_URL)`` runs
    # (FALKORDB_URL is None → ValueError).  Patch ``from_url`` to raise
    # ``ImportError`` so the try/except gracefully falls back to None.
    import redis.asyncio as _redis_asyncio

    _bcp_orig = _redis_asyncio.BlockingConnectionPool  # save for restore
    _bcp_mock = MagicMock()
    _bcp_mock.from_url = MagicMock(side_effect=ImportError("test env — falkordb disabled"))
    _redis_asyncio.BlockingConnectionPool = _bcp_mock  # type: ignore[assignment]
    mocks["_bcp_orig"] = _bcp_orig

    # ═══════════════════════════════════════════════════════════════════
    # IMPORTANT: The patches below use ``patch("services.worker.worker.web.X")``
    # which resolves through the module and patches ``aiohttp.web.X`` globally.
    # Callers MUST ``_unpatch_main_deps(mocks)`` after the test to avoid
    # leaking global replacements of ``web.Application`` / ``AppRunner`` /
    # ``TCPSite`` into subsequent tests (especially HealthCheck).
    # ═══════════════════════════════════════════════════════════════════

    # ── get_queue_name (via settings proxy) ──────────────────────────────
    gqn_patch = patch(
        "services.worker.worker.get_queue_name",
        return_value="OpenZync:test:queue:high",
    )
    gqn_patch.start()
    _patchers.append(gqn_patch)

    # ── create_arq_worker ───────────────────────────────────────────────
    # ``main()`` awaits ``asyncio.gather(high_worker.async_run(), ...)``
    # inside a ``try/except asyncio.CancelledError``.  The mock workers'
    # ``async_run`` must raise ``CancelledError`` so the except clause
    # re-raises it and the test can assert ``pytest.raises(CancelledError)``.
    _high = MagicMock()
    _high.async_run = AsyncMock(side_effect=asyncio.CancelledError())
    _high.pool = MagicMock()
    _low = MagicMock()
    _low.async_run = AsyncMock(side_effect=asyncio.CancelledError())
    _low.pool = MagicMock()

    _cw = MagicMock()
    _cw.side_effect = [_high, _low]

    cw_patch = patch("services.worker.worker.create_arq_worker", _cw)
    cw_patch.start()
    _patchers.append(cw_patch)
    mocks["create_arq_worker"] = _cw
    mocks["high_worker"] = _high
    mocks["low_worker"] = _low

    # ── aiohttp web.Application, AppRunner, TCPSite ──────────────────────
    # ⚠️ These patch ``aiohttp.web.X`` globally (see note above).
    _app = MagicMock()

    def _make_app():
        return _app

    app_patch = patch("services.worker.worker.web.Application", _make_app)
    app_patch.start()
    _patchers.append(app_patch)
    mocks["health_app"] = _app

    _runner = MagicMock()
    _runner.setup = AsyncMock()
    _runner.cleanup = AsyncMock()
    runner_patch = patch("services.worker.worker.web.AppRunner", return_value=_runner)
    runner_patch.start()
    _patchers.append(runner_patch)
    mocks["runner"] = _runner

    _site = MagicMock()
    _site.start = AsyncMock()
    site_patch = patch("services.worker.worker.web.TCPSite", return_value=_site)
    site_patch.start()
    _patchers.append(site_patch)
    mocks["site"] = _site

    # ── monitor_queue_depth ─────────────────────────────────────────────
    mqd_patch = patch("services.worker.worker.monitor_queue_depth")
    mqd_patch.start()
    _patchers.append(mqd_patch)

    # ── asyncio.get_running_loop ─────────────────────────────────────────
    _loop = MagicMock()
    loop_patch = patch("services.worker.worker.asyncio.get_running_loop", return_value=_loop)
    loop_patch.start()
    _patchers.append(loop_patch)
    mocks["loop"] = _loop

    mocks["_patchers"] = _patchers
    return mocks


def _unpatch_main_deps(mocks: dict) -> None:
    """Stop all patches started by :func:`_patch_main_deps`.

    Must be called (e.g. in a ``try/finally``) to restore globally-patched
    modules such as ``aiohttp.web.Application``, ``AppRunner``, and
    ``TCPSite``, as well as direct attribute overrides on ``core.db``,
    ``core.graph_backend``, and ``core.surreal_pool``.
    """
    for p in reversed(mocks.get("_patchers", [])):
        p.stop()
    # Restore direct attribute assignments (not managed by ``patch``).
    import redis.asyncio as _redis_asyncio

    if "_bcp_orig" in mocks:
        _redis_asyncio.BlockingConnectionPool = mocks["_bcp_orig"]
    if "_cdb_restore" in mocks:
        mocks["_cdb_restore"].init_db_engine = mocks["_cdb_init_db_engine_orig"]
        mocks["_cdb_restore"].get_async_session = mocks["_cdb_get_async_session_orig"]
    if "_cgb_restore" in mocks:
        mocks["_cgb_restore"].init_dispatcher = mocks["_cgb_init_dispatcher_orig"]
    if "_csp_restore" in mocks:
        mocks["_csp_restore"].SurrealConnectionPool = mocks["_csp_surreal_pool_orig"]
