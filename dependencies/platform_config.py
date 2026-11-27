"""FastAPI dependency for per-user platform configuration.

Usage in a router::

    from dependencies.platform_config import get_platform_config
    from schemas.platform_config import PlatformConfigBase

    @router.get("/example")
    async def example(
        platform_config: PlatformConfigBase = Depends(get_platform_config),
    ):
        ...  # use platform_config.llm_model, platform_config.graph_backend, etc.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_user_id
from dependencies.db import get_db


async def get_platform_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(require_user_id),
) -> PlatformConfigBase:
    """FastAPI dependency that yields the stored platform config for the current user.

    The config is fetched from Redis cache (fast path) or the DB
    (``users.config_override``, authoritative slow path).  Every field may
    be ``None`` — there is no env-var fallback.

    Usage::

        @router.get("/my-endpoint")
        async def handler(
            platform_config: PlatformConfigBase = Depends(get_platform_config),
        ):
            model = platform_config.llm_model  # str | None
    """
    # Lazy imports to avoid circulars (schemas + core pull heavy deps).
    from core.platform_config import get_platform_config as _get_platform_config  # noqa: PLC0415
    from schemas.platform_config import PlatformConfigBase  # noqa: PLC0415

    redis = getattr(request.app.state, "redis", None)
    config: PlatformConfigBase = await _get_platform_config(UUID(user_id), redis=redis, db=db)
    return config
