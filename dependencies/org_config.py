"""FastAPI dependency for per-organization configuration.

Usage in a router::

    from dependencies.org_config import get_org_config
    from schemas.organization_config import OrgConfigBase

    @router.get("/example")
    async def example(
        org_config: OrgConfigBase = Depends(get_org_config),
    ):
        ...  # use org_config.llm_model, org_config.graph_backend, etc.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Request

from dependencies.auth import require_org_id


async def get_org_config(
    request: Request,
    org_id: str = Depends(require_org_id),
) -> "OrgConfigBase":
    """FastAPI dependency that yields the stored org config for the current org.

    The config is fetched from Redis cache (fast path) or OpenBao KV
    (authoritative slow path).  Every field may be ``None`` — there is no
    env-var fallback.

    Usage::

        @router.get("/my-endpoint")
        async def handler(
            org_config: OrgConfigBase = Depends(get_org_config),
        ):
            model = org_config.llm_model  # str | None
    """
    # Lazy import to avoid circular dependency on schemas
    from core.org_config import get_org_config as _get_org_config
    from schemas.organization_config import OrgConfigBase

    bao_client = getattr(request.app.state, "openbao_client", None)
    redis = getattr(request.app.state, "redis", None)
    return await _get_org_config(UUID(org_id), redis=redis, bao_client=bao_client)
