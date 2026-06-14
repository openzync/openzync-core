"""Repository for prompt template CRUD and versioning.

System-level templates (``organization_id IS NULL``) serve as defaults.
Organizations can create scoped overrides with monotonically increasing
version numbers.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.prompt_template import PromptTemplate


class PromptTemplateRepository:
    """All database access for prompt templates."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_active(
        self,
        org_id: UUID,
        template_name: str,
    ) -> PromptTemplate | None:
        """Get the active template for an org, falling back to system default.

        The lookup order is:
        1. Org-specific active template (``organization_id == org_id``).
        2. System default active template (``organization_id IS NULL``).

        Returns ``None`` if neither exists.
        """
        # (1) Org-specific active template.
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == template_name,
                PromptTemplate.is_active.is_(True),
            )
        )
        template = result.scalar_one_or_none()
        if template is not None:
            return template

        # (2) Fall back to system default.
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id.is_(None),
                PromptTemplate.template_name == template_name,
                PromptTemplate.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def set_for_org(
        self,
        org_id: UUID,
        name: str,
        text: str,
        desc: str | None = None,
    ) -> PromptTemplate:
        """Create a new version for the org, deactivating prior active ones.

        1. Deactivates all currently active templates for this (org, name).
        2. Creates a new version with ``version = max(existing) + 1``.
        3. The new row is marked active.

        Returns the newly created ``PromptTemplate``.
        """
        # Deactivate all currently active templates for this (org, name).
        await self._db.execute(
            update(PromptTemplate)
            .where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.is_active.is_(True),
            )
            .values(is_active=False)
        )

        # Determine the next version number.
        result = await self._db.execute(
            select(func.max(PromptTemplate.version)).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
            )
        )
        max_version: int = result.scalar() or 0

        template = PromptTemplate(
            organization_id=org_id,
            template_name=name,
            template_text=text,
            version=max_version + 1,
            description=desc,
            is_active=True,
        )
        self._db.add(template)
        await self._db.flush()
        await self._db.refresh(template)
        return template

    async def rollback(
        self,
        org_id: UUID,
        name: str,
        version: int,
    ) -> PromptTemplate:
        """Create a new version whose ``template_text`` matches a previous version.

        Looks up the target version first in the org's scope, then falls back
        to the system default.  Raises ``ValueError`` if the version does not
        exist in either scope.
        """
        # Look up the target version — org-specific first, then system default.
        target = await self._get_version_in_scope(
            org_id, name, version,
        )
        if target is None:
            raise ValueError(
                f"Version {version} of template {name!r} not found "
                f"in org {org_id} or system defaults",
            )

        # Deactivate currently active org-specific templates.
        await self._db.execute(
            update(PromptTemplate)
            .where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.is_active.is_(True),
            )
            .values(is_active=False)
        )

        # Determine the next version number.
        result = await self._db.execute(
            select(func.max(PromptTemplate.version)).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
            )
        )
        max_version: int = result.scalar() or 0

        new_template = PromptTemplate(
            organization_id=org_id,
            template_name=name,
            template_text=target.template_text,
            version=max_version + 1,
            description=target.description,
            is_active=True,
        )
        self._db.add(new_template)
        await self._db.flush()
        await self._db.refresh(new_template)
        return new_template

    async def delete_for_org(
        self,
        org_id: UUID,
        name: str,
    ) -> None:
        """Delete all org-specific versions of a template.

        After deletion the organization falls back to the system default
        (``organization_id IS NULL``) for this template.
        """
        await self._db.execute(
            delete(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
            )
        )
        await self._db.flush()

    async def seed_default_prompts(self, org_id: UUID) -> int:
        """Seed the latest active system-default prompts for a new organisation.

        Copies each active system default (``organization_id IS NULL``,
        ``is_active = True``) into the org's scope at ``version = 1``.
        Template names that the org already has are skipped — this method is
        safe for re-runs (idempotent).

        Args:
            org_id: UUID of the newly created organisation.

        Returns:
            The number of templates seeded.
        """
        # Fetch all active system defaults.
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id.is_(None),
                PromptTemplate.is_active.is_(True),
            )
        )
        defaults = list(result.scalars().all())

        count = 0
        for tmpl in defaults:
            # Skip if the org already has a version for this template name.
            existing = await self._db.execute(
                select(PromptTemplate).where(
                    PromptTemplate.organization_id == org_id,
                    PromptTemplate.template_name == tmpl.template_name,
                )
            )
            if existing.scalar_one_or_none() is not None:
                continue

            org_tmpl = PromptTemplate(
                organization_id=org_id,
                template_name=tmpl.template_name,
                template_text=tmpl.template_text,
                version=1,
                description=tmpl.description,
                is_active=True,
            )
            self._db.add(org_tmpl)
            count += 1

        if count:
            await self._db.flush()

        return count

    async def promote_to_system_default(
        self,
        caller_org_id: UUID,
        name: str,
        target_version: int,
    ) -> PromptTemplate:
        """Promote a version to be the active system default.

        1. Look up the target version (caller org scope first,
           then system scope).
        2. Deactivate the current system default.
        3. Create a new system default entry with the target text.

        Args:
            caller_org_id: Org UUID of the caller (used for audit / lookup).
            name: Template name to promote.
            target_version: Version number to promote.

        Returns:
            The newly created system-default ``PromptTemplate``.

        Raises:
            ValueError: If the target version does not exist.
        """
        # ── 1. Find the target version ───────────────────────────────────
        target = await self._get_version_in_scope(caller_org_id, name, target_version)
        if target is None:
            raise ValueError(
                f"Version {target_version} of template {name!r} not found "
                f"in org {caller_org_id} or system defaults",
            )

        # ── 2. Deactivate current system default ─────────────────────────
        await self._db.execute(
            update(PromptTemplate)
            .where(
                PromptTemplate.organization_id.is_(None),
                PromptTemplate.template_name == name,
                PromptTemplate.is_active.is_(True),
            )
            .values(is_active=False)
        )

        # ── 3. Determine the next system version number ──────────────────
        result = await self._db.execute(
            select(func.max(PromptTemplate.version)).where(
                PromptTemplate.organization_id.is_(None),
                PromptTemplate.template_name == name,
            )
        )
        max_version: int = result.scalar() or 0

        # ── 4. Create new system default ─────────────────────────────────
        new_default = PromptTemplate(
            organization_id=None,
            template_name=name,
            template_text=target.template_text,
            version=max_version + 1,
            description=(
                f"[Promoted from v{target_version} by "
                f"org {caller_org_id}]"
            ),
            is_active=True,
        )
        self._db.add(new_default)
        await self._db.flush()
        await self._db.refresh(new_default)

        return new_default

    async def list_names(
        self,
        org_id: UUID,
    ) -> list[dict]:
        """List all distinct template names with metadata for an org.

        Returns one entry per template name.  For each name the entry includes
        ``is_customised`` — whether the org has created any version-specific
        override for that template.
        """
        result = await self._db.execute(
            select(
                PromptTemplate.template_name,
                PromptTemplate.version,
                PromptTemplate.description,
                PromptTemplate.updated_at,
                PromptTemplate.organization_id,
            )
            .where(
                (PromptTemplate.organization_id == org_id)
                | (PromptTemplate.organization_id.is_(None)),
            )
            .order_by(PromptTemplate.template_name, PromptTemplate.version.desc())
        )
        rows = result.all()

        # Collapse into one entry per name, keeping the highest-version row.
        seen: dict[str, dict] = {}
        for row in rows:
            name = row.template_name
            if name not in seen:
                seen[name] = {
                    "name": name,
                    "version": row.version,
                    "is_customised": row.organization_id == org_id,
                    "description": row.description,
                    "updated_at": row.updated_at,
                }

        return list(seen.values())

    async def list_versions(
        self,
        org_id: UUID,
        name: str,
    ) -> list[PromptTemplate]:
        """List all versions of a named template visible to an org.

        Returns both system default versions and org-specific versions,
        ordered by version descending (newest first).
        """
        result = await self._db.execute(
            select(PromptTemplate)
            .where(
                (PromptTemplate.organization_id == org_id)
                | (PromptTemplate.organization_id.is_(None)),
                PromptTemplate.template_name == name,
            )
            .order_by(PromptTemplate.version.desc())
        )
        return list(result.scalars().all())

    async def get_version(
        self,
        org_id: UUID,
        name: str,
        version: int,
    ) -> PromptTemplate | None:
        """Get a specific version of a template visible to an org.

        Searches both org-specific and system default scopes.
        """
        result = await self._db.execute(
            select(PromptTemplate).where(
                (PromptTemplate.organization_id == org_id)
                | (PromptTemplate.organization_id.is_(None)),
                PromptTemplate.template_name == name,
                PromptTemplate.version == version,
            )
        )
        return result.scalar_one_or_none()

    async def get_system_default(
        self,
        name: str,
    ) -> PromptTemplate | None:
        """Get the active system default (``organization_id IS NULL``) template."""
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id.is_(None),
                PromptTemplate.template_name == name,
                PromptTemplate.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_version_in_scope(
        self,
        org_id: UUID,
        name: str,
        version: int,
    ) -> PromptTemplate | None:
        """Look up a version in org scope, then system scope."""
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.version == version,
            )
        )
        template = result.scalar_one_or_none()
        if template is not None:
            return template

        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id.is_(None),
                PromptTemplate.template_name == name,
                PromptTemplate.version == version,
            )
        )
        return result.scalar_one_or_none()
