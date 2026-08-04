"""Unit tests for the ``expire_graph_edges`` ARQ task (Phase 3).

The task is the external-backend (SurrealDB/FalkorDB) expiry leg of the
graph-edge sync: it resolves the org's backend inside the worker, expires
matching edges, and commits.  Retry ×3 via ``with_retry``; on final
failure the metric ``openzync_graph_edge_sync_failures_total`` is
incremented, the full context is logged loudly, and the exception is
re-raised (never swallowed — facts are the source of truth).

No I/O — the backend, session factory, and ``asyncio.sleep`` are mocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from workers.tasks.expire_graph_edges import expire_graph_edges

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
SRC_ENTITY = UUID("00000000-0000-0000-0000-00000000aaaa")
TGT_ENTITY = UUID("00000000-0000-0000-0000-00000000bbbb")
FACT_ID = UUID("00000000-0000-0000-0000-000000000100")
AT_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

_TASK_ARGS = dict(
    org_id=str(ORG_ID),
    project_id=str(PROJECT_ID),
    source_id=str(SRC_ENTITY),
    target_id=str(TGT_ENTITY),
    relationship_type="reports_to",
    at_time=AT_TIME,
    fact_id=str(FACT_ID),
)


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.__aexit__.return_value = None
    return db


def _factory(db: AsyncMock) -> MagicMock:
    factory = MagicMock()
    factory.return_value = db
    return factory


def _ctx(db: AsyncMock) -> dict:
    return {"db_session_factory": _factory(db)}


def _backend(*, count: int = 1) -> AsyncMock:
    backend = AsyncMock()
    backend.expire_relationships_matching = AsyncMock(return_value=count)
    return backend


class TestExpireGraphEdgesTask:
    """Scenario 10b/11 — the ARQ task resolves, expires, commits, reports."""

    @pytest.mark.asyncio
    async def test_happy_path_expires_and_commits(self) -> None:
        """Backend resolved → expiry called with the exact triple, commit, summary."""
        db = _make_db()
        backend = _backend(count=2)
        with patch("workers.backend.resolve_graph_backend", new=AsyncMock(return_value=backend)):
            result = await expire_graph_edges(ctx=_ctx(db), **_TASK_ARGS)

        assert result == f"expired 2 edge(s) for {SRC_ENTITY}->{TGT_ENTITY} reports_to"
        backend.expire_relationships_matching.assert_awaited_once_with(
            org_id=ORG_ID,
            project_id=PROJECT_ID,
            source_id=SRC_ENTITY,
            target_id=TGT_ENTITY,
            relationship_type="reports_to",
            at_time=AT_TIME,
        )
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_graph_disabled_backend_none_returns_zero(self) -> None:
        """Org has no graph backend → no-op, count 0, no commit of an expiry."""
        db = _make_db()
        with patch("workers.backend.resolve_graph_backend", new=AsyncMock(return_value=None)):
            result = await expire_graph_edges(ctx=_ctx(db), **_TASK_ARGS)

        assert result == f"expired 0 edge(s) for {SRC_ENTITY}->{TGT_ENTITY} reports_to"
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_session_factory_raises(self) -> None:
        """Worker ctx without ``db_session_factory`` → loud RuntimeError."""
        with pytest.raises(RuntimeError, match="db_session_factory missing"):
            await expire_graph_edges(ctx={}, **_TASK_ARGS)

    @pytest.mark.asyncio
    async def test_retries_transient_failure_then_succeeds(self) -> None:
        """``with_retry(max_retries=3)`` is applied — a transient failure
        is retried and the final call's count is returned."""
        db = _make_db()
        backend = _backend()
        calls = 0

        async def _flaky(**kwargs) -> int:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("surreal hiccup")
            return 1

        backend.expire_relationships_matching = _flaky
        with (
            patch("workers.backend.resolve_graph_backend", new=AsyncMock(return_value=backend)),
            patch("workers.tasks.base.asyncio.sleep", new=AsyncMock()),
        ):
            result = await expire_graph_edges(ctx=_ctx(db), **_TASK_ARGS)

        assert calls == 3
        assert result == f"expired 1 edge(s) for {SRC_ENTITY}->{TGT_ENTITY} reports_to"
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_final_failure_raises(self) -> None:
        """Persistent failure → retries exhausted, the error is re-raised."""
        db = _make_db()
        backend = _backend()
        backend.expire_relationships_matching = AsyncMock(
            side_effect=RuntimeError("backend down")
        )
        with (
            patch("workers.backend.resolve_graph_backend", new=AsyncMock(return_value=backend)),
            patch("workers.tasks.base.asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(RuntimeError, match="backend down"):
                await expire_graph_edges(ctx=_ctx(db), **_TASK_ARGS)

    @pytest.mark.asyncio
    async def test_final_failure_increments_metric_and_logs_loudly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Scenario 11 — final failure bumps the failure counter + logs context."""
        db = _make_db()
        backend = _backend()
        backend.expire_relationships_matching = AsyncMock(
            side_effect=RuntimeError("backend down")
        )
        with (
            patch("workers.backend.resolve_graph_backend", new=AsyncMock(return_value=backend)),
            patch("workers.tasks.base.asyncio.sleep", new=AsyncMock()),
            patch(
                "workers.tasks.expire_graph_edges.graph_edge_sync_failures_total"
            ) as mock_counter,
        ):
            with pytest.raises(RuntimeError, match="backend down"):
                await expire_graph_edges(ctx=_ctx(db), **_TASK_ARGS)

        mock_counter.inc.assert_called_once()
        # The failure log carries the full context in the record's extra
        # (structured logging) — fact_id, triple, at_time, error.
        failed = [r for r in caplog.records if r.getMessage() == "expire_graph_edges.failed"]
        assert failed, "the final failure must be logged loudly"
        record = failed[0]
        assert record.fact_id == str(FACT_ID)
        assert record.error == "backend down"
        assert record.error_type == "RuntimeError"
