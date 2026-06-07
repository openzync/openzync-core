"""Admin bootstrap and management endpoints — HTTP adapter layer only.

The ``POST /admin/organizations`` endpoint is a first-use bootstrap flow.
It creates an organization and returns an API key — no authentication
required (there is no admin user to authenticate as yet).

In production, this endpoint should be disabled or gated behind a
separate mechanism (environment variable, deployment-time key, etc.).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from schemas.organizations import CreateOrgRequest, CreateOrgResponse
from services.organization_service import OrganizationService

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.post(
    "/organizations",
    status_code=201,
    response_model=CreateOrgResponse,
)
async def create_organization(
    payload: CreateOrgRequest,
    db: AsyncSession = Depends(get_db),
) -> CreateOrgResponse:
    """Create a new organization and generate an admin API key.

    This is a bootstrap endpoint for initial setup. It performs a single
    atomic transaction that:

    1. Creates a new ``Organization`` record.
    2. Generates a ``mg_live_`` API key with ``read``, ``write``, and
       ``admin`` scopes.
    3. Returns the raw API key — this is the **only** time it is visible.

    **Security notes:**
    - This endpoint has **no authentication** — it is designed for the
      first-use flow before any API keys exist.
    - In production, disable this endpoint or gate it behind a
      deployment-time secret environment variable.
    - The raw key is returned exactly once and is **not** persisted.
      Only the salted SHA-256 hash is stored.

    Args:
        payload: Organization name and optional plan.
        db: Async database session from dependency injection.

    Returns:
        A ``CreateOrgResponse`` with the org details and raw API key.
    """
    service = OrganizationService(db=db)
    return await service.create_organization(payload)
