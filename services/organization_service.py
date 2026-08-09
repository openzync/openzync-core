"""Organization service — bootstrap and management business logic.

This is intentionally kept separate from the main domain services because
the bootstrap flow (creating the first organization) has no authentication
requirement and runs before any user exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from core.openbao import OpenBaoClient
from core.org_codes import generate_org_code
from models.organization import Organization
from repositories.organization_repository import OrganizationRepository
from schemas.organizations import CreateOrgRequest, CreateOrgResponse

logger = structlog.get_logger(__name__)


class OrganizationService:
    """Business logic for organization bootstrap and management.

    Args:
        repo: Repository for organization DB access.
        bao_client: Optional OpenBao client for per-org namespace + config
            seeding at org creation time.
    """

    def __init__(
        self,
        repo: OrganizationRepository,
        bao_client: OpenBaoClient | None = None,
    ) -> None:
        self._repo = repo
        # Session for multi-table transactions in create_organization.
        # Exposed via OrganizationRepository.session as a public property.
        self._db: AsyncSession = repo.session
        self._bao_client = bao_client

    async def create_organization(self, payload: CreateOrgRequest) -> CreateOrgResponse:
        """Create a new organization.

        Performs a single atomic transaction:
        1. Creates an ``Organization`` record.
        2. Seeds default prompt templates.
        3. Bootstraps the OpenBao namespace + default config after commit
           (non-fatal on failure — reconcilable later).

        No default project and no API key are created; first-use
        authentication flows through ``POST /v1/auth/signup``.

        Args:
            payload: Organization name and optional plan.

        Returns:
            A ``CreateOrgResponse`` with the org ID and name.
        """
        # ── 1. Create organization ───────────────────────────────────────
        org = Organization(
            name=payload.name,
            plan=payload.plan,
            org_code=generate_org_code(),
        )
        self._db.add(org)
        await self._db.flush()
        await self._db.refresh(org)

        # ── 2. Seed default prompt templates for the new org ─────────────
        from repositories.prompt_template_repository import PromptTemplateRepository

        seeded = await PromptTemplateRepository(self._db).seed_default_prompts(org.id)
        if seeded:
            logger.info(
                "organization.prompts_seeded",
                org_id=str(org.id),
                count=seeded,
            )

        # ── 3. Commit everything atomically ──────────────────────────────
        await self._db.commit()

        # ── 4. Bootstrap OpenBao namespace + default config ──────────────
        if self._bao_client is not None:
            try:
                await self._bao_client.create_org_namespace(org.id)
                defaults = self._load_org_defaults()
                if defaults:
                    await self._bao_client.write_org_config(org.id, defaults)
                logger.info(
                    "organization.openbao_bootstrapped",
                    org_id=str(org.id),
                    defaults_count=len(defaults),
                )
            except Exception:
                # ⚠️ Non-fatal: if OpenBao is down during org creation we
                #    still return success — the namespace can be bootstrapped
                #    later by an admin or a background reconciliation job.
                logger.exception(
                    "organization.openbao_bootstrap_failed",
                    org_id=str(org.id),
                )

        logger.info(
            "organization.created",
            org_id=str(org.id),
            org_name=org.name,
            org_plan=payload.plan,
        )

        return CreateOrgResponse(
            organization_id=org.id,
            organization_name=org.name,
        )

    # ── Org join code (admin management) ──────────────────────────────────────

    async def get_org_code(self, org_id: UUID) -> str:
        """Return the organization's current join code.

        Args:
            org_id: The organization UUID.

        Returns:
            The current org code.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        org = await self._repo.get_by_id(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        return org.org_code

    async def regenerate_org_code(self, org_id: UUID) -> str:
        """Generate and persist a new join code (rotating the old one).

        Immediately invalidates any previously distributed code.

        Args:
            org_id: The organization UUID.

        Returns:
            The new org code.

        Raises:
            NotFoundError: If no organization with this UUID exists.
        """
        new_code = generate_org_code()
        org = await self._repo.set_org_code(org_id, new_code)
        return org.org_code

    def _load_org_defaults(self) -> dict[str, Any]:
        """Load default per-org config values from ``config/defaults/org_config.yaml``.

        Returns:
            A flat dict of key/value pairs, or ``{}`` if the file is missing
            or unreadable.
        """
        path = Path(__file__).parent.parent / "config" / "defaults" / "org_config.yaml"
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("org_config.defaults_file_not_found", path=str(path))
            return {}
        except yaml.YAMLError as e:
            logger.warning("org_config.defaults_file_invalid", path=str(path), error=str(e))
            return {}
