"""Admin REST API for per-organization configuration.

Endpoints allow dashboard users and API keys with ``admin:write`` scope to
read and update UI-exposed settings (LLM, embeddings, graph, behaviour)
that were previously env-var-only.

All endpoints are scoped to the authenticated organization — an admin can
only manage their own org's config.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_org_id, require_scope
from dependencies.db import get_db
from schemas.organization_config import (
    OrgConfigBase,
    OrgConfigResponse,
    UpdateOrgConfigRequest,
)
from services.org_config_service import OrgConfigService

router = APIRouter(
    prefix="/admin/org/config",
    tags=["Admin - Organization Config"],
)

#: Path to the onboarding defaults YAML file (relative to project root).
DEFAULTS_PATH = Path(__file__).parent.parent / "config" / "defaults" / "org_config.yaml"


# ── Dependency factory ────────────────────────────────────────────────────────


def _get_config_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> OrgConfigService:
    """Build a request-scoped OrgConfigService.

    Reads the Redis client and secret store backend from
    ``request.app.state`` (initialised during the application lifespan).
    """
    redis = getattr(request.app.state, "redis", None)
    backend = getattr(request.app.state, "secret_store", None)
    backend_instance = backend.resolve() if backend else None
    return OrgConfigService(db=db, redis=redis, backend=backend_instance)


# ═══════════════════════════════════════════════════════════════════════════════
# GET  /defaults  — Seeded onboarding defaults
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "/defaults",
    response_model=UpdateOrgConfigRequest,
)
async def get_org_config_defaults() -> UpdateOrgConfigRequest:
    """Return seeded onboarding defaults for a new organization.

    These are **not** the stored config — they are starter values for the
    onboarding form.  The user reviews and adjusts them before saving via
    ``PATCH /admin/org/config``.

    No auth required — this endpoint returns only non-sensitive defaults.
    Secrets such as ``openai_api_key`` are returned as empty strings so
    the user must fill them in.
    """
    if not DEFAULTS_PATH.is_file():
        raise HTTPException(status_code=500, detail="Defaults configuration file not found")
    with DEFAULTS_PATH.open() as f:
        data: dict = yaml.safe_load(f)
    return UpdateOrgConfigRequest(**data)


# ═══════════════════════════════════════════════════════════════════════════════
# GET  — Retrieve stored config
# ═══════════════════════════════════════════════════════════════════════════════


@router.get(
    "",
    response_model=OrgConfigResponse,
)
async def get_org_config(
    _org_id: str = Depends(require_org_id),
    service: OrgConfigService = Depends(_get_config_service),
) -> OrgConfigResponse:
    """Get the stored configuration for the current organization.

    Returns only the fields explicitly set in the DB.  Unset fields are
    ``null`` — there is no env-var fallback.
    """
    return await service.get_config_response(UUID(_org_id))


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH — Partial update
# ═══════════════════════════════════════════════════════════════════════════════


@router.patch(
    "",
    response_model=OrgConfigBase,
)
async def update_org_config(
    body: UpdateOrgConfigRequest,
    _org_id: str = Depends(require_scope("admin:write")),
    service: OrgConfigService = Depends(_get_config_service),
) -> OrgConfigBase:
    """Partially update the organization's configuration.

    Only fields explicitly provided in the request body are updated.
    Set a field to ``null`` to remove it from the stored config (it
    will be returned as ``null`` on subsequent reads).

    Requires an API key with ``admin:write`` scope or a JWT dashboard
    session.
    """
    return await service.update_config(UUID(_org_id), body)


# ═══════════════════════════════════════════════════════════════════════════════
# PUT — Full replace
# ═══════════════════════════════════════════════════════════════════════════════


@router.put(
    "",
    response_model=OrgConfigBase,
)
async def replace_org_config(
    body: UpdateOrgConfigRequest,
    _org_id: str = Depends(require_scope("admin:write")),
    service: OrgConfigService = Depends(_get_config_service),
) -> OrgConfigBase:
    """Replace the entire organization configuration.

    Every field is stored as provided.  Fields set to ``null`` are stored
    as ``null``.  Fields not included in the request body are **removed**
    from the stored config.

    Prefer ``PATCH`` for updating individual fields.  ``PUT`` is useful
    for initial setup where you want to set everything at once.

    Requires an API key with ``admin:write`` scope or a JWT dashboard
    session.
    """
    return await service.update_config(UUID(_org_id), body)
