"""Admin bootstrap and management endpoints — HTTP adapter layer only.

The ``POST /admin/organizations`` endpoint is a first-use bootstrap flow.
It creates an organization and its OpenBao namespace — no API key is
generated and no authentication is required (there is no admin user to
authenticate as yet).  First-use authentication flows through
``POST /v1/auth/signup``.

In production, this endpoint should be disabled or gated behind a
separate mechanism (environment variable, deployment-time key, etc.).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import audit_action
from dependencies.db import get_db
from repositories.organization_repository import OrganizationRepository
from schemas.organizations import CreateOrgRequest, CreateOrgResponse
from services.organization_service import OrganizationService

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.post(
    "/organizations",
    status_code=201,
    response_model=CreateOrgResponse,
)
@audit_action("organization.create", "organization", "Organization created")
async def create_organization(
    payload: CreateOrgRequest,
    db: AsyncSession = Depends(get_db),
) -> CreateOrgResponse:
    """Create a new organization (and its OpenBao namespace).

    This is a bootstrap endpoint for initial setup. It performs a single
    atomic transaction that creates a new ``Organization`` record, then
    bootstraps the org's OpenBao namespace with default config.

    No API key is generated here — first-use authentication flows through
    ``POST /v1/auth/signup``.

    **Security notes:**
    - This endpoint has **no authentication** — it is designed for the
      first-use flow before any users or API keys exist.
    - In production, disable this endpoint or gate it behind a
      deployment-time secret environment variable.

    Args:
        payload: Organization name and optional plan.
        db: Async database session from dependency injection.

    Returns:
        A ``CreateOrgResponse`` with the org ID and name.
    """
    service = OrganizationService(repo=OrganizationRepository(db=db))
    return await service.create_organization(payload)
