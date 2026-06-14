"""Organization service — bootstrap and management business logic.

This is intentionally kept separate from the main domain services because
the bootstrap flow (creating the first organization + API key) has no
authentication requirement and runs before any user exists.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from models.api_key import ApiKey
from models.organization import Organization
from schemas.organizations import CreateOrgRequest, CreateOrgResponse
from utils.crypto import compute_lookup_hash, generate_api_key, hash_api_key

logger = structlog.get_logger(__name__)


class OrganizationService:
    """Business logic for organization bootstrap and management.

    Args:
        db: An async SQLAlchemy session.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create_organization(
        self, payload: CreateOrgRequest
    ) -> CreateOrgResponse:
        """Create a new organization and generate an admin API key.

        Performs a single atomic transaction:
        1. Creates an ``Organization`` record.
        2. Generates a ``mg_live_`` API key with ``read``, ``write``, and
           ``admin`` scopes.
        3. Returns the raw API key — this is the **only** time it is visible.

        Args:
            payload: Organization name and optional plan.

        Returns:
            A ``CreateOrgResponse`` with the org details and raw API key.
        """
        # ── 1. Create organization ───────────────────────────────────────
        org = Organization(name=payload.name, plan=payload.plan)
        self._db.add(org)
        await self._db.flush()
        await self._db.refresh(org)

        # ── 2. Generate API key ──────────────────────────────────────────
        raw_key = generate_api_key(prefix="mg_live_")
        key_hash, salt = hash_api_key(raw_key)
        lookup_hash = compute_lookup_hash(raw_key)

        api_key = ApiKey(
            organization_id=org.id,
            key_hash=key_hash,
            lookup_hash=lookup_hash,
            salt=salt,
            prefix="mg_live_",
            name="default",
            scopes=["read", "write", "admin"],
            is_revoked=False,
        )
        self._db.add(api_key)
        await self._db.flush()

        # ── 3. Seed default prompt templates for the new org ─────────────
        from repositories.prompt_template_repository import PromptTemplateRepository

        seeded = await PromptTemplateRepository(self._db).seed_default_prompts(org.id)
        if seeded:
            logger.info(
                "organization.prompts_seeded",
                org_id=str(org.id),
                count=seeded,
            )

        # ── 4. Commit everything atomically ──────────────────────────────
        await self._db.commit()

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
