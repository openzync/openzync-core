"""Platform super-admin bootstrap endpoints — HTTP adapter layer only.

The ``POST /admin/organizations`` endpoint is a platform operator's
org-creation bootstrap flow: it creates an organization and its OpenBao
namespace.  No API key is generated — first-use authentication flows
through ``POST /v1/auth/signup``.

**Security note:** this endpoint is **superadmin-gated** (JWT dashboard
session, platform org, DB-verified ``superadmin`` role) via
``require_superadmin`` — the same gate as every ``/admin/system/*``
endpoint.  The historical unauthenticated bootstrap behavior is gone:
the platform root user (seeded at startup) is the bootstrap identity.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 (runtime import — FastAPI resolves annotation names)
)

from core.audit import audit_action
from dependencies.auth import require_superadmin
from dependencies.db import get_db
from repositories.organization_repository import OrganizationRepository
from schemas.organizations import CreateOrgRequest, CreateOrgResponse
from services.organization_service import OrganizationService

router = APIRouter(prefix="/admin", tags=["Admin"])


def _get_admin_org_service(
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> OrganizationService:
    """Dependency factory for the admin organization service."""
    return OrganizationService(repo=OrganizationRepository(db))


@router.post(
    "/organizations",
    status_code=201,
    response_model=CreateOrgResponse,
)
@audit_action("organization.create", "organization", "Organization created")
async def create_organization(
    payload: CreateOrgRequest,
    _org_id: str = Depends(require_superadmin),  # noqa: B008
    service: OrganizationService = Depends(_get_admin_org_service),  # noqa: B008
) -> CreateOrgResponse:
    """Create a new organization (and its OpenBao namespace).

    This is a superadmin bootstrap endpoint.  It performs a single
    atomic transaction that creates a new ``Organization`` record, then
    bootstraps the org's OpenBao namespace with default config.

    No API key is generated here — first-use authentication flows through
    ``POST /v1/auth/signup``.

    **Security notes:**
    - Requires the platform super-admin role (JWT dashboard session in
      the platform org with a DB-verified ``superadmin`` role).  Members,
      org admins of tenant orgs, and API keys are denied.
    - The org name is validated — the reserved ``SYSTEM`` name is
      rejected.

    Args:
        payload: Organization name and optional plan.
        _org_id: Platform org UUID (superadmin-gated).
        service: Injected organization service.

    Returns:
        A ``CreateOrgResponse`` with the org ID and name.
    """
    return await service.create_organization(payload)
