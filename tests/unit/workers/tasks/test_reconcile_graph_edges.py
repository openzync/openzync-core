"""Unit tests for the ``reconcile_graph_edges`` cron task (Phase 3).

The safety net: a Postgres anti-join finds active edges with no matching
active fact and enqueues one ``expire_graph_edges`` job per stale edge on
the low-priority queue.  Idempotent by construction (the task's
``invalid_at IS NULL`` guard), so overlapping ticks and the post-commit
sync never conflict.

No I/O — the session result rows and ARQ redis are mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from workers.tasks.reconcile_graph_edges import reconcile_graph_edges

pytestmark = pytest.mark.unit

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000002")
SRC_ENTITY = UUID("00000000-0000-0000-0000-00000000aaaa")
TGT_ENTITY = UUID("00000000-0000-0000-0000-00000000bbbb")
EDGE_ID = UUID("00000000-0000-0000-0000-000000000100")
QUEUE = "OpenZync:test:queue:low"


def _edge_row(**overrides) -> SimpleNamespace:
    return SimpleNamespace(
        id=overrides.get("id", EDGE_ID),
        organization_id=overrides.get("org", ORG_ID),
        project_id=overrides.get("project", PROJECT_ID),
        source_id=overrides.get("source", SRC_ENTITY),
        target_id=overrides.get("target", TGT_ENTITY),
        relationship_type=overrides.get("rel", "works_at"),
    )


def _make_db(rows: list[SimpleNamespace]) -> AsyncMock:
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.__aexit__.return_value = None
    result = MagicMock()
    result.all.return_value = rows
    db.execute.return_value = result
    return db


def _factory(db: AsyncMock) -> MagicMock:
    factory = MagicMock()
    factory.return_value = db
    return factory


class TestReconcileGraphEdges:
    """Scenario 12 — stale edges enqueue expiry; kept edges don't."""

    @pytest.mark.asyncio
    async def test_stale_edge_enqueues_expiry_with_edge_provenance(self) -> None:
        """Active edge with no matching active fact → one expire_graph_edges job."""
        db = _make_db([_edge_row()])
        enqueued: list[dict] = []

        async def _enqueue_job(task: str, **kwargs) -> str:
            enqueued.append({"task": task, "kwargs": kwargs})
            return "job-1"

        redis = AsyncMock()
        redis.enqueue_job = _enqueue_job
        ctx = {
            "db_session_factory": _factory(db),
            "redis": redis,
            "_queue_name": QUEUE,
        }

        summary = await reconcile_graph_edges(ctx)

        assert summary == "Enqueued 1 edge expiries (from 1 stale)"
        assert len(enqueued) == 1
        job = enqueued[0]
        assert job["task"] == "expire_graph_edges"
        kwargs = job["kwargs"]
        assert kwargs["org_id"] == str(ORG_ID)
        assert kwargs["project_id"] == str(PROJECT_ID)
        assert kwargs["source_id"] == str(SRC_ENTITY)
        assert kwargs["target_id"] == str(TGT_ENTITY)
        assert kwargs["relationship_type"] == "works_at"
        # Reconcile-sourced expiries carry the EDGE id as provenance (no
        # superseding fact exists) and are tagged for the low queue.
        assert kwargs["fact_id"] == str(EDGE_ID)
        assert kwargs["_queue_name"] == QUEUE
        assert "at_time" in kwargs

    @pytest.mark.asyncio
    async def test_no_stale_edges_returns_noop(self) -> None:
        """Every active edge has a matching active fact → nothing enqueued."""
        db = _make_db([])
        redis = AsyncMock()
        ctx = {
            "db_session_factory": _factory(db),
            "redis": redis,
            "_queue_name": QUEUE,
        }

        summary = await reconcile_graph_edges(ctx)

        assert summary == "No stale edges found"
        redis.enqueue_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_session_factory_skips(self) -> None:
        summary = await reconcile_graph_edges(ctx={"redis": AsyncMock()})
        assert summary == "Skipped: no db_session_factory in ARQ ctx"

    @pytest.mark.asyncio
    async def test_missing_redis_skips(self) -> None:
        summary = await reconcile_graph_edges(
            ctx={"db_session_factory": _factory(_make_db([]))}
        )
        assert summary == "Skipped: no redis in ARQ ctx"

    @pytest.mark.asyncio
    async def test_queue_name_defaults_when_absent(self) -> None:
        """Cron without ``_queue_name`` falls back to the dev low queue."""
        db = _make_db([_edge_row()])
        captured: dict = {}

        async def _enqueue_job(task: str, **kwargs) -> str:
            captured.update(kwargs)
            return "job-1"

        redis = AsyncMock()
        redis.enqueue_job = _enqueue_job
        ctx = {"db_session_factory": _factory(db), "redis": redis}

        await reconcile_graph_edges(ctx)

        assert captured["_queue_name"] == "OpenZync:development:queue:low"

    @pytest.mark.asyncio
    async def test_enqueue_failure_logged_and_continues(self) -> None:
        """A failed enqueue is logged and skipped — the tick still completes."""
        db = _make_db([_edge_row(), _edge_row(id=UUID("00000000-0000-0000-0000-000000000101"))])
        enqueued: list[str] = []

        async def _flaky(task: str, **kwargs) -> str:
            if kwargs["fact_id"] == str(EDGE_ID):
                raise RuntimeError("redis enqueue boom")
            enqueued.append(kwargs["fact_id"])
            return "job-2"

        redis = AsyncMock()
        redis.enqueue_job = _flaky
        ctx = {
            "db_session_factory": _factory(db),
            "redis": redis,
            "_queue_name": QUEUE,
        }

        summary = await reconcile_graph_edges(ctx)

        assert summary == "Enqueued 1 edge expiries (from 2 stale)"
        assert enqueued == ["00000000-0000-0000-0000-000000000101"]
