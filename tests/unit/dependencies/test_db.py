"""Unit tests for dependencies/db.py — get_db async session dependency.

New contract (RLS tenant context):

* the tenant context is **transaction-local** (``set_config(..., is_local
  => true)``), so PostgreSQL discards it when the transaction ends — it
  can never survive onto a pooled connection that the next request
  picks up,
* every session is **initialised** at the start of a request
  (``init_context`` binds ``app.org_id``, pins ``app.bypass_rls='false'``
  and clears ``app.lookup_code``),
* and it is **released** at the end (``release_tenant_context`` drops the
  remembered context and rolls back — there is nothing left standing on
  the connection to reset).

The session the dependency hands out is a ``core.rls.RlsAsyncSession``,
which re-binds the context to the transaction that follows every commit
and every rollback, so a mid-request commit never leaves the session
un-tenanted.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.unit


# ─── Session instrumentation ───────────────────────────────────────────────────

APPLY_CONTEXT_SQL = (
    "SELECT set_config('app.org_id', :org_id, true), "
    "set_config('app.bypass_rls', :bypass_rls, true), "
    "set_config('app.lookup_code', :lookup_code, true)"
)
"""The single statement every context binding goes through."""


def _instrument(session: AsyncMock, events: list[str], label: str = "") -> list[str]:
    """Record the session's transaction-visible calls, in order.

    Mirrors SQLAlchemy's rule that ``commit``/``rollback`` on a session with
    no open transaction are no-ops: every entry says whether a transaction
    was open, so a test can prove that the release path's second rollback is
    harmless instead of merely tolerating two calls.

    Args:
        session: The mock session to instrument.
        events: The shared, ordered event log to append to.
        label: Prefix that identifies the session in the log.

    Returns:
        The event log, for convenience.
    """
    state = {"in_transaction": False}

    async def execute(statement: object, *args: object, **kwargs: object) -> AsyncMock:
        state["in_transaction"] = True
        events.append(f"{label}execute")
        return AsyncMock()

    async def commit() -> None:
        events.append(f"{label}commit(tx_open={state['in_transaction']})")
        state["in_transaction"] = False

    async def rollback() -> None:
        events.append(f"{label}rollback(tx_open={state['in_transaction']})")
        state["in_transaction"] = False

    session.execute.side_effect = execute
    session.commit.side_effect = commit
    session.rollback.side_effect = rollback
    return events


def _executed(session: AsyncMock) -> list[tuple[str, object | None]]:
    """The ``(sql, params)`` of every statement awaited on the session.

    Args:
        session: The instrumented mock session.

    Returns:
        One ``(sql, params)`` pair per awaited ``execute`` call, in order.
    """
    executed: list[tuple[str, object | None]] = []
    for call in session.execute.await_args_list:
        args, _ = call
        sql = str(args[0]) if args else ""
        params = args[1] if len(args) > 1 else None
        executed.append((sql, params))
    return executed


class TestGetDb:
    """get_db: yields AsyncSession with RLS context injection."""

    ORG_ID_STR = "00000000-0000-0000-0000-000000000001"

    @pytest.fixture
    def mock_session(self) -> MagicMock:
        session = AsyncMock(spec=AsyncSession)
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        session.close = AsyncMock()
        return session

    @pytest.fixture
    def mock_factory(self, mock_session: MagicMock) -> AsyncMock:
        """An async context manager that yields mock_session."""
        factory = MagicMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        factory.return_value = cm
        return factory

    @pytest.fixture
    def request_with_org(self) -> MagicMock:
        req = MagicMock()
        req.app.state.db_session_factory = MagicMock()
        req.state.org_id = self.ORG_ID_STR
        return req

    @pytest.fixture
    def request_without_org(self) -> MagicMock:
        req = MagicMock()
        req.app.state.db_session_factory = MagicMock()
        req.state.org_id = None
        return req

    @pytest.mark.asyncio
    async def test_yields_session_and_commits(
        self,
        mock_factory: AsyncMock,
        mock_session: MagicMock,
        request_with_org: MagicMock,
    ) -> None:
        """Happy path: yields the session, commits the data, releases the context.

        The context is INITIALISED before the handler runs — the tenant is
        bound, the bypass is pinned off and any lookup code left on the
        pooled connection is cleared.  The binding is **transaction-local**:
        one statement, all three GUCs, ``is_local => true``.

        It is RELEASED after the handler returns: the remembered context is
        dropped and the transaction is rolled back.  No statement is issued
        — the transaction-local GUC is already gone, so there is nothing
        left standing on the pooled connection to reset.
        """
        from dependencies.db import get_db

        request_with_org.app.state.db_session_factory = mock_factory
        events = _instrument(mock_session, [])

        gen = get_db(request_with_org)
        assert isinstance(gen, AsyncGenerator)

        session = await gen.__anext__()
        assert session is mock_session

        # The context is bound before the handler runs.
        init_sql, init_params = _executed(mock_session)[0]
        assert init_sql == APPLY_CONTEXT_SQL
        assert init_params == {
            "org_id": self.ORG_ID_STR,
            "bypass_rls": "false",
            "lookup_code": "",
        }

        # Finish the generator → the request's data is committed, then the
        # context is released.
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

        assert events == [
            "execute",  # the tenant binding
            "commit(tx_open=True)",  # the request's data
            "rollback(tx_open=False)",  # the release — nothing left to reset
        ]

    @pytest.mark.asyncio
    async def test_rolls_back_on_exception(
        self,
        mock_factory: AsyncMock,
        mock_session: MagicMock,
        request_with_org: MagicMock,
    ) -> None:
        """Exception in the yielded block → the handler's work is rolled back.

        The error path rolls back the open transaction; the release path
        then rolls back again — harmless, because the first rollback
        already closed the transaction (SQLAlchemy's rollback on a session
        with no open transaction is a no-op, hence ``tx_open=False``).
        """
        from dependencies.db import get_db

        request_with_org.app.state.db_session_factory = mock_factory
        events = _instrument(mock_session, [])

        gen = get_db(request_with_org)
        await gen.__anext__()

        with pytest.raises(RuntimeError, match="boom"):
            await gen.athrow(RuntimeError("boom"))

        assert events == [
            "execute",  # the tenant binding
            # The handler's transaction is dropped…
            "rollback(tx_open=True)",
            # …so the release path's rollback has nothing left to undo.
            "rollback(tx_open=False)",
        ]

    @pytest.mark.asyncio
    async def test_no_rls_when_org_id_missing(
        self,
        mock_factory: AsyncMock,
        mock_session: MagicMock,
        request_without_org: MagicMock,
    ) -> None:
        """No org_id in request.state → the context is initialised EMPTY.

        Every request initialises the context — a pooled connection may
        still carry the previous request's tenant — but an unauthenticated
        request gets an empty tenant: no org-scoped row is visible and no
        org-scoped write can succeed.
        """
        from dependencies.db import get_db

        request_without_org.app.state.db_session_factory = mock_factory
        events = _instrument(mock_session, [])

        gen = get_db(request_without_org)
        await gen.__anext__()

        init_sql, init_params = _executed(mock_session)[0]
        assert init_sql == APPLY_CONTEXT_SQL
        assert init_params == {
            # Empty tenant — not a stale one, and not an error.
            "org_id": "",
            "bypass_rls": "false",
            "lookup_code": "",
        }

        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

        assert events == [
            "execute",
            "commit(tx_open=True)",
            "rollback(tx_open=False)",
        ]

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_factory_missing(self) -> None:
        """No db_session_factory on app.state → RuntimeError."""
        from dependencies.db import get_db

        request = MagicMock()
        request.app.state.db_session_factory = None

        with pytest.raises(RuntimeError, match="db_session_factory not found"):
            async for _ in get_db(request):
                pass  # noqa


class TestGetDbSuperadmin:
    """get_db_superadmin: RLS context on the verify session + bypass session."""

    PLATFORM_ORG_ID_STR = "00000000-0000-0000-0000-000000000002"

    @pytest.fixture
    def request_with_session(self) -> MagicMock:
        req = MagicMock()
        req.app.state.db_session_factory = MagicMock()
        req.state.org_id = self.PLATFORM_ORG_ID_STR
        req.state.user_id = "00000000-0000-0000-0000-000000000003"
        return req

    @pytest.fixture
    def verify_and_bypass_sessions(self) -> tuple[AsyncMock, AsyncMock]:
        """Two sessions from the factory: the verify session + the bypass session."""
        verify = AsyncMock(spec=AsyncSession)
        bypass = AsyncMock(spec=AsyncSession)
        return verify, bypass

    @pytest.fixture
    def two_session_factory(
        self, verify_and_bypass_sessions: tuple[AsyncMock, AsyncMock]
    ) -> MagicMock:
        """An async context manager per factory() call, yielding the sessions."""
        verify, bypass = verify_and_bypass_sessions
        factory = MagicMock()
        factory.side_effect = [
            _cm(verify),
            _cm(bypass),
        ]
        return factory

    @pytest.mark.asyncio
    async def test_verify_session_sets_rls_context_before_superadmin_check(
        self,
        request_with_session: MagicMock,
        verify_and_bypass_sessions: tuple[AsyncMock, AsyncMock],
        two_session_factory: MagicMock,
    ) -> None:
        """The superadmin role check runs on a session with get_db's RLS context.

        Fail-closed: the tenant context is bound on the verify session
        BEFORE _ensure_superadmin's role lookup, so the lookup is subject
        to the same row visibility as every other query in the app — the
        bypass is pinned OFF, not on.  The context is released (the
        remembered context is dropped and the transaction is rolled back)
        before the pool takes the connection back.
        """
        from dependencies.auth import _ensure_superadmin  # noqa: F401  — patch target
        from dependencies.db import get_db_superadmin

        verify, bypass = verify_and_bypass_sessions
        request_with_session.app.state.db_session_factory = two_session_factory

        events: list[str] = []
        _instrument(verify, events, "verify:")
        _instrument(bypass, events, "bypass:")

        checked_on: list[object] = []

        async def record_ensure(*args: object, **kwargs: object) -> None:
            checked_on.append(args[3])
            events.append("verify:ensure_superadmin")

        with patch(
            "dependencies.auth._ensure_superadmin",
            new=AsyncMock(side_effect=record_ensure),
        ):
            gen = get_db_superadmin(request_with_session)
            session = await gen.__anext__()
            assert session is bypass

            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()

        # The role lookup ran on the verify session.
        assert checked_on == [verify]

        # The context is bound on the verify session, transaction-locally,
        # with the bypass pinned off and any lookup code cleared.
        init_sql, init_params = _executed(verify)[0]
        assert init_sql == APPLY_CONTEXT_SQL
        assert init_params == {
            "org_id": self.PLATFORM_ORG_ID_STR,
            "bypass_rls": "false",
            "lookup_code": "",
        }

        # The context is bound before the role check and released after it;
        # the bypass session is only opened once the check has passed.
        assert events == [
            "verify:execute",
            "verify:ensure_superadmin",
            "verify:rollback(tx_open=True)",
            "bypass:execute",
            "bypass:execute",
            "bypass:commit(tx_open=True)",
            "bypass:rollback(tx_open=False)",
        ]

    @pytest.mark.asyncio
    async def test_bypass_session_sets_bypass_after_verification(
        self,
        request_with_session: MagicMock,
        verify_and_bypass_sessions: tuple[AsyncMock, AsyncMock],
        two_session_factory: MagicMock,
    ) -> None:
        """The yielded bypass session gets bypass_rls='true' + the platform org.

        The bypass is set only AFTER the superadmin check passed.  It is
        transaction-local, so it cannot outlive the transaction — and the
        ``RlsAsyncSession`` re-binds it to the transaction that follows
        every commit, so a mid-request commit cannot silently drop it.
        """
        from core.config import PLATFORM_ORG_ID
        from dependencies.db import get_db_superadmin

        verify, bypass = verify_and_bypass_sessions
        request_with_session.app.state.db_session_factory = two_session_factory

        events: list[str] = []
        _instrument(verify, events, "verify:")
        _instrument(bypass, events, "bypass:")

        with patch("dependencies.auth._ensure_superadmin", new=AsyncMock()):
            gen = get_db_superadmin(request_with_session)
            session = await gen.__anext__()
            assert session is bypass

            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()

        # The bypass session is bound to the platform org…
        tenant_sql, tenant_params = _executed(bypass)[0]
        assert tenant_sql == APPLY_CONTEXT_SQL
        assert tenant_params == {
            "org_id": str(PLATFORM_ORG_ID),
            "bypass_rls": "false",
            "lookup_code": "",
        }

        # …and the bypass is enabled by a second binding, transaction-local.
        bypass_sql, bypass_params = _executed(bypass)[1]
        assert bypass_sql == APPLY_CONTEXT_SQL
        assert bypass_params == {
            "org_id": str(PLATFORM_ORG_ID),
            "bypass_rls": "true",
            "lookup_code": "",
        }

        assert events == [
            "verify:execute",
            "verify:rollback(tx_open=True)",
            "bypass:execute",
            "bypass:execute",
            "bypass:commit(tx_open=True)",
            "bypass:rollback(tx_open=False)",
        ]

    @pytest.mark.asyncio
    async def test_raises_runtime_error_when_state_missing(self) -> None:
        """No org_id/user_id on request.state → RuntimeError (fail-closed)."""
        from dependencies.db import get_db_superadmin

        request = MagicMock()
        request.app.state.db_session_factory = MagicMock()
        request.state.org_id = None
        request.state.user_id = None

        with pytest.raises(RuntimeError, match="requires an authenticated JWT"):
            async for _ in get_db_superadmin(request):
                pass  # noqa


def _cm(session: AsyncMock) -> AsyncMock:
    """Build an async context manager yielding ``session``."""
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return cm
