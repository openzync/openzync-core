"""Unit tests for ARQ worker entrypoint — metrics, health, lifecycle.

Tests cover:
- Prometheus metric creation and recording on job end
- Queue depth monitoring loop (``while True``, ARQ-cancel exit)
- aiohttp health-check endpoint
- ``create_arq_worker`` factory function
- Job lifecycle callbacks (``on_job_end``, ``on_shutdown``)
- Task registry completeness and deduplication
- ``main`` entrypoint startup / shutdown lifecycle
- ``entrypoint`` synchronous wrapper
- ``setup_logging`` structlog configuration

Note: custom SIGTERM handling was removed — ARQ owns signals
(``Worker.async_run`` installs its own handlers), so there are
intentionally no signal-handler tests.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

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

            mock_total.labels.assert_called_once_with(
                task_type="enrich", status="success"
            )
            mock_total.labels.return_value.inc.assert_called_once()

            mock_dur.labels.assert_called_once_with(task_type="enrich")
            mock_dur.labels.return_value.observe.assert_called_once_with(3.5)

            mock_org.labels.assert_called_once_with(
                org_id="org-1",
                task_type="enrich",
                status="success",
            )
            mock_org.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_defaults(self) -> None:
        """Missing ctx fields fall back to 'unknown'."""
        from services.worker.worker import on_job_end

        with (
            patch("services.worker.worker.worker_tasks_total") as mock_total,
            patch("services.worker.worker.worker_task_duration_seconds"),
            patch("services.worker.worker.worker_tasks_per_org") as mock_org,
        ):
            await on_job_end({})

            mock_total.labels.assert_called_once_with(
                task_type="unknown", status="success"
            )
            mock_org.labels.assert_called_once_with(
                org_id="unknown",
                task_type="unknown",
                status="success",
            )

    @pytest.mark.asyncio
    async def test_failure_status_increments_the_failure_counter(self) -> None:
        """A ctx carrying ``status="failure"`` is counted as a failure.

        ``on_job_end`` used to hard-code ``status="success"`` — every
        failed job was reported as a success.
        """
        from services.worker.worker import on_job_end

        ctx: dict = {
            "job_id": "j-1",
            "task_type": "deliver_webhook",
            "org_id": "org-1",
            "trace_id": "t-1",
            "runtime": 1.25,
            "status": "failure",
        }

        with (
            patch("services.worker.worker.worker_tasks_total") as mock_total,
            patch("services.worker.worker.worker_task_duration_seconds"),
            patch("services.worker.worker.worker_tasks_per_org") as mock_org,
        ):
            await on_job_end(ctx)

            mock_total.labels.assert_called_once_with(
                task_type="deliver_webhook",
                status="failure",
            )
            mock_total.labels.return_value.inc.assert_called_once()
            mock_org.labels.assert_called_once_with(
                org_id="org-1",
                task_type="deliver_webhook",
                status="failure",
            )
            mock_org.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_success_status_increments_the_success_counter(self) -> None:
        """A ctx carrying ``status="success"`` is counted as a success."""
        from services.worker.worker import on_job_end

        ctx: dict = {
            "job_id": "j-1",
            "task_type": "embed_episode",
            "org_id": "org-1",
            "runtime": 0.5,
            "status": "success",
        }

        with (
            patch("services.worker.worker.worker_tasks_total") as mock_total,
            patch("services.worker.worker.worker_task_duration_seconds"),
            patch("services.worker.worker.worker_tasks_per_org") as mock_org,
        ):
            await on_job_end(ctx)

            mock_total.labels.assert_called_once_with(
                task_type="embed_episode",
                status="success",
            )
            mock_org.labels.assert_called_once_with(
                org_id="org-1",
                task_type="embed_episode",
                status="success",
            )

    @pytest.mark.asyncio
    async def test_task_type_and_org_id_are_read_from_the_ctx(self) -> None:
        """The task type and org id come from the ctx, not from a default.

        ``with_task_metrics`` writes them onto the shared ARQ ctx dict;
        ``on_job_end`` must read them back instead of reporting every job
        as ``task_type="unknown"`` / ``org_id="unknown"``.
        """
        from services.worker.worker import on_job_end

        ctx: dict = {"task_type": "deliver_webhook", "org_id": "org-42"}

        with (
            patch("services.worker.worker.worker_tasks_total") as mock_total,
            patch("services.worker.worker.worker_task_duration_seconds"),
            patch("services.worker.worker.worker_tasks_per_org") as mock_org,
        ):
            await on_job_end(ctx)

            mock_total.labels.assert_called_once_with(
                task_type="deliver_webhook",
                status="success",
            )
            mock_org.labels.assert_called_once_with(
                org_id="org-42",
                task_type="deliver_webhook",
                status="success",
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

        with (
            self._default_patches(),
            patch("services.worker.worker.ArqWorker") as mock_aw_cls,
        ):
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

        with (
            self._default_patches(),
            patch("services.worker.worker.ArqWorker") as mock_aw_cls,
        ):
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

        with (
            self._default_patches(),
            patch("services.worker.worker.ArqWorker") as mock_aw_cls,
        ):
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

        with (
            self._default_patches(),
            patch("services.worker.worker.ArqWorker") as mock_aw_cls,
        ):
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

        with (
            self._default_patches(),
            patch("services.worker.worker.ArqWorker") as mock_aw_cls,
        ):
            create_arq_worker(
                queue_name="high",
                functions=[dummy_task],
                redis_settings=RedisSettings(host="localhost"),
                concurrency=4,
                timeout=300,
            )

            assert mock_aw_cls.call_args.kwargs["cron_jobs"] == []

    def test_worker_max_tries_is_one(self) -> None:
        """The worker itself never retries a job.

        Every task owns its retry policy through the explicit
        ``@with_retry`` decorator; ARQ's default of 5 would stack on top
        of it (and on the LLM backend's own retries).
        """
        from arq.connections import RedisSettings

        from services.worker.worker import create_arq_worker

        async def dummy_task(_ctx: object) -> str:
            return "done"

        with (
            self._default_patches(),
            patch("services.worker.worker.ArqWorker") as mock_aw_cls,
        ):
            create_arq_worker(
                queue_name="high",
                functions=[dummy_task],
                redis_settings=RedisSettings(host="localhost"),
                concurrency=4,
                timeout=300,
            )

            assert mock_aw_cls.call_args.kwargs["max_tries"] == 1

    def test_deliver_webhook_is_registered_with_a_higher_max_tries(self) -> None:
        """``deliver_webhook`` keeps ARQ-level retries.

        It is the only task that drives its backoff through ARQ's
        ``Retry``, so it must be registered with a per-function
        ``max_tries`` — otherwise the webhook retries stop working.
        """
        from arq.connections import RedisSettings

        from services.worker.tasks.deliver_webhook import MAX_ATTEMPTS, deliver_webhook
        from services.worker.worker import create_arq_worker

        with (
            self._default_patches(),
            patch("services.worker.worker.ArqWorker") as mock_aw_cls,
        ):
            create_arq_worker(
                queue_name="low",
                functions=[deliver_webhook],
                redis_settings=RedisSettings(host="localhost"),
                concurrency=4,
                timeout=300,
            )

            functions = mock_aw_cls.call_args.kwargs["functions"]
            webhook = next(f for f in functions if f.name == "deliver_webhook")
            assert webhook.max_tries == MAX_ATTEMPTS
            assert webhook.max_tries > 1

    def test_other_tasks_inherit_the_worker_max_tries(self) -> None:
        """Tasks without an override carry ``max_tries=None``.

        ``None`` makes ARQ fall back to the worker's own ``max_tries=1``.
        """
        from arq.connections import RedisSettings

        from services.worker.tasks.deliver_webhook import deliver_webhook
        from services.worker.worker import create_arq_worker

        async def dummy_task(_ctx: object) -> str:
            return "done"

        with (
            self._default_patches(),
            patch("services.worker.worker.ArqWorker") as mock_aw_cls,
        ):
            create_arq_worker(
                queue_name="low",
                functions=[dummy_task, deliver_webhook],
                redis_settings=RedisSettings(host="localhost"),
                concurrency=4,
                timeout=300,
            )

            functions = mock_aw_cls.call_args.kwargs["functions"]
            others = [f for f in functions if f.name != "deliver_webhook"]
            assert others
            for fn in others:
                assert fn.max_tries is None


# ═══════════════════════════════════════════════════════════════════════════════
# RLS-scoped session factory
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestRlsScopedSessionFactory:
    """``rls_scoped_session_factory`` — the worker pool fails closed.

    The worker shares one connection pool across every task.  The tenant
    context is bound **transaction-locally**, so it is discarded when the
    transaction ends and can never stand on a pooled connection.  Opening
    every session with an **empty** tenant still matters: a task that
    forgets to bind its tenant sees no rows at all instead of another
    org's rows.
    """

    @pytest.mark.asyncio
    async def test_session_carries_an_empty_tenant(self) -> None:
        """The tenant is initialised to '' — not inherited from a prior task."""
        from services.worker.worker import rls_scoped_session_factory

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.__aexit__.return_value = None
        factory = MagicMock()
        factory.return_value = session

        scoped = rls_scoped_session_factory(factory)

        async with scoped() as opened:
            assert opened is session

        calls = session.execute.await_args_list
        assert len(calls) == 1
        sql, params = calls[0].args[0], calls[0].args[1]
        # One statement, transaction-local (``is_local => true``): the
        # context cannot outlive the transaction and cannot be carried
        # onto the connection the next task picks up.
        assert str(sql) == (
            "SELECT set_config('app.org_id', :org_id, true), "
            "set_config('app.bypass_rls', :bypass_rls, true), "
            "set_config('app.lookup_code', :lookup_code, true)"
        )
        # An empty tenant — the pool fails closed instead of leaking the
        # previous task's org into this one.
        assert params == {
            "org_id": "",
            # The bypass is pinned off and any stale lookup code is cleared.
            "bypass_rls": "false",
            "lookup_code": "",
        }

    @pytest.mark.asyncio
    async def test_every_session_is_initialised(self) -> None:
        """A second session from the same factory is initialised too."""
        from services.worker.worker import rls_scoped_session_factory

        session = AsyncMock()
        session.__aenter__.return_value = session
        session.__aexit__.return_value = None
        factory = MagicMock()
        factory.return_value = session

        scoped = rls_scoped_session_factory(factory)

        async with scoped():
            pass
        async with scoped():
            pass

        assert session.execute.await_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Queue depth monitoring
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestMonitorQueueDepth:
    """``monitor_queue_depth`` background coroutine.

    The loop is ``while True`` — ARQ's shutdown cancels the task, so each
    test runs one iteration (real ``asyncio.sleep`` patched to a finite
    wait) and then cancels, asserting ``CancelledError`` propagates.
    """

    @pytest.mark.asyncio
    async def test_sets_gauge_from_redis_zcard(self) -> None:
        """Queue depth from Redis zcard is written to Prometheus gauge."""
        _seed_worker_settings()
        mock_redis = AsyncMock()
        mock_redis.zcard.return_value = 42

        from services.worker.worker import monitor_queue_depth

        with (
            patch(
                "services.worker.worker.asyncio.sleep",
                side_effect=asyncio.CancelledError(),
            ),
            patch("services.worker.worker.worker_queue_depth.labels") as mock_labels,
        ):
            mock_gauge = MagicMock()
            mock_labels.return_value = mock_gauge

            with pytest.raises(asyncio.CancelledError):
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
            patch(
                "services.worker.worker.asyncio.sleep",
                side_effect=asyncio.CancelledError(),
            ),
            patch("services.worker.worker.worker_queue_depth.labels") as mock_labels,
        ):
            with pytest.raises(asyncio.CancelledError):
                await monitor_queue_depth(mock_redis, interval=999)
            # depth was None (error from zcard) → .set() never called.
            mock_labels.return_value.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_checks_both_queue_names(self) -> None:
        """Both high and low queue names are monitored."""
        _seed_worker_settings()
        mock_redis = AsyncMock()
        mock_redis.zcard.return_value = 10

        from services.worker.worker import monitor_queue_depth

        with (
            patch(
                "services.worker.worker.asyncio.sleep",
                side_effect=asyncio.CancelledError(),
            ),
            patch("services.worker.worker.worker_queue_depth.labels") as mock_labels,
        ):
            with pytest.raises(asyncio.CancelledError):
                await monitor_queue_depth(mock_redis, interval=999)

            assert mock_labels.call_count == 2

    @pytest.mark.asyncio
    async def test_cancel_exits_loop(self) -> None:
        """ARQ task cancellation propagates out of the sleep (ends the loop)."""
        _seed_worker_settings()
        mock_redis = AsyncMock()
        mock_redis.zcard.return_value = 7

        from services.worker.worker import monitor_queue_depth

        task = asyncio.create_task(monitor_queue_depth(mock_redis, interval=3600))
        await asyncio.sleep(0)  # let one sampling iteration run
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert mock_redis.zcard.await_count == 2  # high + low queues sampled

    @pytest.mark.asyncio
    async def test_zcard_uses_the_bare_queue_name(self) -> None:
        """Queue depth is read from the bare queue name.

        ARQ stores pending jobs in a sorted set named exactly the queue
        name (``arq/connections.py``: ``pipe.zadd(_queue_name, ...)``).
        The old ``f"{queue_name}:jobs"`` key never existed, so the gauge
        stayed at zero forever.
        """
        _seed_worker_settings()
        mock_redis = AsyncMock()
        mock_redis.zcard.return_value = 3

        from services.worker.worker import monitor_queue_depth

        with (
            patch(
                "services.worker.worker.asyncio.sleep",
                side_effect=asyncio.CancelledError(),
            ),
            patch("services.worker.worker.worker_queue_depth.labels"),
            pytest.raises(asyncio.CancelledError),
        ):
            await monitor_queue_depth(mock_redis, interval=999)

        called_with = [call.args[0] for call in mock_redis.zcard.await_args_list]
        assert called_with == [
            "OpenZync:test:queue:high",
            "OpenZync:test:queue:low",
        ]
        # The old suffixed key would never match ARQ's pending-job set.
        assert all(":jobs" not in name for name in called_with)


# ═══════════════════════════════════════════════════════════════════════════════
# Health check endpoint
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestHealthCheck:
    """``health_check`` readiness endpoint (``/ready``) + ``liveness_check``.

    Contract: ``/health`` is liveness-only (always 200); ``/ready`` proves
    Redis PING + Postgres ``SELECT 1`` + Ollama ``/api/tags`` and returns
    200 ``{"status": "ok", "checks": {...}}`` or 503 ``degraded`` with
    per-check booleans.
    """

    @staticmethod
    def _checks(resp: web.Response) -> dict:
        """Parse the JSON probe body."""
        import json as _json

        return _json.loads(resp.text)

    @staticmethod
    def _make_app_with_pool(pool: object) -> web.Application:
        """Create an aiohttp Application with a stub worker holding the pool.

        aiohttp 3.x emits ``NotAppKeyWarning`` for string-keyed items.
        We suppress it since the production code also uses string keys
        (``request.app.get("high_worker")``) and migrating to
        ``web.AppKey`` would break source-test consistency.
        """
        import warnings

        from aiohttp import web
        from aiohttp.web_exceptions import NotAppKeyWarning

        app = web.Application()
        worker = MagicMock()
        worker.pool = pool
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=NotAppKeyWarning)
            app["high_worker"] = worker
        return app

    @staticmethod
    def _set_key(app: web.Application, key: str, value: object) -> None:
        """Assign a string key under NotAppKeyWarning suppression (see above)."""
        import warnings

        from aiohttp.web_exceptions import NotAppKeyWarning

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=NotAppKeyWarning)
            app[key] = value  # type: ignore[index]

    @staticmethod
    def _make_app_with_worker(worker: object):
        """Create an aiohttp Application holding a pre-built worker stub."""
        import warnings

        from aiohttp import web
        from aiohttp.web_exceptions import NotAppKeyWarning

        app = web.Application()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=NotAppKeyWarning)
            app["high_worker"] = worker
        return app

    @pytest.mark.asyncio
    async def test_healthy_when_redis_pings(self) -> None:
        """All three probes pass → 200 with per-check booleans."""
        from services.worker.worker import health_check

        pool = MagicMock()
        pool.execute_command = AsyncMock(return_value=b"PONG")

        app = self._make_app_with_pool(pool)
        self._set_key(app, "db_engine", MagicMock())

        request = MagicMock(spec=web.Request)
        request.app = app

        with (
            patch("services.worker.worker._check_db", new=AsyncMock(return_value=True)),
            patch("services.worker.worker._check_ollama", new=AsyncMock(return_value=True)),
        ):
            resp = await health_check(request)
        assert resp.status == 200
        assert self._checks(resp) == {
            "status": "ok",
            "checks": {"redis": True, "database": True, "ollama": True},
        }

    @pytest.mark.asyncio
    async def test_unhealthy_when_redis_fails(self) -> None:
        """Redis PING raises → 503 degraded, redis=False, rest reported."""
        from services.worker.worker import health_check

        pool = MagicMock()
        pool.execute_command = AsyncMock(side_effect=OSError("connection refused"))

        app = self._make_app_with_pool(pool)
        self._set_key(app, "db_engine", MagicMock())

        request = MagicMock(spec=web.Request)
        request.app = app

        with (
            patch("services.worker.worker._check_db", new=AsyncMock(return_value=True)),
            patch("services.worker.worker._check_ollama", new=AsyncMock(return_value=True)),
        ):
            resp = await health_check(request)
        assert resp.status == 503
        assert self._checks(resp) == {
            "status": "degraded",
            "checks": {"redis": False, "database": True, "ollama": True},
        }

    @pytest.mark.asyncio
    async def test_unhealthy_when_pool_not_ready_and_redis_down(self) -> None:
        """No pool yet + fresh PING raises → 503 degraded, redis=False."""
        from services.worker.worker import health_check

        _seed_worker_settings()

        app = web.Application()
        self._set_key(app, "db_engine", MagicMock())
        request = MagicMock(spec=web.Request)
        request.app = app

        with (
            patch(
                "services.worker.worker.create_pool",
                new=AsyncMock(side_effect=OSError("connection refused")),
            ),
            patch("services.worker.worker._check_db", new=AsyncMock(return_value=True)),
            patch("services.worker.worker._check_ollama", new=AsyncMock(return_value=True)),
        ):
            resp = await health_check(request)
        assert resp.status == 503
        assert self._checks(resp) == {
            "status": "degraded",
            "checks": {"redis": False, "database": True, "ollama": True},
        }

    @pytest.mark.asyncio
    async def test_healthy_via_fresh_client_when_pool_not_ready(self) -> None:
        """worker.pool None + fresh PING ok (+ db/ollama ok) → 200."""
        from services.worker.worker import health_check

        _seed_worker_settings()

        fresh = MagicMock()
        fresh.execute_command = AsyncMock(return_value=b"PONG")
        fresh.close = AsyncMock()

        worker = MagicMock()
        worker.pool = None
        app = self._make_app_with_worker(worker)
        self._set_key(app, "db_engine", MagicMock())

        request = MagicMock(spec=web.Request)
        request.app = app

        with (
            patch(
                "services.worker.worker.create_pool",
                new=AsyncMock(return_value=fresh),
            ),
            patch("services.worker.worker._check_db", new=AsyncMock(return_value=True)),
            patch("services.worker.worker._check_ollama", new=AsyncMock(return_value=True)),
        ):
            resp = await health_check(request)
        assert resp.status == 200
        assert self._checks(resp)["status"] == "ok"
        fresh.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reads_pool_live_from_worker(self) -> None:
        """Pool assigned after app creation is picked up (no startup snap)."""
        from services.worker.worker import health_check

        pool = MagicMock()
        pool.execute_command = AsyncMock(return_value=b"PONG")

        worker = MagicMock()
        worker.pool = None
        app = self._make_app_with_worker(worker)
        self._set_key(app, "db_engine", MagicMock())
        # ARQ assigns worker.pool in async_run(), after the health server
        # starts — mutate after app creation to prove the lookup is live.
        worker.pool = pool

        request = MagicMock(spec=web.Request)
        request.app = app

        with (
            patch(
                "services.worker.worker.create_pool",
                new=AsyncMock(),
            ) as mock_create_pool,
            patch("services.worker.worker._check_db", new=AsyncMock(return_value=True)),
            patch("services.worker.worker._check_ollama", new=AsyncMock(return_value=True)),
        ):
            resp = await health_check(request)
        assert resp.status == 200
        # Live pool was used — no fresh-client fallback.
        mock_create_pool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_worker_key_falls_back_to_fresh_client(self) -> None:
        """No ``high_worker`` key at all (pre-startup) → fresh PING → 200."""
        from services.worker.worker import health_check

        _seed_worker_settings()

        fresh = MagicMock()
        fresh.execute_command = AsyncMock(return_value=b"PONG")
        fresh.close = AsyncMock()

        app = web.Application()  # no "high_worker" — server up, workers not built
        self._set_key(app, "db_engine", MagicMock())
        request = MagicMock(spec=web.Request)
        request.app = app

        with (
            patch(
                "services.worker.worker.create_pool",
                new=AsyncMock(return_value=fresh),
            ) as mock_create_pool,
            patch("services.worker.worker._check_db", new=AsyncMock(return_value=True)),
            patch("services.worker.worker._check_ollama", new=AsyncMock(return_value=True)),
        ):
            resp = await health_check(request)

        assert resp.status == 200
        assert self._checks(resp)["status"] == "ok"
        mock_create_pool.assert_awaited_once()
        fresh.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fresh_client_closed_when_ping_fails(self) -> None:
        """Fresh client is closed even when its PING raises (``finally``)."""
        from services.worker.worker import health_check

        _seed_worker_settings()

        fresh = MagicMock()
        fresh.execute_command = AsyncMock(side_effect=OSError("down"))
        fresh.close = AsyncMock()

        worker = MagicMock()
        worker.pool = None
        app = self._make_app_with_worker(worker)
        self._set_key(app, "db_engine", MagicMock())

        request = MagicMock(spec=web.Request)
        request.app = app

        with (
            patch(
                "services.worker.worker.create_pool",
                new=AsyncMock(return_value=fresh),
            ),
            patch("services.worker.worker._check_db", new=AsyncMock(return_value=True)),
            patch("services.worker.worker._check_ollama", new=AsyncMock(return_value=True)),
        ):
            resp = await health_check(request)

        assert resp.status == 503
        assert self._checks(resp)["checks"]["redis"] is False
        fresh.close.assert_awaited_once()


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
        assert not (
            {
                "classify_dialog",
                "extract_entities",
                "extract_facts",
                "extract_structured",
            }
            & names
        )

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
            patch(
                "services.worker.worker.settings.STRUCTLOG_FORMAT",
                "console",
                create=True,
            ),
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
            patch(
                "services.worker.worker.settings.STRUCTLOG_FORMAT", "json", create=True
            ),
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
            _mocks["prewarm_embeddings"].assert_awaited_once()
            _mocks["init_arq"].assert_awaited_once()
        finally:
            _unpatch_main_deps(_mocks)

    @pytest.mark.asyncio
    async def test_main_registers_no_custom_signal_handlers(self) -> None:
        """No custom SIGTERM/SIGINT handlers are registered (ARQ owns signals)."""
        from services.worker.worker import main

        _mocks = _patch_main_deps()
        try:
            loop = asyncio.get_running_loop()
            with (
                patch.object(loop, "add_signal_handler") as mock_add_handler,
                pytest.raises(asyncio.CancelledError),
            ):
                await main()

            mock_add_handler.assert_not_called()
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

    @pytest.mark.asyncio
    async def test_main_registers_orphaned_blob_cleanup_cron(self) -> None:
        """Daily 03:00 UTC cleanup_orphan_blobs cron reaches the low worker.

        Guards the shipped fix batch: the orphaned-blob cleanup cron must
        be passed to the low-priority ``create_arq_worker`` call, wrapped
        with the real ``cleanup_orphan_blobs`` coroutine and the correct
        schedule (hour=3, minute=0 — installed arq stores ints, not sets).
        """
        from services.worker.worker import main
        from workers.tasks.cleanup_orphan_blobs import cleanup_orphan_blobs

        _mocks = _patch_main_deps()
        try:
            with pytest.raises(asyncio.CancelledError):
                await main()

            calls = _mocks["create_arq_worker"].call_args_list
            assert len(calls) == 2  # high first, low second
            cron_jobs = calls[1].kwargs["cron_jobs"]

            matches = [
                job
                for job in cron_jobs
                if getattr(job, "job_id", None) == "orphaned_blob_cleanup"
            ]
            assert len(matches) == 1
            job = matches[0]
            assert job.coroutine is cleanup_orphan_blobs
            assert job.hour == 3
            assert job.minute == 0
        finally:
            _unpatch_main_deps(_mocks)

    @pytest.mark.asyncio
    async def test_main_registers_nightly_community_detection_cron(self) -> None:
        """Nightly 02:00 UTC summarise_community cron reaches the low worker.

        Guards the community-detection cron registration: it must be passed
        to the low-priority ``create_arq_worker`` call, wrapped with the
        real ``summarise_community`` coroutine, job_id
        ``nightly_community_detection``, and schedule (hour=2, minute=0 —
        installed arq stores ints, not sets).

        NOTE: this cron only exists in nightly mode, i.e. when
        ``AUTO_RUN_COMMUNITY_DETECTION`` is False.  The seeded
        ``WorkerSettings`` (``_seed_worker_settings``) leaves it at its
        default False — ``WorkerSettings`` is a plain BaseModel, so no env
        override applies — hence under the test settings the cron must be
        present.
        """
        from services.worker.worker import main
        from workers.tasks.summarise_community import summarise_community

        _mocks = _patch_main_deps()
        try:
            with pytest.raises(asyncio.CancelledError):
                await main()

            calls = _mocks["create_arq_worker"].call_args_list
            assert len(calls) == 2  # high first, low second
            cron_jobs = calls[1].kwargs["cron_jobs"]

            matches = [
                job
                for job in cron_jobs
                if getattr(job, "job_id", None) == "nightly_community_detection"
            ]
            assert len(matches) == 1
            job = matches[0]
            assert job.coroutine is summarise_community
            assert job.hour == 2
            assert job.minute == 0
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
    _bs.OPENBAO_SECRET_ID = "secret"  # noqa: S105  — mock value, not a credential
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

    # ── prewarm_embeddings (lazy import from core.embeddings in main()) ─
    # Source-module target so the ``from core.embeddings import
    # prewarm_embeddings`` inside ``main()`` resolves to the mock.
    # CI unit env lacks fastembed — without this the real prewarm raises
    # ImportError and all 7 TestMain tests fail.
    _prewarm = AsyncMock()
    prewarm_patch = patch("core.embeddings.prewarm_embeddings", new=_prewarm)
    prewarm_patch.start()
    _patchers.append(prewarm_patch)
    mocks["prewarm_embeddings"] = _prewarm

    # ── init_arq / close_arq (lazy import from core.arq in main()) ─────
    # Source-module target so the ``from core.arq import close_arq,
    # init_arq`` inside ``main()`` resolves to the mocks. Without this
    # the real init_arq runs create_pool under the mocked loop and all 7
    # TestMain tests fail with ConnectionError from real Redis.
    _init_arq = AsyncMock()
    init_arq_patch = patch("core.arq.init_arq", new=_init_arq)
    init_arq_patch.start()
    _patchers.append(init_arq_patch)
    mocks["init_arq"] = _init_arq

    _close_arq = AsyncMock()
    close_arq_patch = patch("core.arq.close_arq", new=_close_arq)
    close_arq_patch.start()
    _patchers.append(close_arq_patch)
    mocks["close_arq"] = _close_arq

    # ── FalkorDB / BlockingConnectionPool — lazy import in try/except ────
    # falkordb IS installed in the test env, so the lazy import succeeds
    # and ``BlockingConnectionPool.from_url(settings.FALKORDB_URL)`` runs
    # (FALKORDB_URL is None → ValueError).  Patch ``from_url`` to raise
    # ``ImportError`` so the try/except gracefully falls back to None.
    import redis.asyncio as _redis_asyncio

    _bcp_orig = _redis_asyncio.BlockingConnectionPool  # save for restore
    _bcp_mock = MagicMock()
    _bcp_mock.from_url = MagicMock(
        side_effect=ImportError("test env — falkordb disabled")
    )
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

    # ── init_redis / close_redis (lazy import from core.redis in main()) ──
    # Source-module target so the ``from core.redis import ...`` inside
    # ``main()`` resolves to the mocks.  Without this the real init_redis
    # builds a real TCP client and the leader-lock SET NX EX dials real
    # Redis — every TestMain test fails with ConnectionError.
    _election_redis = AsyncMock()
    _election_redis.set = AsyncMock(return_value=True)
    init_redis_patch = patch("core.redis.init_redis", return_value=_election_redis)
    init_redis_patch.start()
    _patchers.append(init_redis_patch)
    mocks["election_redis"] = _election_redis

    _close_election_redis = AsyncMock()
    close_redis_patch = patch("core.redis.close_redis", new=_close_election_redis)
    close_redis_patch.start()
    _patchers.append(close_redis_patch)
    mocks["close_election_redis"] = _close_election_redis

    # NOTE: no ``asyncio.get_running_loop`` patch.  ``main()`` no longer
    # touches the loop, and patching it globally poisons the real redis
    # client's health check (redis/asyncio/connection.py) with a MagicMock.

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
