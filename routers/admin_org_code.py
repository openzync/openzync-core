"""Admin endpoints for organization join-code management.

The org code is the join token a new member presents at
``POST /v1/auth/join`` to join an existing organization.  Stored plaintext
by explicit product decision — treat it as sensitive, and regenerate
(rotate) it whenever it leaks.

Both endpoints require the org ``admin`` role (JWT only).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 (runtime import — FastAPI resolves annotation names)
)

from core.audit import audit_action
from core.org_codes import generate_org_code
from dependencies.auth import require_org_admin
from dependencies.db import get_db
from repositories.organization_repository import OrganizationRepository
from schemas.auth import OrgCodeResponse

router = APIRouter(
    prefix="/admin/org/org-code",
    tags=["Admin - Organization"],
)


def _get_org_repo(
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> OrganizationRepository:
    """Dependency factory for the organization repository."""
    return OrganizationRepository(db)


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
    _org_id: str = Depends(require_org_admin),  # noqa: B008
    repo: OrganizationRepository = Depends(_get_org_repo),  # noqa: B008
) -> OrgCodeResponse:
    """Return the organization's current join code.

    Args:
        _org_id: Authenticated organization ID (admin-gated).
        repo: Organization repository (injected).

    Returns:
        The current org code.

    Raises:
        HTTPException: 404 if the organization no longer exists.
    """
    org = await repo.get_by_id(UUID(_org_id))
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return OrgCodeResponse(org_code=org.org_code)


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
    _org_id: str = Depends(require_org_admin),  # noqa: B008
    repo: OrganizationRepository = Depends(_get_org_repo),  # noqa: B008
) -> OrgCodeResponse:
    """Generate and persist a new join code (rotating the old one).

    Args:
        _org_id: Authenticated organization ID (admin-gated).
        repo: Organization repository (injected).

    Returns:
        The new org code.

    Raises:
        HTTPException: 404 if the organization no longer exists.
    """
    new_code = generate_org_code()
    # NotFoundError (org deleted between auth and here) propagates to the
    # global exception handler → 404.
    org = await repo.set_org_code(UUID(_org_id), new_code)
    return OrgCodeResponse(org_code=org.org_code)
