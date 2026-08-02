"""Unit tests for core Redis connection management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestInitRedis:
    """init_redis creates a properly configured async Redis client."""

    def test_redis_from_url_called_with_correct_url(self) -> None:
        """Client is created using aioredis.from_url with the given URL."""
        from core.redis import init_redis

        with patch("core.redis.aioredis.from_url") as mock_from_url:
            mock_from_url.return_value = MagicMock()
            client = init_redis("redis://localhost:6379/0")

            mock_from_url.assert_called_once()
            args, kwargs = mock_from_url.call_args
            assert args[0] == "redis://localhost:6379/0"

    def test_default_connection_settings(self) -> None:
        """Client is created with sensible defaults."""
        from core.redis import init_redis

        with patch("core.redis.aioredis.from_url") as mock_from_url:
            mock_from_url.return_value = MagicMock()
            init_redis("redis://localhost:6379/0")

            _args, kwargs = mock_from_url.call_args
            assert kwargs["encoding"] == "utf-8"
            assert kwargs["decode_responses"] is True
            assert kwargs["socket_connect_timeout"] == 5
            assert kwargs["socket_timeout"] == 10
            assert kwargs["retry_on_timeout"] is True
            assert kwargs["health_check_interval"] == 30
            assert kwargs["max_connections"] == 50

    def test_redis_scheme_accepted(self) -> None:
        """Standard redis:// scheme works."""
        from core.redis import init_redis

        with patch("core.redis.aioredis.from_url") as mock_from_url:
            mock_from_url.return_value = MagicMock()
            client = init_redis("redis://localhost:6379/0")
            assert client is not None

    def test_rediss_scheme_accepted(self) -> None:
        """TLS rediss:// scheme works."""
        from core.redis import init_redis

        with patch("core.redis.aioredis.from_url") as mock_from_url:
            mock_from_url.return_value = MagicMock()
            client = init_redis("rediss://localhost:6379/0")
            assert client is not None

    def test_unix_socket_scheme_accepted(self) -> None:
        """Unix socket redis:// scheme works."""
        from core.redis import init_redis

        with patch("core.redis.aioredis.from_url") as mock_from_url:
            mock_from_url.return_value = MagicMock()
            client = init_redis("redis:///var/run/redis/redis.sock")
            assert client is not None


@pytest.mark.unit
class TestCloseRedis:
    """close_redis closes the client gracefully."""

    @pytest.mark.asyncio
    async def test_aclose_called(self) -> None:
        """Client.aclose() is awaited on close."""
        from core.redis import close_redis

        mock_client = AsyncMock()
        await close_redis(mock_client)
        mock_client.aclose.assert_awaited_once()


@pytest.mark.unit
class TestGetRedisDependency:
    """FastAPI dependency get_redis returns client from app.state."""

    @pytest.mark.asyncio
    async def test_returns_client_when_present(self) -> None:
        """Returns the Redis client when set on app.state."""
        from core.redis import get_redis

        mock_client = MagicMock()
        request = MagicMock()
        request.app.state.redis = mock_client

        client = await get_redis(request)
        assert client is mock_client

    @pytest.mark.asyncio
    async def test_raises_when_missing(self) -> None:
        """RuntimeError is raised when redis is not on app.state."""
        from core.redis import get_redis

        request = MagicMock()
        del request.app.state.redis

        with pytest.raises(RuntimeError, match="Redis client not found"):
            await get_redis(request)


@pytest.mark.unit
class TestRedisHealth:
    """check_redis_health connectivity check."""

    @pytest.mark.asyncio
    async def test_healthy(self) -> None:
        """Returns True when PING gets PONG."""
        from core.redis import check_redis_health

        mock_client = AsyncMock()
        mock_client.ping.return_value = True

        result = await check_redis_health(mock_client)
        assert result is True
        mock_client.ping.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unhealthy(self) -> None:
        """Returns False when ping raises."""
        from core.redis import check_redis_health

        mock_client = AsyncMock()
        mock_client.ping.side_effect = Exception("Connection refused")

        result = await check_redis_health(mock_client)
        assert result is False

    @pytest.mark.asyncio
    async def test_unhealthy_returns_false_not_pong(self) -> None:
        """Returns False when ping returns falsy."""
        from core.redis import check_redis_health

        mock_client = AsyncMock()
        mock_client.ping.return_value = False

        result = await check_redis_health(mock_client)
        assert result is False
