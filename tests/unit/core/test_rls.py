"""Unit tests for core/rls.py — the RLS tenant context contract.

The tenant context is **transaction-local** (``set_config(..., is_local =>
true)``): PostgreSQL discards it when the transaction ends, so it can never
outlive its transaction and can never sit on a pooled connection that the
next request picks up.

That cost is paid by :class:`core.rls.RlsAsyncSession`, which *remembers*
the context on the session and re-binds it to the session's next
transaction after every commit and every rollback.  The re-bind is the
first statement of the new transaction, so no statement of that
transaction can observe a foreign tenant.

The context is only remembered when it is bound through this module.  A
hand-rolled ``set_config`` is *not* remembered, so a tenant bound that way
is silently lost at the next commit — the sharp edge the worker task
files were converted away from.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core import rls
from core.rls import RlsAsyncSession, RlsContext

pytestmark = pytest.mark.unit


ORG_ID_STR = "00000000-0000-0000-0000-000000000001"
OTHER_ORG_ID_STR = "00000000-0000-0000-0000-000000000002"

APPLY_SQL = (
    "SELECT set_config('app.org_id', :org_id, true), "
    "set_config('app.bypass_rls', :bypass_rls, true), "
    "set_config('app.lookup_code', :lookup_code, true)"
)
"""The single statement every context binding goes through."""


class _FakeSession:
    """Minimal ``AsyncSession`` stand-in that records what was run.

    ``operations`` is the ordered log of session calls (``execute`` /
    ``rollback`` / ``commit``) and ``executed`` holds the ``(sql, params)``
    of every statement.  That is what lets the tests pin *when* the tenant
    was bound and *what* the release did.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        """``(sql, params)`` of every statement executed on this session."""
        self.operations: list[str] = []
        """Ordered log of session operations: execute / rollback / commit."""
        self.rls_context = RlsContext()
        """The context the session remembers — empty until one is bound."""

    async def execute(self, query: Any, params: Any | None = None) -> Any:
        sql = str(query.text if hasattr(query, "text") else query)
        self.executed.append((sql, params))
        self.operations.append("execute")
        return None

    async def rollback(self) -> None:
        self.operations.append("rollback")

    async def commit(self) -> None:
        self.operations.append("commit")


class TestInitContext:
    """``init_context`` — every request starts from a known context."""

    @pytest.mark.asyncio
    async def test_binds_the_tenant_and_neutralises_the_connection(self) -> None:
        """The tenant is bound, the bypass is pinned off, the lookup code cleared.

        All three GUCs are bound by **one** statement, and the statement is
        transaction-local (``is_local => true``) — the context can never
        outlive the transaction that set it.
        """
        db = _FakeSession()

        await rls.init_context(db, ORG_ID_STR)

        assert len(db.executed) == 1
        sql, params = db.executed[0]
        assert sql == APPLY_SQL
        assert params == {
            "org_id": ORG_ID_STR,
            "bypass_rls": "false",
            "lookup_code": "",
        }

    @pytest.mark.asyncio
    async def test_the_binding_is_transaction_local(self) -> None:
        """Every ``set_config`` in the binding carries the ``is_local`` flag.

        This is the whole point of the redesign: a session-scoped GUC
        (``is_local => false``) survives the transaction and stands on the
        pooled connection for the next request.  The flag is what tells the
        two designs apart, so it is asserted explicitly.
        """
        db = _FakeSession()

        await rls.init_context(db, ORG_ID_STR)

        sql, _params = db.executed[0]
        assert sql.count("set_config(") == 3
        assert sql.count(", true)") == 3
        assert ", false)" not in sql

    @pytest.mark.asyncio
    async def test_unauthenticated_request_binds_an_empty_tenant(self) -> None:
        """No tenant → an EMPTY context, never an unset one.

        The org-isolation policies read
        ``NULLIF(current_setting('app.org_id', true), '')::UUID``, so an
        empty context is NULL: no row matches, and nothing raises.
        """
        db = _FakeSession()

        await rls.init_context(db, None)

        _sql, params = db.executed[0]
        assert params == {
            "org_id": "",
            "bypass_rls": "false",
            "lookup_code": "",
        }

    @pytest.mark.asyncio
    async def test_init_context_remembers_the_context_on_the_session(self) -> None:
        """The session remembers the context — that is what survives a commit."""
        db = _FakeSession()

        await rls.init_context(db, ORG_ID_STR)

        assert db.rls_context == RlsContext(org_id=ORG_ID_STR)

    @pytest.mark.asyncio
    async def test_init_context_pins_a_remembered_bypass_off(self) -> None:
        """A context initialised from scratch cannot inherit a stale bypass."""
        db = _FakeSession()
        db.rls_context = RlsContext(org_id=OTHER_ORG_ID_STR, bypass_rls=True)

        await rls.init_context(db, ORG_ID_STR)

        assert db.rls_context.bypass_rls is False
        assert db.rls_context.org_id == ORG_ID_STR


