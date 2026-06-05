"""Admin bootstrap and management endpoints.

The ``POST /admin/organizations`` endpoint is a first-use bootstrap flow.
It creates an organization and returns an API key — no authentication
required (there is no admin user to authenticate as yet).

In production, this endpoint should be disabled or gated behind a
separate mechanism (environment variable, deployment-time key, etc.).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db

router = APIRouter(prefix="/admin", tags=["Admin"])

# ═══════════════════════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════════════════════


class CreateOrgRequest(BaseModel):
    """Request body for ``POST /admin/organizations``.

    Attributes:
        name: Human-readable organization name.
        plan: Billing plan. One of ``free``, ``pro``, ``enterprise``.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable organization name.",
        examples=["Acme Corp"],
    )
    plan: str = Field(
        default="free",
        pattern=r"^(free|pro|enterprise)$",
        description="Billing plan for the organization.",
        examples=["free", "pro", "enterprise"],
    )


class CreateOrgResponse(BaseModel):
    """Response body for ``POST /admin/organizations``.

    Attributes:
        organization_id: UUID of the newly created organization.
        organization_name: Name of the organization.
        api_key: Full API key string (shown once — not persisted).
        api_key_prefix: The prefix identifying the key type.
        api_key_name: Human-readable label for the key.
        message: Warning to save the key.
    """

    organization_id: UUID = Field(..., description="UUID of the newly created organization.")
    organization_name: str = Field(..., description="Name of the organization.")
    api_key: str = Field(..., description="Full API key string (shown once — not persisted).")
    api_key_prefix: str = Field(
        default="mg_live_",
        description="Prefix identifying the key type.",
    )
    api_key_name: str = Field(
        default="default",
        description="Human-readable label for the key.",
    )
    message: str = Field(
        default="Save this API key — it will not be shown again.",
        description="Warning that the key will not be retrievable later.",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Bootstrap endpoint
# ═══════════════════════════════════════════════════════════════════════════════


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
        A :class:`CreateOrgResponse` with the org details and raw API key.
    """
    import structlog
    from utils.crypto import compute_lookup_hash, generate_api_key, hash_api_key

    logger = structlog.get_logger()

    # ── 1. Create organization via ORM ──────────────────────────────────
    from models.organization import Organization

    org = Organization(name=payload.name, plan=payload.plan)
    db.add(org)
    await db.flush()
    await db.refresh(org)

    # ── 2. Generate API key ─────────────────────────────────────────────
    raw_key = generate_api_key(prefix="mg_live_")
    key_hash, salt = hash_api_key(raw_key)
    lookup_hash = compute_lookup_hash(raw_key)

    # ║ NOTE: The ApiKey ORM model does not currently map the ``salt``
    # ║ and ``lookup_hash`` columns, although they exist in the migration.
    # ║ We use a raw INSERT to set all columns atomically.
    await db.execute(
        text("""
            INSERT INTO api_keys (
                organization_id, key_hash, lookup_hash, salt,
                prefix, name, scopes, is_revoked
            ) VALUES (
                :org_id, :key_hash, :lookup_hash, :salt,
                :prefix, :name, :scopes, :is_revoked
            )
        """),
        {
            "org_id": org.id,
            "key_hash": key_hash,
            "lookup_hash": lookup_hash,
            "salt": salt,
            "prefix": "mg_live_",
            "name": "default",
            "scopes": ["read", "write", "admin"],
            "is_revoked": False,
        },
    )

    # ── 3. Commit everything atomically ─────────────────────────────────
    await db.commit()

    logger.info(
        "organization.created",
        org_id=str(org.id),
        org_name=org.name,
        org_plan=payload.plan,
    )

    return CreateOrgResponse(
        organization_id=org.id,
        organization_name=org.name,
        api_key=raw_key,
        api_key_name="default",
    )
