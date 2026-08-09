"""Admin endpoints for organization join-code management.

The org code is the join token a new member presents at
``POST /v1/auth/join`` to join an existing organization.  Stored plaintext
by explicit product decision — treat it as sensitive, and regenerate
(rotate) it whenever it leaks.

Both endpoints require the org ``admin`` role (JWT only).  The router is a
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
from dependencies.auth import require_org_admin
from dependencies.db import get_db
from repositories.organization_repository import OrganizationRepository
from schemas.auth import OrgCodeResponse
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
    summary="Get the organization's join code",
    description=(
        "Returns the current join code for the authenticated organization. "
        "Requires the org admin role (JWT)."
    ),
)
async def get_org_code(
    org_id: str = Depends(require_org_admin),  # noqa: B008
    service: OrganizationService = Depends(_get_org_service),  # noqa: B008
) -> OrgCodeResponse:
    """Return the organization's current join code.

    Args:
        org_id: Authenticated organization ID (admin-gated).
        service: Organization service (injected).

    Returns:
        The current org code.

    Raises:
        NotFoundError: If the organization no longer exists (→ 404 via the
            global exception handler).
    """
    code = await service.get_org_code(UUID(org_id))
    return OrgCodeResponse(org_code=code)


@router.post(
    "/regenerate",
    response_model=OrgCodeResponse,
    summary="Rotate the organization's join code",
    description=(
        "Generates a new join code and persists it, immediately "
        "invalidating any previously distributed code.  Requires the org "
        "admin role (JWT)."
    ),
)
@audit_action(
    "org.code.regenerate",
    "organization",
    "Organization join code regenerated",
)
async def regenerate_org_code(
    org_id: str = Depends(require_org_admin),  # noqa: B008
    service: OrganizationService = Depends(_get_org_service),  # noqa: B008
) -> OrgCodeResponse:
    """Generate and persist a new join code (rotating the old one).

    Args:
        org_id: Authenticated organization ID (admin-gated).
        service: Organization service (injected).

    Returns:
        The new org code.

    Raises:
        NotFoundError: If the organization no longer exists (→ 404 via the
            global exception handler).
    """
    code = await service.regenerate_org_code(UUID(org_id))
    return OrgCodeResponse(org_code=code)
