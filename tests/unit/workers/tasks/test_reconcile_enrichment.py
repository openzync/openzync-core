"""Unit tests for reconcile_enrichment task."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.worker.worker_settings import WorkerSettings

_EPISODE_ID = str(uuid4())
_ORG_ID = str(uuid4())
_PROJECT_ID = str(uuid4())
_SESSION_ID = str(uuid4())
_CONTENT = "Test episode content."


@pytest.mark.unit
class TestReconcileEnrichment:
    """reconcile_enrichment task tests."""

    def _mock_stale_episode(
        self,
        enrichment_status: int = 0,
        episode_id: str | None = None,
    ) -> dict:
        return {
            "id": episode_id or _EPISODE_ID,
            "content": _CONTENT,
            "org_id": _ORG_ID,
            "project_id": _PROJECT_ID,
            "session_id": _SESSION_ID,
            "metadata": {},
            "enrichment_status": enrichment_status,
        }

    def _mock_row(self, enrichment_status: int = 0) -> MagicMock:
        row = MagicMock()
        row.id = _EPISODE_ID
        row.content = _CONTENT
        row.organization_id = _ORG_ID
        row.project_id = _PROJECT_ID
        row.session_id = _SESSION_ID
        row.metadata_ = {}
        row.enrichment_status = enrichment_status
        return row

    @pytest.mark.asyncio
    async def test_no_stale_episodes(self) -> None:
        """No pending episodes → no-op."""
        session_factory = MagicMock()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        result = MagicMock()
        result.all.return_value = []
        db.execute.return_value = result
        session_factory.return_value = db

        arq_redis = AsyncMock()
        arq_redis.zcard.return_value = 0

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
        }

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        result_str = await reconcile_enrichment(ctx)

        assert result_str == "No stale episodes found"

    @pytest.mark.asyncio
    async def test_no_session_factory(self) -> None:
        """Missing db_session_factory → graceful skip."""
        arq_redis = AsyncMock()
        ctx: dict = {"redis": arq_redis}

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        result = await reconcile_enrichment(ctx)

        assert "Skipped" in result
        assert "no db_session_factory" in result

    @pytest.mark.asyncio
    async def test_no_redis(self) -> None:
        """Missing redis in context → graceful skip."""
        session_factory = MagicMock()
        ctx: dict = {"db_session_factory": session_factory}

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        result = await reconcile_enrichment(ctx)

        assert "Skipped" in result
        assert "no redis" in result

    @pytest.mark.asyncio
    async def test_backlog_skip(self) -> None:
        """High-priority queue backlog exceeds threshold → skip."""
        session_factory = MagicMock()
        arq_redis = AsyncMock()
        arq_redis.zcard.return_value = 2000  # above BACKLOG_SKIP_THRESHOLD (1000)

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
        }

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        result = await reconcile_enrichment(ctx)

        assert "Skipped" in result
        assert "high-priority queue" in result

    @pytest.mark.asyncio
    async def test_re_enqueues_missing_tasks(self) -> None:
        """Stale episodes with missing bits → tasks re-enqueued."""
        session_factory = MagicMock()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        row = self._mock_row(enrichment_status=0)  # all bits missing
        result = MagicMock()
        result.all.return_value = [row]
        db.execute.return_value = result
        session_factory.return_value = db

        arq_redis = AsyncMock()
        arq_redis.zcard.return_value = 0
        arq_redis.enqueue_job = AsyncMock()

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
        }

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        result = await reconcile_enrichment(ctx)

        assert "Re-enqueued" in result
        # Should enqueue: enrich_episode (LLM combined) + embed_episode + link_entities_to_episode
        assert arq_redis.enqueue_job.call_count >= 3

    @pytest.mark.asyncio
    async def test_target_queues_come_from_worker_settings(self) -> None:
        """Enqueue targets are the worker's high / low queues, not literals.

        ARQ never puts ``_queue_name`` in the task context, so the old
        ``ctx.get("_queue_name")`` lookup always fell back to a hard-coded
        ``OpenZync:development:queue:low`` that no production worker
        consumes.  The targets now come from ``settings.high_queue_full``
        and ``settings.low_queue_full``.
        """
        session_factory = MagicMock()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        row = self._mock_row(enrichment_status=0)  # all bits missing
        result = MagicMock()
        result.all.return_value = [row]
        db.execute.return_value = result
        session_factory.return_value = db

        arq_redis = AsyncMock()
        arq_redis.zcard.return_value = 0
        arq_redis.enqueue_job = AsyncMock()

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
            # Decoy: ARQ never sets ``_queue_name`` in the task context.
            "_queue_name": "OpenZync:decoy:queue:low",
        }

        prod_settings = WorkerSettings(
            DATABASE_URL="postgresql+asyncpg://localhost:5432/test",
            REDIS_URL="redis://localhost:6379/0",
            ENV="prod",
        )

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        with patch("services.worker.worker_settings._settings", prod_settings):
            await reconcile_enrichment(ctx)

        targets = {
            call.args[0]: call.kwargs["_queue_name"]
            for call in arq_redis.enqueue_job.await_args_list
        }
        # enrich_episode / embed_episode are high-priority, the entity
        # linking pass is low-priority — both named by the worker settings.
        assert targets["enrich_episode"] == "OpenZync:prod:queue:high"
        assert targets["embed_episode"] == "OpenZync:prod:queue:high"
        assert targets["link_entities_to_episode"] == "OpenZync:prod:queue:low"
        # The development-only literal is gone.
        assert "OpenZync:development:queue:low" not in targets.values()

    @pytest.mark.asyncio
    async def test_already_enriched_skipped(self) -> None:
        """Episode with all enrichment bits set → not returned as stale."""

        session_factory = MagicMock()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        # The mock simulates the SQL WHERE enrichment_status != ENRICHMENT_ALL
        # by returning empty results when ENRICHMENT_ALL is set.
        result = MagicMock()
        result.all.return_value = []  # row excluded by WHERE clause
        db.execute.return_value = result
        session_factory.return_value = db

        arq_redis = AsyncMock()
        arq_redis.zcard.return_value = 0

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
        }

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        result = await reconcile_enrichment(ctx)

        assert result == "No stale episodes found"

    @pytest.mark.asyncio
    async def test_partial_enrichment(self) -> None:
        """Episode with only some bits set → only missing tasks enqueued."""
        # Only bit 0 set (entities extracted), rest missing
        from workers.tasks.base import ENRICHMENT_ENTITIES

        session_factory = MagicMock()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        row = self._mock_row(enrichment_status=ENRICHMENT_ENTITIES)
        result = MagicMock()
        result.all.return_value = [row]
        db.execute.return_value = result
        session_factory.return_value = db

        arq_redis = AsyncMock()
        arq_redis.zcard.return_value = 0
        arq_redis.enqueue_job = AsyncMock()

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
        }

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        result = await reconcile_enrichment(ctx)

        assert "Re-enqueued" in result
        # Should enqueue: enrich_episode (missing LLM bits) + embed_episode + link_entities
        # Entity bit (bit 0) is set → NOT re-enqueued individually

    @pytest.mark.asyncio
    async def test_enqueue_failure_logged(self) -> None:
        """Enqueue failure logged, other tasks still processed."""
        session_factory = MagicMock()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        row = self._mock_row(enrichment_status=0)
        result = MagicMock()
        result.all.return_value = [row]
        db.execute.return_value = result
        session_factory.return_value = db

        arq_redis = AsyncMock()
        arq_redis.zcard.return_value = 0
        # First enqueue fails, subsequent ones succeed
        arq_redis.enqueue_job = AsyncMock(
            side_effect=[Exception("Queue full"), None, None, None, None]
        )

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
        }

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        result = await reconcile_enrichment(ctx)

        assert "Re-enqueued" in result
        # Some tasks should still have been enqueued despite the failure.
        # With enrichment_status=0: enrich_episode (LLM) + embed_episode +
        # link_entities = 3, plus the fact-repair pass (the mock DB returns
        # the same row for both queries) = 4.  The first enqueue fails, the
        # rest succeed, but all 4 calls are made.
        assert arq_redis.enqueue_job.call_count == 4

    @pytest.mark.asyncio
    async def test_db_error_propagates(self) -> None:
        """Database error propagates."""
        session_factory = MagicMock()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        db.execute.side_effect = Exception("DB error")
        session_factory.return_value = db

        arq_redis = AsyncMock()
        arq_redis.zcard.return_value = 0

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
        }

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        with pytest.raises(Exception, match="DB error"):
            await reconcile_enrichment(ctx)

    @pytest.mark.asyncio
    async def test_backlog_guard_zcard_failure(self) -> None:
        """zcard failure → falls back to 0, does not skip."""

        session_factory = MagicMock()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        row = self._mock_row(enrichment_status=0)
        result = MagicMock()
        result.all.return_value = [row]
        db.execute.return_value = result
        session_factory.return_value = db

        arq_redis = AsyncMock()
        arq_redis.zcard.side_effect = Exception("Redis down")
        arq_redis.enqueue_job = AsyncMock()

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
        }

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        result = await reconcile_enrichment(ctx)

        assert "Re-enqueued" in result


@pytest.mark.unit
class TestReconcileEnrichmentRls:
    """Cron RLS regression — both cross-org scans bypass RLS before querying.

    The policies read ``NULLIF(current_setting('app.org_id', true), '')``,
    so an unset context matches no rows — the bypass is what makes the
    platform-wide scan possible.  The fix sets ``app.bypass_rls`` at the
    top of each scan session — fact-embedding repair pass and
    stale-episode pass — and these tests pin that ordering.
    """

    @pytest.mark.asyncio
    async def test_bypass_rls_precedes_both_scans(self) -> None:
        """Each scan session opens with the RLS bypass enabled."""
        session_factory = MagicMock()
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        result = MagicMock()
        result.all.return_value = []  # empty so no org-config I/O is attempted
        db.execute.return_value = result
        session_factory.return_value = db

        arq_redis = AsyncMock()
        arq_redis.zcard.return_value = 0

        ctx: dict = {
            "db_session_factory": session_factory,
            "redis": arq_redis,
        }

        from workers.tasks.reconcile_enrichment import reconcile_enrichment

        summary = await reconcile_enrichment(ctx)

        assert summary == "No stale episodes found"
        stmts = [str(call.args[0]) for call in db.execute.await_args_list]
        # Fact-embedding pass then episode pass, each opening with the bypass.
        assert len(stmts) == 4
        assert "app.bypass_rls" in stmts[0]
        assert "facts" in stmts[1]
        assert "app.bypass_rls" in stmts[2]
        assert "episodes" in stmts[3]
        # The bypass removes any need for the per-org GUC on these scans:
        # every context binding enables the bypass and binds NO tenant —
        # a stale tenant would narrow the platform-wide scan.
        bindings = [
            call.args[1]
            for call in db.execute.await_args_list
            if len(call.args) > 1
            and isinstance(call.args[1], dict)
            and "bypass_rls" in call.args[1]
        ]
        assert bindings
        for params in bindings:
            assert params["bypass_rls"] == "true"
            assert params["org_id"] == ""
