"""Unit tests for per-org SurrealDB connection pool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core.exceptions import ServiceUnavailableError


def _make_org_config(**overrides: dict) -> MagicMock:
    """Build a mock OrgConfigBase with SurrealDB fields."""
    cfg = MagicMock()
    cfg.surrealdb_url = overrides.get("url", "ws://localhost:8000/rpc")
    cfg.surrealdb_user = overrides.get("user", "root")
    cfg.surrealdb_pass = overrides.get("password", "root")
    cfg.surrealdb_namespace = overrides.get("namespace", "openzync")
    cfg.surrealdb_database = overrides.get("database", "openzync")
    return cfg


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
    """get_or_create creates and caches per-org connections."""

    @pytest.mark.asyncio
    async def test_creates_new_connection(self) -> None:
        """New AsyncSurreal is created for a new org."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_id = uuid4()
        org_config = _make_org_config()

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            result = await pool.get_or_create(org_id, org_config)

            assert result is mock_surreal
            mock_surreal_cls.assert_called_once_with("ws://localhost:8000/rpc")
            mock_surreal.connect.assert_awaited_once()
            mock_surreal.signin.assert_awaited_once_with(
                {"username": "root", "password": "root"},
            )
            mock_surreal.use.assert_awaited_once_with("openzync", "openzync")

    @pytest.mark.asyncio
    async def test_returns_cached_connection(self) -> None:
        """Second call for same org returns cached connection."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_id = uuid4()
        org_config = _make_org_config()

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            conn1 = await pool.get_or_create(org_id, org_config)
            conn2 = await pool.get_or_create(org_id, org_config)

            assert conn1 is conn2  # same instance
            assert mock_surreal_cls.call_count == 1  # only created once
            assert mock_surreal.connect.call_count == 1

    @pytest.mark.asyncio
    async def test_different_orgs_get_different_connections(self) -> None:
        """Separate orgs get separate AsyncSurreal instances."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_a = uuid4()
        org_b = uuid4()
        org_config = _make_org_config()

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal_a = AsyncMock()
            mock_surreal_b = AsyncMock()
            mock_surreal_cls.side_effect = [mock_surreal_a, mock_surreal_b]

            conn_a = await pool.get_or_create(org_a, org_config)
            conn_b = await pool.get_or_create(org_b, org_config)

            assert conn_a is mock_surreal_a
            assert conn_b is mock_surreal_b
            assert conn_a is not conn_b
            assert mock_surreal_cls.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_when_no_url_configured(self) -> None:
        """GraphBackendUnavailableError is raised when url is empty."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_id = uuid4()
        org_config = _make_org_config(url=None)

        with pytest.raises(ServiceUnavailableError, match="Failed to connect to SurrealDB"):
            await pool.get_or_create(org_id, org_config)

    @pytest.mark.asyncio
    async def test_raises_on_connection_failure(self) -> None:
        """GraphBackendUnavailableError is raised when connect fails."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_id = uuid4()
        org_config = _make_org_config()

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal = AsyncMock()
            mock_surreal.connect.side_effect = Exception("Connection refused")
            mock_surreal_cls.return_value = mock_surreal

            with pytest.raises(ServiceUnavailableError, match="SurrealDB connection failed"):
                await pool.get_or_create(org_id, org_config)

    @pytest.mark.asyncio
    async def test_uses_system_url_when_provided(self) -> None:
        """System URL override is used instead of org_config url."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_id = uuid4()
        org_config = _make_org_config(
            url=None,  # org-level URL not set
            user="org_user",
            password="org_pass",
            namespace="ns1",
            database="db1",
        )

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            result = await pool.get_or_create(
                org_id, org_config, system_url="ws://system:8000/rpc",
            )

            assert result is mock_surreal
            mock_surreal_cls.assert_called_once_with("ws://system:8000/rpc")
            mock_surreal.signin.assert_awaited_once_with(
                {"username": "org_user", "password": "org_pass"},
            )
            mock_surreal.use.assert_awaited_once_with("ns1", "db1")

    @pytest.mark.asyncio
    async def test_uses_defaults_for_missing_config_fields(self) -> None:
        """Default user/pass/namespace/database are used when org config has None."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_id = uuid4()
        org_config = _make_org_config(
            url="ws://localhost:8000/rpc",
            user=None,
            password=None,
            namespace=None,
            database=None,
        )

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            await pool.get_or_create(org_id, org_config)

            mock_surreal.signin.assert_awaited_once_with(
                {"username": "root", "password": "root"},
            )
            mock_surreal.use.assert_awaited_once_with("openzync", "openzync")

    @pytest.mark.asyncio
    async def test_concurrent_access_returns_same_connection(self) -> None:
        """Concurrent requests for same org return the same connection (lock)."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_id = uuid4()
        org_config = _make_org_config()

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal

            # Simulate two concurrent calls
            import asyncio

            async def call() -> None:
                await pool.get_or_create(org_id, org_config)

            await asyncio.gather(call(), call())
            # Only one AsyncSurreal should have been created
            assert mock_surreal_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_updates_last_used_on_cache_hit(self) -> None:
        """Cache hit updates last_used timestamp."""
        from core.surreal_pool import SurrealConnectionPool

        pool = SurrealConnectionPool()
        org_id = uuid4()
        org_config = _make_org_config()

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal = AsyncMock()
            mock_surreal_cls.return_value = mock_surreal
            await pool.get_or_create(org_id, org_config)

            first_used = pool._pool[org_id]["last_used"]

            import time
            time.sleep(0.001)
            await pool.get_or_create(org_id, org_config)

            second_used = pool._pool[org_id]["last_used"]
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

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal_a = AsyncMock()
            mock_surreal_b = AsyncMock()
            mock_surreal_cls.side_effect = [mock_surreal_a, mock_surreal_b]

            await pool.get_or_create(org_a, _make_org_config())
            await pool.get_or_create(org_b, _make_org_config())

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

        with patch("core.surreal_pool.AsyncSurreal") as mock_surreal_cls:
            mock_surreal_a = AsyncMock()
            mock_surreal_a.close.side_effect = Exception("Close failed")
            mock_surreal_b = AsyncMock()
            mock_surreal_cls.side_effect = [mock_surreal_a, mock_surreal_b]

            await pool.get_or_create(org_a, _make_org_config())
            await pool.get_or_create(org_b, _make_org_config())

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
