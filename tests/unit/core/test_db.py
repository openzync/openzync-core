"""Unit tests for core database infrastructure (engine, session, health)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest.mark.unit
class TestInitDbEngine:
    """init_db_engine URL parsing, validation, and defaults."""

    def test_asyncpg_url_accepted(self) -> None:
        """Engine is created when URL uses postgresql+asyncpg:// scheme."""
        from core.db import init_db_engine

        with patch("core.db.create_async_engine") as mock_create:
            engine = init_db_engine("postgresql+asyncpg://u:p@localhost:5432/test")
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            assert "postgresql+asyncpg://" in str(args[0])

    def test_sync_postgres_url_raises(self) -> None:
        """Engine raises ValueError when URL lacks +asyncpg driver."""
        from core.db import init_db_engine

        with pytest.raises(ValueError, match="must use the postgresql\\+asyncpg:// scheme"):
            init_db_engine("postgresql://u:p@localhost:5432/test")

    def test_non_postgres_url_passes_through(self) -> None:
        """Non-postgres URLs (e.g. sqlite) bypass the asyncpg check."""
        from core.db import init_db_engine

        with patch("core.db.create_async_engine") as mock_create:
            init_db_engine("sqlite+aiosqlite:///test.db")
            mock_create.assert_called_once()

    def test_default_pool_settings(self) -> None:
        """Engine is created with sensible pool defaults."""
        from core.db import init_db_engine

        with patch("core.db.create_async_engine") as mock_create:
            init_db_engine("postgresql+asyncpg://u:p@localhost:5432/test")
            _args, kwargs = mock_create.call_args
            assert kwargs["pool_pre_ping"] is True
            assert kwargs["pool_size"] == 20
            assert kwargs["max_overflow"] == 10

    def test_custom_pool_settings_override(self) -> None:
        """Caller-supplied kwargs override defaults."""
        from core.db import init_db_engine

        with patch("core.db.create_async_engine") as mock_create:
            init_db_engine(
                "postgresql+asyncpg://u:p@localhost:5432/test",
                pool_size=5,
                max_overflow=2,
            )
            _args, kwargs = mock_create.call_args
            assert kwargs["pool_size"] == 5
            assert kwargs["max_overflow"] == 2

    def test_connect_args_always_set(self) -> None:
        """statement_cache_size=0 is always passed."""
        from core.db import init_db_engine

        with patch("core.db.create_async_engine") as mock_create:
            init_db_engine("postgresql+asyncpg://u:p@localhost:5432/test")
            _args, kwargs = mock_create.call_args
            assert kwargs["connect_args"] == {"statement_cache_size": 0}


@pytest.mark.unit
class TestCloseDbEngine:
    """close_db_engine calls dispose on the engine."""

    @pytest.mark.asyncio
    async def test_dispose_called(self) -> None:
        """Engine.dispose() is awaited on close."""
        from core.db import close_db_engine

        mock_engine = AsyncMock(spec=AsyncEngine)
        await close_db_engine(mock_engine)
        mock_engine.dispose.assert_awaited_once()


@pytest.mark.unit
class TestGetAsyncSession:
    """get_async_session returns a properly configured sessionmaker."""

    def test_returns_async_sessionmaker(self) -> None:
        """Factory produces AsyncSession instances."""
        from core.db import get_async_session

        mock_engine = MagicMock(spec=AsyncEngine)
        factory = get_async_session(mock_engine)
        assert factory is not None
        # The factory class produces AsyncSession
        assert factory.class_ is AsyncSession

    def test_sessionmaker_configured(self) -> None:
        """Factory has expire_on_commit=False and correct bind."""
        from core.db import get_async_session

        mock_engine = MagicMock(spec=AsyncEngine)
        factory = get_async_session(mock_engine)
        assert factory.kw.get("expire_on_commit") is False
        assert factory.kw.get("bind") is mock_engine


@pytest.mark.unit
class TestGetDbDependency:
    """FastAPI dependency get_db yields sessions from app.state."""

    @pytest.mark.asyncio
    async def test_yields_session_when_factory_present(self) -> None:
        """Session is yielded when db_session_factory is set on app.state."""
        from core.db import get_db

        mock_session = AsyncMock(spec=AsyncSession)
        # The factory is an async context manager — __aenter__ returns the session
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session

        request = MagicMock()
        request.app.state.db_session_factory = mock_factory

        gen = get_db(request)
        session = await gen.__anext__()
        assert session is mock_session

        # Cleanup
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    @pytest.mark.asyncio
    async def test_raises_when_factory_missing(self) -> None:
        """RuntimeError is raised when db_session_factory is not on state."""
        from core.db import get_db

        request = MagicMock()
        # Simulate missing attribute — getattr returns None
        del request.app.state.db_session_factory

        gen = get_db(request)
        with pytest.raises(RuntimeError, match="db_session_factory not found"):
            await gen.__anext__()


@pytest.mark.unit
class TestCoreDbReexport:
    """core.db.get_db must remain the RLS-aware dependencies.db.get_db."""

    def test_core_db_reexports_rls_get_db(self) -> None:
        """core.db re-exports the RLS dependency rather than a local duplicate.

        A future revert to a local non-RLS ``get_db`` in core/db.py (a
        pattern that silently disables row-level security) fails here
        because the two import paths must resolve to the same function.
        """
        from core.db import get_db as core_get_db
        from dependencies.db import get_db as deps_get_db

        assert core_get_db is deps_get_db


@pytest.mark.unit
class TestDbHealth:
    """check_db_health connectivity check."""

    @pytest.mark.asyncio
    async def test_healthy(self) -> None:
        """Returns True when SELECT 1 succeeds."""
        from core.db import check_db_health

        mock_engine = AsyncMock(spec=AsyncEngine)
        mock_conn = AsyncMock()
        mock_engine.connect.return_value.__aenter__.return_value = mock_conn
        mock_conn.execute.return_value = MagicMock()
        mock_conn.execute.return_value.scalar.return_value = 1

        result = await check_db_health(mock_engine)
        assert result is True
        mock_conn.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unhealthy_connection_refused(self) -> None:
        """Returns False when connect raises."""
        from core.db import check_db_health

        mock_engine = AsyncMock(spec=AsyncEngine)
        mock_engine.connect.side_effect = Exception("Connection refused")

        result = await check_db_health(mock_engine)
        assert result is False

    @pytest.mark.asyncio
    async def test_unhealthy_query_fails(self) -> None:
        """Returns False when execute raises."""
        from core.db import check_db_health

        mock_engine = AsyncMock(spec=AsyncEngine)
        mock_conn = AsyncMock()
        mock_engine.connect.return_value.__aenter__.return_value = mock_conn
        mock_conn.execute.side_effect = Exception("Query timeout")

        result = await check_db_health(mock_engine)
        assert result is False