class TestSetTenantContext:
    """``set_tenant_context`` — bind one tenant and remember it."""

    @pytest.mark.asyncio
    async def test_binds_the_tenant_and_remembers_it(self) -> None:
        """The tenant is bound and recorded on the session."""
        db = _FakeSession()

        await rls.set_tenant_context(db, ORG_ID_STR)

        _sql, params = db.executed[0]
        assert params["org_id"] == ORG_ID_STR
        assert db.rls_context.org_id == ORG_ID_STR

    @pytest.mark.asyncio
    async def test_preserves_the_bypass_and_the_lookup_code(self) -> None:
        """Binding a tenant does not silently clear the bypass."""
        db = _FakeSession()
        db.rls_context = RlsContext(bypass_rls=True, lookup_code="abc")

        await rls.set_tenant_context(db, ORG_ID_STR)

        _sql, params = db.executed[0]
        assert params == {
            "org_id": ORG_ID_STR,
            "bypass_rls": "true",
            "lookup_code": "abc",
        }

    @pytest.mark.asyncio
    async def test_a_hand_rolled_set_config_is_not_remembered(self) -> None:
        """A raw ``set_config`` binds the tenant but does NOT record it.

        This is the sharp edge the nine worker task files were converted
        away from: a hand-bound tenant is silently lost at the next commit,
        because only :func:`core.rls.set_tenant_context` records the
        context on the session.
        """
        db = _FakeSession()

        await db.execute(
            text("SELECT set_config('app.org_id', :org_id, true)"),
            {"org_id": ORG_ID_STR},
        )

        assert db.rls_context.is_empty


class TestReleaseTenantContext:
    """``release_tenant_context`` — drop the remembered context, roll back."""

    @pytest.mark.asyncio
    async def test_drops_the_context_and_rolls_back(self) -> None:
        """The release drops the remembered context and rolls back.

        It issues **no statement**: the context is transaction-local, so
        the end of the transaction already discarded it — there is nothing
        left standing on the pooled connection to reset.
        """
        db = _FakeSession()
        db.rls_context = RlsContext(org_id=ORG_ID_STR, bypass_rls=True)

        await rls.release_tenant_context(db)

        assert db.operations == ["rollback"]
        assert db.executed == []
        assert db.rls_context.is_empty

    @pytest.mark.asyncio
    async def test_a_broken_connection_is_logged_not_raised(self) -> None:
        """A dead connection is logged loudly; the original error still wins."""
        db = _FakeSession()

        async def _boom() -> None:
            raise rls.SQLAlchemyError("connection is closed")

        db.rollback = _boom  # type: ignore[method-assign]

        await rls.release_tenant_context(db)

        # The remembered context is dropped even when the rollback failed.
        assert db.rls_context.is_empty


