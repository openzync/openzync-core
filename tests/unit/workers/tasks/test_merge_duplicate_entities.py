"""Unit tests for merge_duplicate_entities task."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

_ORG_ID = str(uuid4())


@pytest.mark.unit
class TestMergeDuplicateEntities:
    """merge_duplicate_entities task tests."""

    def _make_db(
        self, org_ids: list | None = None, project_ids: list | None = None
    ) -> AsyncMock:
        """Fake session whose ``execute`` answers the queries the task makes.

        The RLS context statements (``set_config``) are bookkeeping, not
        queries: they get a benign default so they can never be mistaken
        for a row set.  The real queries are answered by the table they
        select from, so the fake stays honest for a sweep over several
        orgs — each org runs its own project query.
        """
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = None
        org_id_tuples = [(oid,) for oid in (org_ids or [uuid4()])]
        proj_id_tuples = [(pid,) for pid in (project_ids or [uuid4()])]
        org_result = MagicMock()
        org_result.all.return_value = org_id_tuples
        project_result = MagicMock()
        project_result.all.return_value = proj_id_tuples

        async def _execute(
            statement: object, *args: object, **kwargs: object
        ) -> MagicMock:
            sql = str(statement)
            if "set_config" in sql:
                return MagicMock()
            if "organizations" in sql:
                return org_result
            if "projects" in sql:
                return project_result
            raise AssertionError(f"unexpected statement: {sql}")

        db.execute.side_effect = _execute
        return db

    def _make_entities(
        self, count: int = 3, names: list[str] | None = None
    ) -> list[dict]:
        if names is None:
            names = [f"Entity_{i}" for i in range(count)]
        now = datetime.now(UTC).isoformat()
        return [
            {
                "id": str(uuid4()),
                "name": names[i] if i < len(names) else f"Entity_{i}",
                "entity_type": "test_type",
                "created_at": now,
            }
            for i in range(count)
        ]

    def _factory(self, db: AsyncMock) -> MagicMock:
        f = MagicMock()
        f.return_value = db
        return f

    def _ctx(self, db: AsyncMock) -> dict:
        return {"db_engine": MagicMock(), "db_session_factory": self._factory(db)}

    @pytest.mark.asyncio
    async def test_no_eligible_orgs(self) -> None:
        """No organizations exist → returns skipped."""
        with patch(
            "workers.tasks.merge_duplicate_entities.with_retry",
            lambda **kw: lambda f: f,
        ):
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            org_result = MagicMock()
            org_result.all.return_value = []
            db.execute.return_value = org_result

            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db))

            assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_no_duplicates(self) -> None:
        """Entity dedup with no duplicates → no-op."""
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(3)
        mock_backend.bulk_search_entities.return_value = []

        with (
            patch(
                "workers.tasks.merge_duplicate_entities.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(
                "workers.tasks.merge_duplicate_entities.resolve_graph_backend",
                return_value=mock_backend,
            ),
            patch("asyncio.sleep", AsyncMock()),
        ):
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db))

            assert result["status"] == "completed"
            assert result["clusters_merged"] == 0

    @pytest.mark.asyncio
    async def test_duplicates_merged(self) -> None:
        """Duplicate entities merged correctly."""
        mock_backend = AsyncMock()
        entities = self._make_entities(names=["DuplicateName", "DuplicateName"])
        mock_backend.get_all_entities.return_value = entities
        mock_backend.bulk_search_entities.return_value = []
        mock_backend.merge_entities.return_value = {
            "rewired_count": 2,
            "deleted_count": 1,
            "merged_count": 1,
        }

        with (
            patch(
                "workers.tasks.merge_duplicate_entities.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(
                "workers.tasks.merge_duplicate_entities.resolve_graph_backend",
                return_value=mock_backend,
            ),
            patch("services.audit_log_service.AuditLogService") as mock_audit_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_audit = AsyncMock()
            mock_audit_cls.return_value = mock_audit
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db))

            assert result["status"] == "completed"
            assert result["clusters_merged"] >= 1
            assert mock_backend.merge_entities.called
            mock_audit.log_action.assert_called_once()

    @pytest.mark.asyncio
    async def test_single_org_no_duplicates(self) -> None:
        """Entity with no duplicates → returns early."""
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(1)

        with (
            patch(
                "workers.tasks.merge_duplicate_entities.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(
                "workers.tasks.merge_duplicate_entities.resolve_graph_backend",
                return_value=mock_backend,
            ),
            patch("asyncio.sleep", AsyncMock()),
        ):
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result["status"] == "completed"
            assert result["clusters_merged"] == 0

    @pytest.mark.asyncio
    async def test_backend_unavailable(self) -> None:
        """Graph backend unavailable → gracefully skips."""
        with (
            patch(
                "workers.tasks.merge_duplicate_entities.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(
                "workers.tasks.merge_duplicate_entities.resolve_graph_backend",
                return_value=None,
            ),
        ):
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db), org_id=_ORG_ID)

            assert result["status"] == "completed"
            assert result["clusters_merged"] == 0

    @pytest.mark.asyncio
    async def test_all_orgs_fail(self) -> None:
        """All orgs fail → raises RuntimeError."""
        with (
            patch(
                "workers.tasks.merge_duplicate_entities.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(
                "workers.tasks.merge_duplicate_entities.resolve_graph_backend",
                side_effect=Exception("Backend down"),
            ),
        ):
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            with pytest.raises(RuntimeError, match="All .* orgs failed"):
                await merge_duplicate_entities(ctx=self._ctx(db))

    @pytest.mark.asyncio
    async def test_db_error_propagates(self) -> None:
        """Database error during org query propagates."""
        with patch(
            "workers.tasks.merge_duplicate_entities.with_retry",
            lambda **kw: lambda f: f,
        ):
            db = AsyncMock()
            db.__aenter__.return_value = db
            db.__aexit__.return_value = None
            db.execute.side_effect = Exception("DB error")

            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            with pytest.raises(Exception):
                await merge_duplicate_entities(ctx=self._ctx(db))

    @pytest.mark.asyncio
    async def test_transitive_relationships(self) -> None:
        """Merge with transitive relationships handles correctly."""
        mock_backend = AsyncMock()
        entities = self._make_entities(names=["Alpha", "Alpha", "Beta", "Beta"])
        mock_backend.get_all_entities.return_value = entities
        mock_backend.bulk_search_entities.return_value = []
        mock_backend.merge_entities.return_value = {
            "rewired_count": 3,
            "deleted_count": 2,
            "merged_count": 2,
        }

        with (
            patch(
                "workers.tasks.merge_duplicate_entities.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(
                "workers.tasks.merge_duplicate_entities.resolve_graph_backend",
                return_value=mock_backend,
            ),
            patch("services.audit_log_service.AuditLogService") as mock_audit_cls,
            patch("asyncio.sleep", AsyncMock()),
        ):
            mock_audit = AsyncMock()
            mock_audit_cls.return_value = mock_audit
            db = self._make_db()
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db))

            assert result["status"] == "completed"
            assert result["clusters_merged"] >= 1

    # ── RLS tenant context ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_tenant_context_is_rebound_for_each_org(self) -> None:
        """The per-org loop binds the tenant again for every org it sweeps.

        The sweep iterates over orgs, so a context bound once for the
        first org would leave every later org reading under the wrong
        tenant (and, under enforced RLS, reading nothing at all).
        """
        org_1, org_2 = uuid4(), uuid4()
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(1)

        with (
            patch(
                "workers.tasks.merge_duplicate_entities.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(
                "workers.tasks.merge_duplicate_entities.resolve_graph_backend",
                return_value=mock_backend,
            ),
            patch("asyncio.sleep", AsyncMock()),
        ):
            db = self._make_db(org_ids=[org_1, org_2])
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            result = await merge_duplicate_entities(ctx=self._ctx(db))

        assert result["status"] == "completed"
        # The ``set_config('app.org_id', …)`` bindings, in order.  The
        # release statement also mentions ``app.org_id`` but carries no
        # params, so it is excluded by the parameter check.
        binds = [
            call.args[1]["org_id"]
            for call in db.execute.call_args_list
            if len(call.args) > 1 and "app.org_id" in str(call.args[0])
        ]
        assert binds == [str(org_1), str(org_2)]

    @pytest.mark.asyncio
    async def test_org_listing_runs_under_the_bypass_not_a_stale_tenant(self) -> None:
        """The cross-org scan is not tenant-scoped — it runs under the bypass.

        The organization list spans every tenant, so it cannot be read
        under one org's context.  It is read under a transaction-scoped
        bypass instead, and the transaction is closed right after — so the
        per-org work below runs under the tenant context, not under the
        bypass.

        The per-org binding goes through ``core.rls``, so it is remembered
        on the session and re-applied after the per-org commit instead of
        being lost at the next transaction boundary.
        """
        org_1 = uuid4()
        mock_backend = AsyncMock()
        mock_backend.get_all_entities.return_value = self._make_entities(1)

        with (
            patch(
                "workers.tasks.merge_duplicate_entities.with_retry",
                lambda **kw: lambda f: f,
            ),
            patch(
                "workers.tasks.merge_duplicate_entities.resolve_graph_backend",
                return_value=mock_backend,
            ),
            patch("asyncio.sleep", AsyncMock()),
        ):
            db = self._make_db(org_ids=[org_1])
            from workers.tasks.merge_duplicate_entities import merge_duplicate_entities

            await merge_duplicate_entities(ctx=self._ctx(db))

        statements = [str(call.args[0]) for call in db.execute.call_args_list]
        # The bypass is enabled before anything else is read…
        assert statements[0] == "SELECT set_config('app.bypass_rls', 'true', true)"
        # …the organization list is read while it is enabled…
        org_list_index = next(
            i for i, s in enumerate(statements) if "organizations" in s
        )
        assert statements[org_list_index - 1] == (
            "SELECT set_config('app.bypass_rls', 'true', true)"
        )
        # …and the bypass is reset right after, before the per-org work.
        reset_index = next(
            i for i, s in enumerate(statements) if "app.bypass_rls', 'false'" in s
        )
        assert reset_index > org_list_index
        # The transaction is closed so the per-org work cannot run under
        # the (transaction-scoped) bypass…
        assert db.rollback.await_count >= 1
        # …it runs under the tenant context instead — bound through
        # ``core.rls`` so the binding is remembered on the session.
        assert statements[reset_index + 1] == (
            "SELECT set_config('app.org_id', :org_id, true), "
            "set_config('app.bypass_rls', :bypass_rls, true), "
            "set_config('app.lookup_code', :lookup_code, true)"
        )
        tenant_params = db.execute.call_args_list[reset_index + 1].args[1]
        assert tenant_params == {
            "org_id": str(org_1),
            "bypass_rls": "false",
            "lookup_code": "",
        }
        # The release at the end of the task drops the remembered context.
        assert db.rls_context.is_empty
