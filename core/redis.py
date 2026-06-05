"""Redis connection management and FastAPI dependency.

Usage in a FastAPI application:

    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from core.config import settings
    from core.redis import init_redis, close_redis

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis_client = init_redis(str(settings.REDIS_URL))
        app.state.redis = redis_client
        yield
        await close_redis(redis_client)

    app = FastAPI(lifespan=lifespan)

Then in routers:

    from fastapi import Depends
    from core.redis import get_redis

    @router.get("/health")
    async def health(redis: redis.asyncio.Redis = Depends(get_redis)): ...
"""

from __future__ import annotations

import logging

from fastapi import Request
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


def init_redis(redis_url: str) -> aioredis.Redis:
    """Create and return an async Redis client with connection pooling.

    Args:
        redis_url: Redis connection string (e.g. ``redis://localhost:6379/0``).

    Returns:
        A configured :class:`redis.asyncio.Redis` instance.

    Raises:
        ValueError: If the URL scheme is not supported.
    """
    client = aioredis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
        retry_on_timeout=True,
        health_check_interval=30,
        max_connections=50,
    )
    return client


async def close_redis(client: aioredis.Redis) -> None:
    """Gracefully close the Redis connection and its pool.

    Args:
        client: The async Redis client to shut down.
    """
    await client.aclose()


async def get_redis(request: Request) -> aioredis.Redis:
    """FastAPI dependency that yields an async Redis client.

    The client is read from ``request.app.state.redis`` and **must** be
    initialised during the application lifespan (e.g. via ``init_redis()``).

    Usage:

        @router.get("/cache")
        async def get_cache(redis: aioredis.Redis = Depends(get_redis)): ...

    Returns:
        An :class:`redis.asyncio.Redis` instance.

    Raises:
        RuntimeError: If the client is not available on ``app.state``.
    """
    client: aioredis.Redis | None = getattr(request.app.state, "redis", None)
    if client is None:
        raise RuntimeError(
            "Redis client not found on app.state. "
            "Ensure init_redis() was called and app.state.redis was set "
            "during the application lifespan."
        )
    return client


async def check_redis_health(client: aioredis.Redis) -> bool:
    """Check whether the Redis server is reachable via PING.

    Args:
        client: An async Redis client.

    Returns:
        ``True`` if PONG was received, ``False`` otherwise.
    """
    try:
        return await client.ping()
    except Exception:
        logger.warning("Redis health check failed.", exc_info=True)
        return False
