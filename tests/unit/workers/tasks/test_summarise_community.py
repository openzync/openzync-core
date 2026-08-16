"""Unit tests for summarise_community task."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_ORG_ID = str(uuid4())


@pytest.mark.unit
class TestSummariseCommunity:
    """summarise_community task tests."""

    def _make_db(self, org_ids: list | None = None, project_ids: list | None = None) -> AsyncMock:
        db = AsyncMock()
        # ``add`` is sync in SQLAlchemy — an AsyncMock child would return an
        # unawaited coroutine (RuntimeWarning → error under filterwarnings).
        db.add = MagicMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        org_id_tuples = [(oid,) for oid in (org_ids or [uuid4()])]
        proj_id_tuples = [(pid,) for pid in (project_ids or [uuid4()])]
        org_result = MagicMock()
        org_result.all.return_value = org_id_tuples
        proj_result = MagicMock()
        proj_result.all.return_value = proj_id_tuples

        def _execute_side_effect(*args, **kwargs):
            # Check if it's an Organization query or Project query
            from sqlalchemy.sql.elements import TextClause

            stmt = args[0] if args else kwargs.get("statement")
            if isinstance(stmt, TextClause):
                # RLS set_config calls — return value is discarded.
                dummy = MagicMock()
                dummy.all.return_value = []
                return dummy
            stmt_str = str(stmt) if stmt is not None else ""
            if "organizations" in stmt_str.lower() or "Organization" in stmt_str:
                return org_result
            return proj_result

        db.execute.side_effect = _execute_side_effect
        return db

    def _make_entities(self, count: int) -> list[dict]:
        return [
            {"id": str(uuid4()), "name": f"Entity_{i}", "type": "test", "summary": ""}
            for i in range(count)
        ]

    def _factory(self, db: AsyncMock) -> MagicMock:
        f = MagicMock()
        f.return_value = db
        return f

    def _ctx(self, db: AsyncMock) -> dict:
        return {"db_engine": MagicMock(), "db_session_factory": self._factory(db)}

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """Community summarization via Label Propagation completes."""
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(8)
        mock_backend.get_all_relationships.return_value = []
        mock_backend.create_entity.return_value = {"id": str(uuid4())}
        mock_backend.create_relationship_bulk = AsyncMock()

        with (
            patch("workers.tasks.summarise_community.resolve_graph_backend", return_value=mock_backend),
            patch("packages.community.algorithms.build_entity_graph") as mock_build,
            patch("packages.community.algorithms.detect_communities_label_propagation") as mock_detect,
            patch("core.llm.resolve_backend") as mock_llm,
            patch("core.org_config.get_org_config") as mock_cfg,
        ):
            mock_graph = MagicMock()
            mock_build.return_value = mock_graph
            community_ids = [str(uuid4()) for _ in range(3)]
            mock_detect.return_value = [set(community_ids)]

            mock_llm_backend = AsyncMock()
            mock_llm_backend.chat.return_value = MagicMock(content="Summary text.")
            mock_llm.return_value = mock_llm_backend
            mock_cfg.return_value = MagicMock(to_llm_config_dict=lambda: {})

            # Need to handle org query + project query — use separate side effects
            oid = uuid4()
            pid = uuid4()
            db = self._make_db(org_ids=[oid], project_ids=[pid])

            from workers.tasks.summarise_community import summarise_community

            result = await summarise_community(ctx=self._ctx(db))

            assert result["status"] == "completed"
            assert result["communities_created"] >= 1
            assert mock_backend.create_entity.called

    @pytest.mark.asyncio
    async def test_no_eligible_orgs(self) -> None:
        """No organizations exist → returns skipped."""
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        org_result = MagicMock()
        org_result.all.return_value = []
        db.execute.return_value = org_result

        from workers.tasks.summarise_community import summarise_community

        result = await summarise_community(ctx=self._ctx(db))

        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_single_org(self) -> None:
        """Single org processed when org_id is provided."""
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(6)
        mock_backend.get_all_relationships.return_value = []
        mock_backend.create_entity.return_value = {"id": str(uuid4())}
        mock_backend.create_relationship_bulk = AsyncMock()

        with (
            patch("workers.tasks.summarise_community.resolve_graph_backend", return_value=mock_backend),
            patch("packages.community.algorithms.build_entity_graph"),
            patch("packages.community.algorithms.detect_communities_label_propagation") as mock_detect,
            patch("core.llm.resolve_backend"),
            patch("core.org_config.get_org_config"),
        ):
            mock_detect.return_value = [{str(uuid4()), str(uuid4())}]

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            proj_result = MagicMock()
            proj_result.all.return_value = [(uuid4(),)]
            db.execute.return_value = proj_result

            from workers.tasks.summarise_community import summarise_community

            result = await summarise_community(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_too_few_entities(self) -> None:
        """Project with fewer than 5 entities → no communities."""
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(3)

        with patch("workers.tasks.summarise_community.resolve_graph_backend", return_value=mock_backend):
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            proj_result = MagicMock()
            proj_result.all.return_value = [(uuid4(),)]
            db.execute.return_value = proj_result

            from workers.tasks.summarise_community import summarise_community

            result = await summarise_community(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result["communities_created"] == 0

    @pytest.mark.asyncio
    async def test_no_communities_found(self) -> None:
        """Graph with enough entities but no communities → none created."""
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(8)
        mock_backend.get_all_relationships.return_value = []

        with (
            patch("workers.tasks.summarise_community.resolve_graph_backend", return_value=mock_backend),
            patch("packages.community.algorithms.build_entity_graph"),
            patch("packages.community.algorithms.detect_communities_label_propagation", return_value=[]),
        ):
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            db.execute.return_value = proj_result = MagicMock()
            proj_result.all.return_value = [(uuid4(),)]

            from workers.tasks.summarise_community import summarise_community

            result = await summarise_community(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result["communities_created"] == 0

    @pytest.mark.asyncio
    async def test_llm_fallback_summary(self) -> None:
        """LLM failure → template-based fallback summary used."""
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(6)
        mock_backend.get_all_relationships.return_value = []
        mock_backend.create_entity.return_value = {"id": str(uuid4())}
        mock_backend.create_relationship_bulk = AsyncMock()

        with (
            patch("workers.tasks.summarise_community.resolve_graph_backend", return_value=mock_backend),
            patch("packages.community.algorithms.build_entity_graph"),
            patch("packages.community.algorithms.detect_communities_label_propagation") as mock_detect,
            patch("core.llm.resolve_backend", side_effect=Exception("LLM down")),
        ):
            mock_detect.return_value = [{str(uuid4()), str(uuid4()), str(uuid4())}]

            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            proj_result = MagicMock()
            proj_result.all.return_value = [(uuid4(),)]
            db.execute.return_value = proj_result

            from workers.tasks.summarise_community import summarise_community

            result = await summarise_community(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result["communities_created"] >= 1
            assert mock_backend.create_entity.called

    @pytest.mark.asyncio
    async def test_backend_unavailable(self) -> None:
        """Graph backend unavailable → skips org gracefully."""
        with patch("workers.tasks.summarise_community.resolve_graph_backend", return_value=None):
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            proj_result = MagicMock()
            proj_result.all.return_value = [(uuid4(),)]
            db.execute.return_value = proj_result

            from workers.tasks.summarise_community import summarise_community

            result = await summarise_community(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result["communities_created"] == 0

    @pytest.mark.asyncio
    async def test_db_error_propagates(self) -> None:
        """Database errors are not silently swallowed."""
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        db.execute.side_effect = Exception("DB error")

        from workers.tasks.summarise_community import summarise_community

        with pytest.raises(Exception):
            await summarise_community(ctx=self._ctx(db), org_id=_ORG_ID)

    @pytest.mark.asyncio
    async def test_discovery_sets_bypass_rls_before_org_query(self) -> None:
        """RLS bypass is set before the org-discovery query in cron mode.

        Regression for the RLS fix: without the transaction-local
        ``set_config('app.bypass_rls', 'true', true)`` the organizations
        policy (migrations/0001) admits no rows under any RLS-enforced
        role, so the nightly run silently returns "skipped".
        """
        oid = uuid4()
        db = self._make_db(org_ids=[oid], project_ids=[])

        with patch(
            "workers.tasks.summarise_community.resolve_graph_backend",
            return_value=None,
        ):
            from workers.tasks.summarise_community import summarise_community

            await summarise_community(ctx=self._ctx(db))

        calls = db.execute.call_args_list
        assert str(calls[0].args[0]) == (
            "SELECT set_config('app.bypass_rls', 'true', true)"
        )
        assert "organizations" in str(calls[1].args[0]).lower()

    @pytest.mark.asyncio
    async def test_process_org_sets_org_id_context(self) -> None:
        """RLS app.org_id context is set before per-org project discovery.

        Regression for the RLS fix: without the transaction-local
        ``set_config('app.org_id', ...)`` the projects policy
        (migrations/0019) admits no rows for this org, so every org
        silently reports zero projects.

        The statement is pinned to exact full-string equality (including the
        trailing ``, true)``) so a regression to session-local
        ``set_config('app.org_id', :org_id, false)`` fails this test — the
        bug class this fix batch prevents (cf. the ``59c3fcb79b85``
        session-level precedent in the cleanup worker docstring).
        """
        oid = uuid4()
        db = self._make_db(org_ids=[], project_ids=[uuid4()])
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(2)

        with patch(
            "workers.tasks.summarise_community.resolve_graph_backend",
            return_value=mock_backend,
        ):
            from workers.tasks.summarise_community import summarise_community

            await summarise_community(ctx=self._ctx(db), org_id=str(oid))

        calls = db.execute.call_args_list
        assert str(calls[0].args[0]) == (
            "SELECT set_config('app.org_id', :org_id, true)"
        )
        assert calls[0].args[1] == {"org_id": str(oid)}
        assert "projects" in str(calls[1].args[0]).lower()

    @pytest.mark.asyncio
    async def test_org_failure_rolls_back_transaction(self) -> None:
        """A mid-org DB failure rolls back the shared session before re-raising.

        Regression for the review fix: without the per-org rollback, a
        failed org aborts the shared transaction and the next org's
        ``set_config`` raises ``InFailedSqlTransaction``, cascading to
        "All orgs failed".  With it, ``db.rollback`` is awaited, the org is
        collected as failed, and the run reports ``partial`` with only the
        successful org's commit issued.
        """
        org_1 = uuid4()
        org_2 = uuid4()

        discovery = MagicMock()
        discovery.all.return_value = [(org_1,), (org_2,)]
        empty = MagicMock()
        empty.all.return_value = []
        proj_result = MagicMock()
        proj_result.all.return_value = [(uuid4(),)]

        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        # bypass_rls + org-discovery + org_1 set_config + org_1 project
        # query + org_2 set_config + org_2 project query (raises).
        db.execute.side_effect = [
            empty, discovery, empty, proj_result, empty,
            Exception("mid-org DB error"),
        ]

        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(2)

        with patch(
            "workers.tasks.summarise_community.resolve_graph_backend",
            return_value=mock_backend,
        ):
            from workers.tasks.summarise_community import summarise_community

            result = await summarise_community(ctx=self._ctx(db))

        assert result["status"] == "partial"
        assert result["orgs_processed"] == 2
        assert result["orgs_failed"] == 1
        db.rollback.assert_awaited_once()
        # Only org_1 committed — org_2's failed transaction was rolled back.
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_orgs_fail_raises(self) -> None:
        """Every org failing in cron mode raises ``RuntimeError``.

        Guards the "All N orgs failed" branch in ``summarise_community``
        (discovery mode, >1 org): without it, a regression that swallows
        per-org failures and reports success would pass tests.  Both orgs
        must be attempted and their failures aggregated into the raised
        error — the per-org ``set_config`` calls below prove both were
        reached despite the first org's failure.
        """
        org_1 = uuid4()
        org_2 = uuid4()

        discovery = MagicMock()
        discovery.all.return_value = [(org_1,), (org_2,)]
        empty = MagicMock()
        empty.all.return_value = []

        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        # bypass_rls + org discovery + org_1 set_config + org_1 project
        # query (raises) + org_2 set_config + org_2 project query (raises).
        db.execute.side_effect = [
            empty, discovery, empty,
            Exception("project select failed"),
            empty,
            Exception("project select failed"),
        ]

        mock_backend = AsyncMock()

        with patch(
            "workers.tasks.summarise_community.resolve_graph_backend",
            return_value=mock_backend,
        ):
            from workers.tasks.summarise_community import summarise_community

            with pytest.raises(RuntimeError, match="All 2 orgs failed"):
                await summarise_community(ctx=self._ctx(db))

        calls = db.execute.call_args_list
        # Both orgs were attempted — each org's set_config ran with its id.
        assert calls[2].args[1] == {"org_id": str(org_1)}
        assert calls[4].args[1] == {"org_id": str(org_2)}
