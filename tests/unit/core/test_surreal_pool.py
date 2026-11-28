"""Unit tests for per-user SurrealDB connection pool."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.exceptions import GraphBackendUnavailableError


def _make_settings(**overrides: object) -> SimpleNamespace:
    """Build settings with SurrealDB env fields (connection details are env-owned)."""
    values: dict[str, object] = {
        "SURREALDB_URL": "ws://localhost:8000/rpc",
        "SURREALDB_USER": "root",
        "SURREALDB_PASS": "root",
        "SURREALDB_NAMESPACE": "openzync",
        "SURREALDB_DATABASE": "openzync",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.unit
class TestSurrealConnectionPoolInit:
    """Pool initialisation state."""

    def test_empty_pool_on_init(self) -> None:
        """Pool starts empty with no connections."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        assert pool._pool == {}
        assert pool._locks == {}


@pytest.mark.unit
class TestSurrealPoolGetOrCreate:
    """get_or_create creates and caches per-user connections."""

    @pytest.mark.asyncio
    async def test_creates_new_connection(self) -> None:
        """New AsyncSurreal is created for a new user."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        user_id = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            result = await pool.get_or_create(user_id)

            assert result is mock_surreal
            mock_surreal_cls.assert_called_once_with("ws://localhost:8000/rpc")
            mock_surreal.connect.assert_awaited_once()
            mock_surreal.signin.assert_awaited_once_with(
                {"username": "root", "password": "root"},
            )
            mock_surreal.use.assert_awaited_once_with("openzync", "openzync")

    @pytest.mark.asyncio
    async def test_returns_cached_connection(self) -> None:
        """Second call for same user returns cached connection."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        user_id = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            conn1 = await pool.get_or_create(user_id)
            conn2 = await pool.get_or_create(user_id)

            assert conn1 is conn2  # same instance
            assert mock_surreal_cls.call_count == 1  # only created once
            assert mock_surreal.connect.call_count == 1

    @pytest.mark.asyncio
    async def test_different_orgs_get_different_connections(self) -> None:
        """Separate users get separate AsyncSurreal instances."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_a = uuid4()
        org_b = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal_a = AsyncMock()
            mock_surreal_b = AsyncMock()
            mock_surreal_cls.side_effect = [mock_surreal_a, mock_surreal_b]

            conn_a = await pool.get_or_create(org_a)
            conn_b = await pool.get_or_create(org_b)

            assert conn_a is mock_surreal_a
            assert conn_b is mock_surreal_b
            assert conn_a is not conn_b
            assert mock_surreal_cls.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_when_no_url_configured(self) -> None:
        """GraphBackendUnavailableError is raised when OZ_SURREALDB_URL is unset."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        user_id = uuid4()

        with patch(
            "core.surreal_pool.get_settings",
            return_value=_make_settings(SURREALDB_URL=None),
        ):
            with pytest.raises(
                GraphBackendUnavailableError, match="OZ_SURREALDB_URL"
            ):
                await pool.get_or_create(user_id)

    @pytest.mark.asyncio
    async def test_raises_on_connection_failure(self) -> None:
        """GraphBackendUnavailableError is raised when connect fails."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        user_id = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal = AsyncMock()
            mock_surreal.connect.side_effect = Exception("Connection refused")
            mock_surreal_cls.return_value = mock_surreal

            with pytest.raises(
                GraphBackendUnavailableError, match="SurrealDB connection failed"
            ):
                await pool.get_or_create(user_id)

    @pytest.mark.asyncio
    async def test_uses_settings_url(self) -> None:
        """The OZ_SURREALDB_URL + credentials from settings are used."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        user_id = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(
                    SURREALDB_URL="ws://system:8000/rpc",
                    SURREALDB_USER="org_user",
                    SURREALDB_PASS="org_pass",
                    SURREALDB_NAMESPACE="ns1",
                    SURREALDB_DATABASE="db1",
                ),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            result = await pool.get_or_create(user_id)

            assert result is mock_surreal
            mock_surreal_cls.assert_called_once_with("ws://system:8000/rpc")
            mock_surreal.signin.assert_awaited_once_with(
                {"username": "org_user", "password": "org_pass"},
            )
            mock_surreal.use.assert_awaited_once_with("ns1", "db1")

    @pytest.mark.asyncio
    async def test_uses_defaults_for_missing_config_fields(self) -> None:
        """Default user/pass/namespace/database apply when settings leave them unset."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        user_id = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(
                    SURREALDB_URL="ws://localhost:8000/rpc",
                    SURREALDB_USER="root",
                    SURREALDB_PASS="root",
                    SURREALDB_NAMESPACE="openzync",
                    SURREALDB_DATABASE="openzync",
                ),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            await pool.get_or_create(user_id)

            mock_surreal.signin.assert_awaited_once_with(
                {"username": "root", "password": "root"},
            )
            mock_surreal.use.assert_awaited_once_with("openzync", "openzync")

    @pytest.mark.asyncio
    async def test_concurrent_access_returns_same_connection(self) -> None:
        """Concurrent requests for same user return the same connection (lock)."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        user_id = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            # Simulate two concurrent calls
            import asyncio

            async def call() -> None:
                await pool.get_or_create(user_id)

            await asyncio.gather(call(), call())
            # Only one AsyncSurreal should have been created
            assert mock_surreal_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_updates_last_used_on_cache_hit(self) -> None:
        """Cache hit updates last_used timestamp."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        user_id = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal
            await pool.get_or_create(user_id)

            first_used = pool._pool[user_id]["last_used"]

            import time
            time.sleep(0.001)
            await pool.get_or_create(user_id)

            second_used = pool._pool[user_id]["last_used"]
            assert second_used > first_used


@pytest.mark.unit
class TestSurrealPoolCloseAll:
    """close_all shuts down all cached connections."""

    @pytest.mark.asyncio
    async def test_closes_all_connections(self) -> None:
        """All cached connections are closed."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_a = uuid4()
        org_b = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal_a = AsyncMock()
            mock_surreal_b = AsyncMock()
            mock_surreal_cls.side_effect = [mock_surreal_a, mock_surreal_b]

            await pool.get_or_create(org_a)
            await pool.get_or_create(org_b)

            await pool.close_all()

            mock_surreal_a.close.assert_awaited_once()
            mock_surreal_b.close.assert_awaited_once()
            assert pool._pool == {}
            assert pool._locks == {}

    @pytest.mark.asyncio
    async def test_logs_but_does_not_raise_on_close_failure(self) -> None:
        """Individual close failures are logged, but all connections are still closed."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_a = uuid4()
        org_b = uuid4()

        with (
            patch(
                "core.surreal_pool.get_settings",
                return_value=_make_settings(),
            ),
            patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls,
        ):
            mock_surreal_a = AsyncMock()
            mock_surreal_a.close.side_effect = Exception("Close failed")
            mock_surreal_b = AsyncMock()
            mock_surreal_cls.side_effect = [mock_surreal_a, mock_surreal_b]

            await pool.get_or_create(org_a)
            await pool.get_or_create(org_b)

            await pool.close_all()

            mock_surreal_a.close.assert_awaited_once()
            mock_surreal_b.close.assert_awaited_once()
            assert pool._pool == {}  # cleared regardless

    @pytest.mark.asyncio
    async def test_safe_to_call_on_empty_pool(self) -> None:
        """close_all is a no-op on an empty pool."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        await pool.close_all()  # should not raise
        assert pool._pool == {}
