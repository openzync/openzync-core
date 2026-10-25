"""Regression: metrics batch fan-out uses sibling sessions (item 2).

- With ``db_session_factory`` on app state all 12 queries succeed and
  the envelope reports ``partial=False``.
- Without a factory the batch falls back to sequential DB legs and still
  returns the partial-OK envelope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from services.admin_metrics.batch_service import AdminMetricsBatchService

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

NAMES = [
    "episodes_per_day",
    "messages_per_day",
    "activity_per_day",
    "entities_per_day",
    "facts_per_day",
    "enrichment_progress",
    "top_projects_by_episodes",
    "top_users_by_messages",
    "error_rate_by_day",
    "latency_percentiles",
    "queue_depth_over_time",
    "context_retrieval_rate",
]

PROM_NAMES = {"error_rate_by_day", "latency_percentiles", "context_retrieval_rate"}


def _ok(name: str) -> dict:
    """Minimal per-query result shaped like ``shared.build_result``."""
    return {
        "query": name,
        "org_scoped": True,
        "columns": ["date", "count"],
        "rows": [["2026-08-18", 1]],
        "total": 1,
        "parameters": {},
    }


class _OneSession:
    """Async context manager yielding a single mock sibling session."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


def _make_request(*, with_factory: bool) -> SimpleNamespace:
    """Fake request carrying (or omitting) ``db_session_factory``."""
    state = SimpleNamespace()
    if with_factory:
        sessions: list[AsyncMock] = []

        def _factory() -> _OneSession:
            session = AsyncMock()
            sessions.append(session)
            return _OneSession(session)

        state.db_session_factory = _factory
        state.created_sessions = sessions
    return SimpleNamespace(app=SimpleNamespace(state=state), state=state)


def _patch_legs(monkeypatch: pytest.MonkeyPatch, *, fail: set[str] | None = None):
    """Stub every DB/Prom leg to return ``_ok`` (or raise for ``fail``)."""
    fail = fail or set()
    from services.admin_metrics import batch_service as bs

    for name in NAMES:
        if name in PROM_NAMES:
            continue

        async def _db_leg(
            self,  # noqa: ANN001
            *args,  # noqa: ANN002
            _name: str = name,
            **kwargs,  # noqa: ANN003
        ) -> dict:
            if _name in (fail or set()):
                raise RuntimeError(f"{_name} down")
            return _ok(_name)

        monkeypatch.setattr(bs.DbMetricsQueriesService, name, _db_leg, raising=False)
    for name in PROM_NAMES:

        async def _prom_leg(
            self,  # noqa: ANN001
            *args,  # noqa: ANN002
            _name: str = name,
            **kwargs,  # noqa: ANN003
        ) -> dict:
            if _name in (fail or set()):
                raise RuntimeError(f"{_name} down")
            return _ok(_name)

        monkeypatch.setattr(
            bs.PromMetricsQueriesService, name, _prom_leg, raising=False
        )


@pytest.mark.asyncio
async def test_batch_all_twelve_succeed_partial_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent batch: 12/12 succeed → ``partial=False``, no errors."""
    _patch_legs(monkeypatch)
    request = _make_request(with_factory=True)
    service = AdminMetricsBatchService(db=AsyncMock(), prom_client=AsyncMock())
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 8, tzinfo=UTC)

    with (
        patch(
            "services.admin_metrics.batch_service.resolve_graph_backend_fail_soft",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "services.admin_metrics.batch_service.set_tenant_context",
            new=AsyncMock(),
        ),
        patch(
            "services.admin_metrics.batch_service.release_tenant_context",
            new=AsyncMock(),
        ),
    ):
        result = await service.run_batch(
            request, MagicMock(), ORG_ID, start, end, 20, None, False
        )

    assert set(result["results"]) == set(NAMES)
    assert result["errors"] == []
    assert result["meta"]["partial"] is False
    assert result["meta"]["limit"] == 20
    # One sibling session per DB leg (9 DB legs, Prom shares the client).
    assert len(request.state.created_sessions) == len(NAMES) - len(PROM_NAMES)


@pytest.mark.asyncio
async def test_batch_sequential_fallback_without_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No factory → sequential DB legs on the shared session, still 200."""
    _patch_legs(monkeypatch)
    request = _make_request(with_factory=False)
    assert getattr(request.state, "db_session_factory", None) is None
    service = AdminMetricsBatchService(db=AsyncMock(), prom_client=AsyncMock())
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 8, tzinfo=UTC)

    with patch(
        "services.admin_metrics.batch_service.resolve_graph_backend_fail_soft",
        new=AsyncMock(return_value=None),
    ):
        result = await service.run_batch(
            request, MagicMock(), ORG_ID, start, end, 20, None, False
        )

    assert set(result["results"]) == set(NAMES)
    assert result["errors"] == []
    assert result["meta"]["partial"] is False


@pytest.mark.asyncio
async def test_batch_single_failure_stays_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failing leg lands in ``errors`` with HTTP-200 ``partial=True``."""
    _patch_legs(monkeypatch, fail={"facts_per_day"})
    request = _make_request(with_factory=True)
    service = AdminMetricsBatchService(db=AsyncMock(), prom_client=AsyncMock())
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 8, tzinfo=UTC)

    with (
        patch(
            "services.admin_metrics.batch_service.resolve_graph_backend_fail_soft",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "services.admin_metrics.batch_service.set_tenant_context",
            new=AsyncMock(),
        ),
        patch(
            "services.admin_metrics.batch_service.release_tenant_context",
            new=AsyncMock(),
        ),
    ):
        result = await service.run_batch(
            request, MagicMock(), ORG_ID, start, end, 20, None, False
        )

    assert "facts_per_day" not in result["results"]
    assert {e["query"] for e in result["errors"]} == {"facts_per_day"}
    assert result["meta"]["partial"] is True