class TestRlsBypass:
    """``rls_bypass`` — a scoped, transaction-bound window with policies off."""

    @pytest.mark.asyncio
    async def test_bypass_is_transaction_scoped_and_reset_on_exit(self) -> None:
        """The bypass is SET LOCAL, so it cannot outlive its transaction."""
        db = _FakeSession()

        async with rls.rls_bypass(db):
            assert db.executed[-1][0] == (
                "SELECT set_config('app.bypass_rls', 'true', true)"
            )

        # The reset is the last thing the block does.
        assert db.executed[-1][0] == (
            "SELECT set_config('app.bypass_rls', 'false', true)"
        )

    @pytest.mark.asyncio
    async def test_bypass_is_reset_when_the_block_raises(self) -> None:
        """An exception inside the block still resets the bypass."""
        db = _FakeSession()

        with pytest.raises(RuntimeError, match="boom"):
            async with rls.rls_bypass(db):
                raise RuntimeError("boom")

        assert db.executed[-1][0] == (
            "SELECT set_config('app.bypass_rls', 'false', true)"
        )


class TestRlsAsyncSession:
    """``RlsAsyncSession`` — the tenant survives a commit and a rollback.

    The base ``AsyncSession`` methods are patched out so the test drives
    the subclass's own logic (the re-bind) without a live database; the
    end-to-end behaviour is pinned by the integration test.
    """

    @staticmethod
    def _session() -> RlsAsyncSession:
        """An ``RlsAsyncSession`` whose ``execute`` is a recording mock."""
        session = RlsAsyncSession()
        session.execute = AsyncMock()  # type: ignore[method-assign]
        return session

    @pytest.mark.asyncio
    async def test_commit_rebinds_the_remembered_context(self) -> None:
        """After a commit the SAME tenant is bound to the next transaction.

        A transaction-local GUC is discarded by the COMMIT, so without the
        re-bind the session would run un-tenanted — and the next
        transaction could pick up another request's connection.
        """
        session = self._session()

        with patch.object(AsyncSession, "commit", new=AsyncMock()):
            await rls.set_tenant_context(session, ORG_ID_STR)
            await session.commit()

        statements = [str(c.args[0]) for c in session.execute.await_args_list]
        params = [c.args[1] for c in session.execute.await_args_list]
        assert statements == [APPLY_SQL, APPLY_SQL]
        assert params[1]["org_id"] == ORG_ID_STR

    @pytest.mark.asyncio
    async def test_the_rebind_is_the_first_statement_after_the_commit(self) -> None:
        """No statement of the new transaction can observe a foreign tenant.

        The re-bind opens the next transaction, so it is that transaction's
        first statement — asserted here by recording the interleaving of
        the base commit and the session's statements.
        """
        session = self._session()
        order: list[str] = []

        async def _execute(query: Any, params: Any | None = None) -> Any:
            order.append("execute")
            return None

        async def _commit() -> None:
            order.append("commit")

        session.execute = AsyncMock(side_effect=_execute)  # type: ignore[method-assign]

        with patch.object(AsyncSession, "commit", new=AsyncMock(side_effect=_commit)):
            await rls.set_tenant_context(session, ORG_ID_STR)
            order.clear()
            await session.commit()

        assert order == ["commit", "execute"]

    @pytest.mark.asyncio
    async def test_rollback_rebinds_the_remembered_context(self) -> None:
        """A rollback discards the context too — the re-bind restores it."""
        session = self._session()

        with (
            patch.object(AsyncSession, "rollback", new=AsyncMock()),
            patch.object(AsyncSession, "get_transaction", return_value=object()),
        ):
            await rls.set_tenant_context(session, ORG_ID_STR)
            await session.rollback()

        statements = [str(c.args[0]) for c in session.execute.await_args_list]
        assert statements == [APPLY_SQL, APPLY_SQL]

    @pytest.mark.asyncio
    async def test_rollback_without_a_transaction_does_not_rebind(self) -> None:
        """No transaction → nothing was discarded → no re-bind is issued."""
        session = self._session()

        with (
            patch.object(AsyncSession, "rollback", new=AsyncMock()),
            patch.object(AsyncSession, "get_transaction", return_value=None),
        ):
            await rls.set_tenant_context(session, ORG_ID_STR)
            await session.rollback()

        statements = [str(c.args[0]) for c in session.execute.await_args_list]
        assert statements == [APPLY_SQL]

    @pytest.mark.asyncio
    async def test_a_hand_bound_context_is_not_rebound_after_a_commit(self) -> None:
        """A raw ``set_config`` is not remembered — the tenant is lost.

        The counterpart of :func:`core.rls.set_tenant_context`: binding by
        hand leaves the session with nothing to re-bind, so the next
        transaction runs un-tenanted.  This is why the worker task files
        must go through ``core.rls``.
        """
        session = self._session()

        with patch.object(AsyncSession, "commit", new=AsyncMock()):
            await session.execute(
                text("SELECT set_config('app.org_id', :org_id, true)"),
                {"org_id": ORG_ID_STR},
            )
            await session.commit()

        statements = [str(c.args[0]) for c in session.execute.await_args_list]
        assert statements == ["SELECT set_config('app.org_id', :org_id, true)"]

    @pytest.mark.asyncio
    async def test_an_empty_context_is_not_rebound(self) -> None:
        """An unbound session has nothing to restore — no statement is issued."""
        session = self._session()

        with patch.object(AsyncSession, "commit", new=AsyncMock()):
            await session.commit()

        session.execute.assert_not_awaited()


