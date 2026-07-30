"""Unit tests for ARQ pool singleton and ARQPool lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestARQPoolInit:
    """ARQPool construction and URL resolution."""

    def test_url_from_arg(self) -> None:
        """URL is taken from the constructor argument when provided."""
        from core.arq import ARQPool

        pool = ARQPool(redis_url="redis://custom:6379/2")
        assert pool._redis_url == "redis://custom:6379/2"

    def test_url_from_settings_when_not_provided(self) -> None:
        """URL falls back to settings.REDIS_URL when not provided."""
        from core.arq import ARQPool

        pool = ARQPool()
        assert pool._redis_url == "redis://localhost:6379/1"  # from conftest

    def test_pool_is_none_after_init(self) -> None:
        """Pool is None until initialize() is called."""
        from core.arq import ARQPool

        pool = ARQPool()
        assert pool._pool is None


@pytest.mark.unit
class TestARQPoolInitialize:
    """ARQPool.initialize() connects to Redis."""

    @pytest.mark.asyncio
    async def test_creates_pool_successfully(self) -> None:
        """Pool is created when Redis is reachable."""
        from core.arq import ARQPool

        mock_pool = MagicMock()

        with (
            patch("core.arq.RedisSettings.from_dsn") as mock_dsn,
            patch("core.arq.create_pool", new_callable=AsyncMock) as mock_create,
        ):
            mock_create.return_value = mock_pool
            pool = ARQPool(redis_url="redis://localhost:6379/1")
            await pool.initialize()

            mock_dsn.assert_called_once_with("redis://localhost:6379/1")
            mock_create.assert_awaited_once()
            assert pool._pool is mock_pool

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_failure(self) -> None:
        """ConnectionError is raised when Redis is unreachable."""
        from core.arq import ARQPool

        with (
            patch("core.arq.RedisSettings.from_dsn") as mock_dsn,
            patch("core.arq.create_pool", new_callable=AsyncMock) as mock_create,
        ):
            mock_create.side_effect = Exception("Connection refused")
            pool = ARQPool(redis_url="redis://localhost:6379/1")

            with pytest.raises(ConnectionError, match="Failed to connect to Redis"):
                await pool.initialize()

            assert pool._pool is None


@pytest.mark.unit
class TestARQPoolClose:
    """ARQPool.close() shuts down the pool gracefully."""

    @pytest.mark.asyncio
    async def test_closes_pool_when_initialized(self) -> None:
        """Pool.close() and wait_closed() are called, pool set to None."""
        from core.arq import ARQPool

        mock_pool = AsyncMock()
        pool = ARQPool(redis_url="redis://localhost:6379/1")
        pool._pool = mock_pool

        await pool.close()

        mock_pool.close.assert_awaited_once()
        mock_pool.wait_closed.assert_awaited_once()
        assert pool._pool is None

    @pytest.mark.asyncio
    async def test_safe_to_call_multiple_times(self) -> None:
        """Second call to close is a no-op."""
        from core.arq import ARQPool

        pool = ARQPool(redis_url="redis://localhost:6379/1")
        pool._pool = AsyncMock()
        await pool.close()
        assert pool._pool is None
        # Second call should not raise
        await pool.close()

    @pytest.mark.asyncio
    async def test_safe_to_call_when_not_initialized(self) -> None:
        """Close is a no-op when pool was never initialized."""
        from core.arq import ARQPool

        pool = ARQPool(redis_url="redis://localhost:6379/1")
        await pool.close()  # should not raise


@pytest.mark.unit
class TestARQPoolAccessor:
    """ARQPool.pool property and enqueue."""

    def test_pool_accessible_when_initialized(self) -> None:
        """Pool property returns the underlying pool when set."""
        from core.arq import ARQPool

        mock_pool = MagicMock()
        pool = ARQPool()
        pool._pool = mock_pool
        assert pool.pool is mock_pool

    def test_pool_raises_when_not_initialized(self) -> None:
        """Pool property raises RuntimeError when not initialized."""
        from core.arq import ARQPool

        pool = ARQPool()
        with pytest.raises(RuntimeError, match="has not been initialised"):
            _ = pool.pool

    @pytest.mark.asyncio
    async def test_enqueue_without_queue(self) -> None:
        """Enqueue sends task to default queue and returns job ID."""
        from core.arq import ARQPool

        mock_job = MagicMock()
        mock_job.job_id = "job-123"
        mock_pool = AsyncMock()
        mock_pool.enqueue_job.return_value = mock_job

        pool = ARQPool()
        pool._pool = mock_pool

        job_id = await pool.enqueue("send_notification", user_id=42)
        assert job_id == "job-123"
        mock_pool.enqueue_job.assert_awaited_once_with(
            "send_notification",
            user_id=42,
        )

    @pytest.mark.asyncio
    async def test_enqueue_with_queue_name(self) -> None:
        """Enqueue passes _queue_name when queue is specified."""
        from core.arq import ARQPool

        mock_job = MagicMock()
        mock_job.job_id = "job-456"
        mock_pool = AsyncMock()
        mock_pool.enqueue_job.return_value = mock_job

        pool = ARQPool()
        pool._pool = mock_pool

        job_id = await pool.enqueue("process", queue_name="high", order_id="abc")
        assert job_id == "job-456"
        mock_pool.enqueue_job.assert_awaited_once_with(
            "process",
            order_id="abc",
            _queue_name="high",
        )

    @pytest.mark.asyncio
    async def test_enqueue_returns_none_when_job_none(self) -> None:
        """Enqueue returns None when ARQ returns None."""
        from core.arq import ARQPool

        mock_pool = AsyncMock()
        mock_pool.enqueue_job.return_value = None

        pool = ARQPool()
        pool._pool = mock_pool

        job_id = await pool.enqueue("send_notification")
        assert job_id is None

    @pytest.mark.asyncio
    async def test_enqueue_raises_when_not_initialized(self) -> None:
        """Enqueue raises RuntimeError when pool not initialized."""
        from core.arq import ARQPool

        pool = ARQPool()
        with pytest.raises(RuntimeError, match="has not been initialised"):
            await pool.enqueue("task")


@pytest.mark.unit
class TestArqSingleton:
    """Module-level init_arq / close_arq / get_arq singleton."""

    @pytest.mark.asyncio
    async def test_init_creates_and_returns_pool(self) -> None:
        """init_arq creates the global pool singleton."""
        from core import arq as arq_module

        # Ensure singleton is None
        arq_module._pool = None

        with (
            patch("core.arq.RedisSettings.from_dsn"),
            patch("core.arq.create_pool", new_callable=AsyncMock) as mock_create,
        ):
            mock_create.return_value = MagicMock()
            pool = await arq_module.init_arq("redis://localhost:6379/1")

            assert arq_module._pool is pool
            assert pool._pool is not None

    @pytest.mark.asyncio
    async def test_get_arq_returns_pool_after_init(self) -> None:
        """get_arq returns the initialised pool."""
        from core import arq as arq_module

        arq_module._pool = None

        with (
            patch("core.arq.RedisSettings.from_dsn"),
            patch("core.arq.create_pool", new_callable=AsyncMock),
        ):
            await arq_module.init_arq("redis://localhost:6379/1")
            retrieved = arq_module.get_arq()
            assert retrieved is arq_module._pool

    def test_get_arq_raises_before_init(self) -> None:
        """get_arq raises RuntimeError before init_arq is called."""
        from core import arq as arq_module

        arq_module._pool = None
        with pytest.raises(RuntimeError, match="has not been initialised"):
            arq_module.get_arq()

    @pytest.mark.asyncio
    async def test_close_arq_clears_singleton(self) -> None:
        """close_arq closes the pool and sets singleton to None."""
        from core import arq as arq_module

        mock_pool = AsyncMock()
        arq_module._pool = MagicMock()
        arq_module._pool.close = AsyncMock()

        await arq_module.close_arq()
        assert arq_module._pool is None

    @pytest.mark.asyncio
    async def test_close_arq_safe_when_none(self) -> None:
        """close_arq is a no-op when singleton is None."""
        from core import arq as arq_module

        arq_module._pool = None
        await arq_module.close_arq()  # should not raise

    @pytest.mark.asyncio
    async def test_reinit_closes_existing_pool(self) -> None:
        """init_arq closes existing pool before creating a new one."""
        from core import arq as arq_module

        # Set up an existing pool — using AsyncMock so .close() is an auto-created AsyncMock
        existing = AsyncMock()
        arq_module._pool = existing

        with (
            patch("core.arq.RedisSettings.from_dsn"),
            patch("core.arq.create_pool", new_callable=AsyncMock) as mock_create,
        ):
            mock_create.return_value = MagicMock()
            await arq_module.init_arq("redis://localhost:6379/1")
            existing.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_init_raises_on_failure(self) -> None:
        """init_arq propagates ConnectionError when Redis is down."""
        from core import arq as arq_module

        arq_module._pool = None

        with (
            patch("core.arq.RedisSettings.from_dsn"),
            patch("core.arq.create_pool", new_callable=AsyncMock) as mock_create,
        ):
            mock_create.side_effect = Exception("Connection refused")
            with pytest.raises(ConnectionError):
                await arq_module.init_arq("redis://localhost:6379/1")
            assert arq_module._pool is not None  # _pool is set before init
