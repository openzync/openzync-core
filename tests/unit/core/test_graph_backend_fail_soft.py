"""Regression: shared graph-backend fail-soft resolver (item 4).

``core.graph_backend.resolve_graph_backend_fail_soft`` is the single home
for the dispatcher+pool+resolve sequence used by the admin metrics,
admin stats, and batch services. A down graph must degrade to ``None``,
never raise.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.exceptions import GraphBackendUnavailableError
from core.graph_backend import resolve_graph_backend_fail_soft

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


def _make_request(*, dispatcher=None, surreal_pool=None, falkordb=None):
    """Fake request with app-state graph plumbing (all optional)."""
    state = SimpleNamespace(
        graph_backend_dispatcher=dispatcher,
        surreal_connection_pool=surreal_pool,
        falkordb_client=falkordb,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _org_config(backend: str = "falkordb") -> SimpleNamespace:
    return SimpleNamespace(graph_backend=backend, graph_max_traversal_depth=None)


@contextmanager
def _patched_graph_stats(*, module_path: str):
    """Stub GraphStatsService where the service under test imported it."""
    with patch(f"{module_path}.GraphStatsService") as cls:
        inst = cls.return_value
        inst.resolve_project_ids = AsyncMock(return_value=[])
        inst.entity_totals = AsyncMock(return_value=(0, 0))
        inst.entity_counts_per_day = AsyncMock(return_value={})
        yield cls


@pytest.mark.asyncio
async def test_fail_soft_returns_backend_on_success() -> None:
    """Healthy dispatcher resolves and returns the backend instance."""
    backend = MagicMock()
    dispatcher = MagicMock()
    dispatcher.resolve_and_create.return_value = backend
    request = _make_request(dispatcher=dispatcher)

    result = await resolve_graph_backend_fail_soft(
        request, _org_config(), ORG_ID, AsyncMock(), "admin_metrics"
    )

    assert result is backend
    dispatcher.resolve_and_create.assert_called_once()


@pytest.mark.asyncio
async def test_fail_soft_no_dispatcher_returns_none() -> None:
    """Missing dispatcher degrades to ``None`` instead of raising."""
    request = _make_request(dispatcher=None)
    result = await resolve_graph_backend_fail_soft(
        request, _org_config(), ORG_ID, AsyncMock(), "admin_metrics"
    )
    assert result is None


@pytest.mark.asyncio
async def test_fail_soft_surreal_pool_failure_returns_none() -> None:
    """A failing SurrealDB pool degrades to ``None`` (no resolve attempt)."""
    pool = AsyncMock()
    pool.get_or_create.side_effect = RuntimeError("surreal down")
    dispatcher = MagicMock()
    request = _make_request(dispatcher=dispatcher, surreal_pool=pool)

    result = await resolve_graph_backend_fail_soft(
        request, _org_config("surrealdb"), ORG_ID, AsyncMock(), "admin_stats"
    )

    assert result is None
    dispatcher.resolve_and_create.assert_not_called()


@pytest.mark.asyncio
async def test_fail_soft_unknown_backend_returns_none() -> None:
    """``ValueError``/``GraphBackendUnavailableError`` degrade to ``None``."""
    for exc in (
        ValueError("unknown backend 'neo4j'"),
        GraphBackendUnavailableError("graph down"),
    ):
        dispatcher = MagicMock()
        dispatcher.resolve_and_create.side_effect = exc
        request = _make_request(dispatcher=dispatcher)
        result = await resolve_graph_backend_fail_soft(
            request, _org_config(), ORG_ID, AsyncMock(), "admin_metrics"
        )
        assert result is None


@pytest.mark.asyncio
async def test_fail_soft_unexpected_error_returns_none() -> None:
    """Any other resolver exception degrades to ``None`` — never raises."""
    dispatcher = MagicMock()
    dispatcher.resolve_and_create.side_effect = RuntimeError("boom")
    request = _make_request(dispatcher=dispatcher)
    result = await resolve_graph_backend_fail_soft(
        request, _org_config(), ORG_ID, AsyncMock(), "admin_stats"
    )
    assert result is None


def test_three_services_import_the_shared_resolver() -> None:
    """Metrics, stats, and batch services share the one fail-soft helper."""
    import services.admin_metrics.batch_service as batch_mod
    import services.admin_metrics_service as metrics_mod
    import services.admin_stats_service as stats_mod

    for mod in (metrics_mod, stats_mod, batch_mod):
        assert mod.resolve_graph_backend_fail_soft is resolve_graph_backend_fail_soft


@pytest.mark.asyncio
async def test_metrics_service_calls_shared_resolver() -> None:
    """AdminMetricsService.get_summary goes through the shared resolver."""
    import services.admin_metrics_service as metrics_mod

    service = metrics_mod.AdminMetricsService(db=AsyncMock(), prom=AsyncMock())
    seen: list[str] = []

    async def _spy(request, org_config, org_id, db, log_prefix):  # noqa: ANN001, ANN202
        seen.append(log_prefix)
        return None

    repo = AsyncMock()
    for name in (
        "count_episodes_total",
        "count_episodes_24h",
        "count_episodes_archived",
        "count_episodes_enrichable",
        "count_episodes_in_progress",
        "count_episodes_pending",
        "count_episodes_fully_enriched",
        "count_episodes_with_embeddings",
        "count_users_total",
    ):
        setattr(repo, name, AsyncMock(return_value=0))
    service._repo = repo  # noqa: SLF001
    from core.exceptions import MetricsUnavailableError

    service._prom.get_summary = AsyncMock(  # noqa: SLF001
        side_effect=MetricsUnavailableError("prom down")
    )
    with (
        pytest.MonkeyPatch().context() as mp,
        _patched_graph_stats(module_path="services.admin_metrics_service"),
    ):
        mp.setattr(metrics_mod, "resolve_graph_backend_fail_soft", _spy)
        result = await service.get_summary(
            _make_request(dispatcher=None),
            _org_config(),
            ORG_ID,
            MagicMock(),
            MagicMock(),
        )
    assert seen == ["admin_metrics"]
    assert result.status == "degraded"


@pytest.mark.asyncio
async def test_stats_service_calls_shared_resolver() -> None:
    """AdminStatsService.get_usage_stats goes through the shared resolver."""
    import services.admin_stats_service as stats_mod

    service = stats_mod.AdminStatsService(db=AsyncMock())
    seen: list[str] = []

    async def _spy(request, org_config, org_id, db, log_prefix):  # noqa: ANN001, ANN202
        seen.append(log_prefix)
        return None

    service._repo = AsyncMock(  # noqa: SLF001
        episodes_per_day=AsyncMock(return_value=[]),
        sessions_per_day=AsyncMock(return_value=[]),
        facts_per_day=AsyncMock(return_value=[]),
        extractions_per_day=AsyncMock(return_value=[]),
        observations_per_day=AsyncMock(return_value=[]),
        classifications_per_day=AsyncMock(return_value=[]),
        edges_per_day=AsyncMock(return_value=[]),
    )
    with (
        pytest.MonkeyPatch().context() as mp,
        _patched_graph_stats(module_path="services.admin_stats_service"),
    ):
        mp.setattr(stats_mod, "resolve_graph_backend_fail_soft", _spy)
        await service.get_usage_stats(
            _make_request(dispatcher=None),
            _org_config(),
            ORG_ID,
            MagicMock(),
            None,
            None,
        )
    assert seen == ["admin_stats"]


def test_batch_service_calls_shared_resolver() -> None:
    """AdminMetricsBatchService.run_batch goes through the shared resolver."""
    import services.admin_metrics.batch_service as batch_mod

    src = inspect.getsource(batch_mod.AdminMetricsBatchService.run_batch)
    assert "resolve_graph_backend_fail_soft" in src
