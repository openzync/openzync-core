"""Unit tests for dependencies/db.py — get_db async session dependency."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.unit


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
        self, mock_factory: AsyncMock, mock_session: MagicMock, request_with_org: MagicMock
    ) -> None:
        """Happy path: yields session, commits on success, sets RLS context."""
        from dependencies.db import get_db

        request_with_org.app.state.db_session_factory = mock_factory

        gen = get_db(request_with_org)
        assert isinstance(gen, AsyncGenerator)

        session = await gen.__anext__()
        assert session is mock_session

        # RLS context was set — session.execute was called for set_config calls
        assert mock_session.execute.await_count >= 2

        # Finish the generator → commit is called
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

        mock_session.commit.assert_awaited_once()
        mock_session.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rolls_back_on_exception(
        self, mock_factory: AsyncMock, mock_session: MagicMock, request_with_org: MagicMock
    ) -> None:
        """Exception in yielded block → session.rollback() called, exception re-raised."""
        from dependencies.db import get_db

        request_with_org.app.state.db_session_factory = mock_factory

        gen = get_db(request_with_org)
        await gen.__anext__()

        with pytest.raises(RuntimeError, match="boom"):
            await gen.athrow(RuntimeError("boom"))

        mock_session.rollback.assert_awaited_once()
        mock_session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_rls_when_org_id_missing(
        self, mock_factory: AsyncMock, mock_session: MagicMock, request_without_org: MagicMock
    ) -> None:
        """No org_id in request.state → RLS context is NOT set."""
        from dependencies.db import get_db

        request_without_org.app.state.db_session_factory = mock_factory

        gen = get_db(request_without_org)
        await gen.__anext__()

        # Without org_id, execute is NOT called for RLS — only internal session calls
        # Reset the call count from session creation, then verify no set_config calls
        mock_session.execute.reset_mock()

        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

        # session.execute should NOT have been called with set_config
        for call in mock_session.execute.call_args_list:
            args, _ = call
            if args:
                arg_str = str(args[0])
                assert "set_config" not in arg_str, f"Unexpected RLS call: {arg_str}"

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

        Fail-closed: set_config('app.org_id'...) and set_config
        ('app.bypass_rls', 'false'...) must execute on the verify session
        BEFORE _ensure_superadmin's role lookup, so the lookup is subject to
        the same row visibility as every other query in the app.
        """
        from dependencies.auth import _ensure_superadmin  # noqa: F401  — patch target
        from dependencies.db import get_db_superadmin

        verify, bypass = verify_and_bypass_sessions
        request_with_session.app.state.db_session_factory = two_session_factory

        events: list[tuple[str, object]] = []

        def record_execute(*args: object, **kwargs: object) -> AsyncMock:
            events.append(("set_config", str(args[0])))
            return AsyncMock()

        verify.execute.side_effect = record_execute

        async def record_ensure(*args: object, **kwargs: object) -> None:
            events.append(("ensure_superadmin", args[3]))

        with patch(
            "dependencies.auth._ensure_superadmin",
            new=AsyncMock(side_effect=record_ensure),
        ):
            gen = get_db_superadmin(request_with_session)
            session = await gen.__anext__()
            assert session is bypass

            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()

        # RLS context set before the role check; bypass only on the yielded session.
        assert events == [
            ("set_config", "SELECT set_config('app.org_id', :org_id, true)"),
            ("set_config", "SELECT set_config('app.bypass_rls', 'false', true)"),
            ("ensure_superadmin", verify),
        ]

    @pytest.mark.asyncio
    async def test_bypass_session_sets_bypass_after_verification(
        self,
        request_with_session: MagicMock,
        verify_and_bypass_sessions: tuple[AsyncMock, AsyncMock],
        two_session_factory: MagicMock,
    ) -> None:
        """The yielded bypass session gets bypass_rls='true' + platform org_id."""
        from dependencies.db import get_db_superadmin

        verify, bypass = verify_and_bypass_sessions
        request_with_session.app.state.db_session_factory = two_session_factory

        with patch(
            "dependencies.auth._ensure_superadmin", new=AsyncMock()
        ):
            gen = get_db_superadmin(request_with_session)
            session = await gen.__anext__()
            assert session is bypass

            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()

        set_config_calls = [str(c.args[0]) for c in bypass.execute.await_args_list]
        assert any(
            "set_config('app.bypass_rls', 'true', true)" in c
            for c in set_config_calls
        )
        assert any(
            "set_config('app.org_id', :org_id, true)" in c
            for c in set_config_calls
        )
        bypass.commit.assert_awaited_once()
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
