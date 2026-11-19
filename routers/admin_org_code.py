"""Admin endpoints for self-registration toggle display.

``join_enabled`` is the admin toggle that pauses self-registration for
the platform org.  The legacy ``org_code`` column is retained in the
schema but join codes are no longer issued or rotated.

All endpoints require the org ``admin`` role (JWT only).  The router is a
thin adapter: it extracts the org ID from the admin dependency, delegates
to :class:`OrganizationService`, and maps the result to a schema.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 (runtime import — FastAPI resolves annotation names)
)

from core.audit import audit_action
from dependencies.auth import require_permission
from dependencies.db import get_db
from repositories.organization_repository import OrganizationRepository
from schemas.auth import OrgCodeResponse, UpdateOrgJoinRequest
from services.organization_service import OrganizationService

router = APIRouter(
    prefix="/admin/org/org-code",
    tags=["Admin - Organization"],
)


def _get_org_service(
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> OrganizationService:
    """Dependency factory for the organization service."""
    return OrganizationService(repo=OrganizationRepository(db))


@router.get(
    "",
    response_model=OrgCodeResponse,
    summary="Get the organization's join code and registration state",
    description=(
        "Returns the current join code and whether self-registration "
        "is enabled for the authenticated organization.  Requires the org "
        "admin role (JWT)."
    ),
)
async def get_org_code(
    org_id: str = Depends(require_permission("configuration:read")),  # noqa: B008
    service: OrganizationService = Depends(_get_org_service),  # noqa: B008
) -> OrgCodeResponse:
    """Return the organization's current join code and registration state.

    Args:
        org_id: Authenticated organization ID (admin-gated).
        service: Organization service (injected).

    Returns:
        The current registration toggle and ``join_enabled`` flag.

    Raises:
        NotFoundError: If the organization no longer exists (→ 404 via the
            global exception handler).
    """
    info = await service.get_org_code(UUID(org_id))
    return OrgCodeResponse(
        org_code=info.org_code,
        join_enabled=info.join_enabled,
    )


@router.patch(
    "",
    response_model=OrgCodeResponse,
    summary="Toggle self-registration",
    description=(
        "Enables or disables joining the platform org for the "
        "authenticated organization.  When disabled, join attempts with a "
        "valid registration toggle return 403.  Requires the org admin role (JWT)."
    ),
)
@audit_action(
    "org.code.join_enabled",
    "organization",
    "Org-code registration toggled",
)
async def set_join_enabled(
    payload: UpdateOrgJoinRequest,
    org_id: str = Depends(require_permission("configuration:write")),  # noqa: B008
    service: OrganizationService = Depends(_get_org_service),  # noqa: B008
) -> OrgCodeResponse:
    """Toggle whether the organization accepts new members via self-registration.

    Args:
        payload: The new ``join_enabled`` value.
        org_id: Authenticated organization ID (admin-gated).
        service: Organization service (injected).

    Returns:
        The fresh registration toggle and ``join_enabled`` flag.

    Raises:
        NotFoundError: If the organization no longer exists (→ 404 via the
            global exception handler).
    """
    info = await service.set_join_enabled(UUID(org_id), payload.join_enabled)
    return OrgCodeResponse(
        org_code=info.org_code,
        join_enabled=info.join_enabled,
    )



