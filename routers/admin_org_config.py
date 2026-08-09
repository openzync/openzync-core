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

from core.audit import audit_action
from core.config import get_settings
from dependencies.auth import require_org_admin, require_scope
from schemas.organization_config import (
    SYSTEM_MANAGED_FALKORDB_FIELDS,
    SYSTEM_MANAGED_SURREALDB_FIELDS,
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


def _get_system_managed_fields() -> list[str]:
    """Return field names that are locked at the system level.

    Checks the global Settings instance.  When a backend's URL is set at
    the system level, all corresponding per-org config fields are blocked.
    """
    settings = get_settings()
    fields: list[str] = []
    if settings.SURREALDB_URL:
        fields.extend(SYSTEM_MANAGED_SURREALDB_FIELDS)
    if settings.FALKORDB_URL:
        fields.extend(SYSTEM_MANAGED_FALKORDB_FIELDS)
    return fields


# ── Dependency factory ────────────────────────────────────────────────────────


def _get_config_service(
    request: Request,
) -> OrgConfigService:
    """Build a request-scoped OrgConfigService.

    Reads the OpenBao client and Redis client from ``request.app.state``
    (initialised during the application lifespan).
    """
    bao_client = getattr(request.app.state, "openbao_client", None)
    if bao_client is None:
        raise HTTPException(
            status_code=503,
            detail="OpenBao client not available — secrets backend not initialised",
        )
    redis = getattr(request.app.state, "redis", None)
    return OrgConfigService(bao_client=bao_client, redis=redis)


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
    _org_id: str = Depends(require_org_admin),
    service: OrgConfigService = Depends(_get_config_service),
) -> OrgConfigResponse:
    """Get the stored configuration for the current organization.

    Returns only the fields explicitly set in the DB.  Unset fields are
    ``null`` — there is no env-var fallback.

    Admin-gated (``require_org_admin``): the response contains unmasked
    secrets, so members and API keys are denied.
    """
    response = await service.get_config_response(UUID(_org_id))
    response.system_managed_fields = _get_system_managed_fields()
    return response


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH — Partial update
# ═══════════════════════════════════════════════════════════════════════════════


@router.patch(
    "",
    response_model=OrgConfigBase,
)
@audit_action("config.update", "config", "Configuration updated")
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
    # ── Reject system-managed fields ─────────────────────────────────
    system_managed = _get_system_managed_fields()
    if system_managed:
        overridden = {
            f for f in system_managed
            if f in body.model_dump(exclude_unset=True)
        }
        if overridden:
            raise HTTPException(
                status_code=422,
                detail=(
                    "These fields are configured at the system level "
                    "and cannot be modified: "
                    f"{', '.join(sorted(overridden))}."
                ),
            )
    return await service.update_config(UUID(_org_id), body)


# ═══════════════════════════════════════════════════════════════════════════════
# PUT — Full replace
# ═══════════════════════════════════════════════════════════════════════════════


@router.put(
    "",
    response_model=OrgConfigBase,
)
@audit_action("config.update", "config", "Configuration updated")
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
    # ── Reject system-managed fields ─────────────────────────────────
    system_managed = _get_system_managed_fields()
    if system_managed:
        overridden = {
            f for f in system_managed
            if f in body.model_dump(exclude_unset=True)
        }
        if overridden:
            raise HTTPException(
                status_code=422,
                detail=(
                    "These fields are configured at the system level "
                    "and cannot be modified: "
                    f"{', '.join(sorted(overridden))}."
                ),
            )
    return await service.update_config(UUID(_org_id), body)