class _FakeOp:
    """Stand-in for alembic's ``op`` — records the SQL the migration runs."""

    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def _load_migration_0059() -> Any:
    """Import ``migrations/versions/0059_force_rls.py`` (not a valid module name)."""
    path = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "versions"
        / "0059_force_rls.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0059_force_rls", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestUnsetTenantContext:
    """An unset tenant context must match no rows — it must not raise.

    ``current_setting('app.org_id')`` without ``missing_ok`` raises
    ``UndefinedObjectError`` when the GUC is unset (a 500 instead of an
    empty result set).  The org-isolation policies use
    ``NULLIF(current_setting('app.org_id', true), '')::UUID``: an unset or
    empty context is NULL, so the comparison matches nothing.
    """

    @pytest.mark.asyncio
    async def test_policies_fail_closed_instead_of_raising(self, monkeypatch) -> None:
        """Every org-isolation policy uses the fail-closed expression."""
        module = _load_migration_0059()
        statements: list[str] = []
        monkeypatch.setattr(module, "op", _FakeOp(statements))

        module.upgrade()

        policies = [s for s in statements if "CREATE POLICY org_isolation_" in s]
        assert policies, "the migration must create the org-isolation policies"
        for policy in policies:
            assert "NULLIF(current_setting('app.org_id', true), '')::UUID" in policy
            # The raising form must not survive anywhere.
            assert "current_setting('app.org_id')" not in policy


class TestOrgAnonInsertPolicy:
    """``org_anon_insert`` must not admit an arbitrary organization row.

    Permissive policies are OR'ed, so a ``WITH CHECK (true)`` overrides
    the ``FOR ALL`` policy's ``id = <app.org_id>`` and lets a
    tenant-bound session insert an organization row for *any* id.  The
    CHECK is therefore the same expression the SELECT policy uses.
    """

    @pytest.mark.asyncio
    async def test_the_check_is_not_unconditionally_true(self, monkeypatch) -> None:
        """The INSERT policy's CHECK binds the row to the session's tenant."""
        module = _load_migration_0059()
        statements: list[str] = []
        monkeypatch.setattr(module, "op", _FakeOp(statements))

        module.upgrade()

        policy = next(s for s in statements if "CREATE POLICY org_anon_insert" in s)
        assert "WITH CHECK (true)" not in policy
        assert "id = NULLIF(current_setting('app.org_id', true), '')::UUID" in policy

    @pytest.mark.asyncio
    async def test_the_audit_policy_no_longer_admits_platform_rows(
        self, monkeypatch
    ) -> None:
        """``audit_logs`` no longer hands the organization-less rows to tenants.

        Those rows carry every unauthenticated request's IP address, user
        agent, path and query string — a cross-tenant disclosure.
        """
        module = _load_migration_0059()
        statements: list[str] = []
        monkeypatch.setattr(module, "op", _FakeOp(statements))

        module.upgrade()

        policy = next(
            s for s in statements if "CREATE POLICY org_isolation_audit_logs" in s
        )
        assert "organization_id IS NULL" not in policy
