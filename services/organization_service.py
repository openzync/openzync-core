"""Organization service — bootstrap and management business logic.

This is intentionally kept separate from the main domain services because
the bootstrap flow (creating the first organization + API key) has no
authentication requirement and runs before any user exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from core.openbao import OpenBaoClient
from models.api_key import ApiKey
from models.organization import Organization
from models.project import Project
from repositories.organization_repository import OrganizationRepository
from schemas.organizations import CreateOrgRequest, CreateOrgResponse
from utils.crypto import compute_lookup_hash, generate_api_key, hash_api_key

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

    async def create_organization(
        self, payload: CreateOrgRequest
    ) -> CreateOrgResponse:
        """Create a new organization with a default project and admin API key.

        Performs a single atomic transaction:
        1. Creates an ``Organization`` record.
        2. Creates a default project scoped to the organization.
        3. Generates a ``oz_live_`` API key scoped to the default project.
        4. Seeds default prompt templates.
        5. Returns the raw API key — this is the **only** time it is visible.

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

        # ── 2. Create default project ────────────────────────────────────
        project = Project(
            organization_id=org.id,
            name=f"{payload.name} - Default",
        )
        self._db.add(project)
        await self._db.flush()
        await self._db.refresh(project)

        # ── 3. Generate API key scoped to the default project ────────────
        raw_key = generate_api_key(prefix="oz_live_")
        key_hash, salt = hash_api_key(raw_key)
        lookup_hash = compute_lookup_hash(raw_key)

        api_key = ApiKey(
            organization_id=org.id,
            project_id=project.id,
            key_hash=key_hash,
            lookup_hash=lookup_hash,
            salt=salt,
            prefix="oz_live_",
            name="default",
            scopes=["read", "write", "admin"],
            is_revoked=False,
        )
        self._db.add(api_key)
        await self._db.flush()

        # ── 4. Seed default prompt templates for the new org ─────────────
        from repositories.prompt_template_repository import PromptTemplateRepository

        seeded = await PromptTemplateRepository(self._db).seed_default_prompts(org.id)
        if seeded:
            logger.info(
                "organization.prompts_seeded",
                org_id=str(org.id),
                count=seeded,
            )

        # ── 5. Commit everything atomically ──────────────────────────────
        await self._db.commit()

        # ── 6. Bootstrap OpenBao namespace + default config ──────────────
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
            project_id=str(project.id),
            org_plan=payload.plan,
        )

        return CreateOrgResponse(
            organization_id=org.id,
            organization_name=org.name,
            api_key=raw_key,
            api_key_name="default",
        )

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
