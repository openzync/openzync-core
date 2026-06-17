"""Repository for prompt template CRUD and versioning.

After Option A (https://github.com/TheLinkAI/openzep/issues/XXX):

- System-level rows (``organization_id IS NULL``) no longer exist.
- The source of truth for defaults is ``services/worker/prompts/manifest.yaml``
  plus the ``.jinja2`` files on disk.
- Every organization gets a complete copy of all manifest templates at signup
  via :meth:`seed_default_prompts`.
- Runtime resolution (``get_active``, ``get_active_by_type``) only queries
  org-scoped rows.  There is no fallback to system rows (because there aren't any).
- If an organisation is missing a template (deleted, or added after signup),
  the admin must explicitly import it via the import endpoint.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from uuid import UUID

import sqlalchemy
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.prompt_manifest import load_manifest
from models.prompt_template import PromptTemplate

logger = logging.getLogger(__name__)


class PromptTemplateRepository:
    """All database access for prompt templates."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ═══════════════════════════════════════════════════════════════════════════
    # Read methods
    # ═══════════════════════════════════════════════════════════════════════════

    async def get_active(
        self,
        org_id: UUID,
        template_name: str,
    ) -> PromptTemplate | None:
        """Get the active org-scoped template, or ``None``.

        Args:
            org_id: The organisation UUID.
            template_name: Template name identifier.

        Returns:
            The active ``PromptTemplate``, or ``None`` if not found.
        """
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == template_name,
                PromptTemplate.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def get_active_by_type(
        self,
        org_id: UUID,
        type: str,
    ) -> PromptTemplate | None:
        """Get the active default template for a given type.

        Only org-scoped rows are queried — there are no system-level rows.
        Returns ``None`` if the org has no default for this type.

        Args:
            org_id: The organisation UUID.
            type: The template type (e.g. ``"fact_extraction"``).

        Returns:
            The active type-default ``PromptTemplate``, or ``None``.
        """
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.type == type,
                PromptTemplate.is_default_for_type.is_(True),
                PromptTemplate.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def get_version(
        self,
        org_id: UUID,
        name: str,
        version: int,
    ) -> PromptTemplate | None:
        """Get a specific version of a template (org scope only).

        Args:
            org_id: The organisation UUID.
            name: Template name identifier.
            version: Exact version number to find.

        Returns:
            The matching ``PromptTemplate``, or ``None``.
        """
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.version == version,
            )
        )
        return result.scalar_one_or_none()

    # ═══════════════════════════════════════════════════════════════════════════
    # Write methods — org-scoped only
    # ═══════════════════════════════════════════════════════════════════════════

    async def set_as_type_default(
        self,
        org_id: UUID,
        name: str,
    ) -> PromptTemplate:
        """Mark a template as the active default for its type.

        Sets ``is_default_for_type = True`` for the named template and
        ``is_default_for_type = False`` for all other templates of the
        same type for this organisation.

        Args:
            org_id: The organisation UUID.
            name: The template name to promote.

        Returns:
            The updated ``PromptTemplate``.

        Raises:
            ValueError: If the template does not exist or has no type.
        """
        # Find the template in org scope only.
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.is_active.is_(True),
            )
        )
        template = result.scalar_one_or_none()
        if template is None:
            raise ValueError(
                f"Template {name!r} not found for org {org_id}"
            )
        if template.type is None:
            raise ValueError(f"Template {name!r} has no type assigned")

        # Deactivate current default for this type in the org scope.
        await self._db.execute(
            update(PromptTemplate)
            .where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.type == template.type,
                PromptTemplate.is_default_for_type.is_(True),
            )
            .values(is_default_for_type=False)
        )

        # Activate the target.
        template.is_default_for_type = True
        await self._db.flush()
        await self._db.refresh(template)
        return template

    async def set_for_org(
        self,
        org_id: UUID,
        name: str,
        text: str,
        desc: str | None = None,
        template_type: str | None = None,
    ) -> PromptTemplate:
        """Create a new version for the org, deactivating prior active ones.

        1. Deactivates all currently active templates for this ``(org, name)``.
        2. Creates a new version with ``version = max(existing) + 1``.
        3. The new row is marked active.

        Args:
            org_id: The organisation UUID.
            name: Template name identifier.
            text: Jinja2 template body.
            desc: Optional description.
            template_type: Optional type classifier (e.g. ``"fact_extraction"``).

        Returns:
            The newly created ``PromptTemplate``.
        """
        # Capture whether the old active version was the type default.
        old_result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.is_active.is_(True),
            )
        )
        old_active = old_result.scalar_one_or_none()
        old_was_default = (
            old_active is not None
            and old_active.is_default_for_type
            and old_active.type is not None
        )

        # Deactivate old versions and clear any default flag so the unique
        # partial index (org_id, type) WHERE is_default_for_type = true
        # is not violated when the new version carries the flag.
        await self._db.execute(
            update(PromptTemplate)
            .where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.is_active.is_(True),
            )
            .values(is_active=False, is_default_for_type=False)
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
            type=template_type,
            is_active=True,
            is_default_for_type=old_was_default,
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

        Looks up the target version in the org's scope.  Raises ``ValueError``
        if the version does not exist.

        Args:
            org_id: The organisation UUID.
            name: Template name.
            version: Target version number to rollback to.

        Returns:
            The newly created ``PromptTemplate``.

        Raises:
            ValueError: If the target version is not found in the org scope.
        """
        # Look up the target version in org scope only.
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.version == version,
            )
        )
        target = result.scalar_one_or_none()
        if target is None:
            raise ValueError(
                f"Version {version} of template {name!r} not found "
                f"in org {org_id}",
            )

        # Capture whether the old active version was the type default.
        old_active_result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.is_active.is_(True),
            )
        )
        old_active = old_active_result.scalar_one_or_none()
        old_was_default = (
            old_active is not None
            and old_active.is_default_for_type
            and old_active.type is not None
        )

        # Deactivate currently active org-specific templates and clear default.
        await self._db.execute(
            update(PromptTemplate)
            .where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
                PromptTemplate.is_active.is_(True),
            )
            .values(is_active=False, is_default_for_type=False)
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
            type=target.type,
            is_active=True,
            is_default_for_type=old_was_default,
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

        After deletion the organisation no longer has a copy of this
        template.  The admin can re-import it via the import endpoint.

        Args:
            org_id: The organisation UUID.
            name: Template name to delete.
        """
        await self._db.execute(
            delete(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
            )
        )
        await self._db.flush()

    # ═══════════════════════════════════════════════════════════════════════════
    # Seeding — reads from disk, not system rows
    # ═══════════════════════════════════════════════════════════════════════════

    async def seed_default_prompts(self, org_id: UUID) -> int:
        """Seed all manifest-defined prompt templates into an organisation.

        Reads ``manifest.yaml`` and the corresponding ``.jinja2`` files
        from disk and creates an org-scoped copy at ``version = 1`` for
        each template entry.

        Template names that the org already has are skipped — idempotent.

        Args:
            org_id: UUID of the newly created organisation.

        Returns:
            Number of templates seeded.
        """
        MANIFEST = load_manifest()
        count = 0

        for entry in MANIFEST.templates:
            name = entry["name"]

            # Skip if the org already has this template name.
            existing = await self._db.execute(
                select(PromptTemplate).where(
                    PromptTemplate.organization_id == org_id,
                    PromptTemplate.template_name == name,
                )
            )
            if existing.scalar_one_or_none() is not None:
                continue

            text = MANIFEST.get_template_text(entry["file"])
            org_tmpl = PromptTemplate(
                organization_id=org_id,
                template_name=name,
                template_text=text,
                version=1,
                description=entry.get("description"),
                type=entry.get("type"),
                is_default_for_type=entry.get("is_default_for_type", False),
                is_active=True,
            )
            self._db.add(org_tmpl)
            count += 1

        if count:
            await self._db.flush()

        return count

    async def import_system_template(
        self,
        org_id: UUID,
        template_name: str,
    ) -> PromptTemplate:
        """Import a prompt template from disk manifest into the org's scope.

        Creates an org copy at ``version = 1`` with the template text from
        the manifest.  Idempotent — raises ``ValueError`` if the org already
        has this template name.

        Args:
            org_id: The organisation UUID.
            template_name: The template name to import (e.g. ``"extract_facts_v4"``).

        Returns:
            The newly created org ``PromptTemplate``.

        Raises:
            ValueError: If no manifest entry exists for the given name, or
                if the org already has the template.
        """
        MANIFEST = load_manifest()
        entry = MANIFEST.get_by_name(template_name)
        if entry is None:
            raise ValueError(
                f"No manifest entry found for template {template_name!r}. "
                f"Available names: {list(MANIFEST.by_name.keys())}",
            )

        # Check if org already has it.
        existing = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == template_name,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError(
                f"Template {template_name!r} already imported into "
                f"this organisation",
            )

        text = MANIFEST.get_template_text(entry["file"])
        org_tmpl = PromptTemplate(
            organization_id=org_id,
            template_name=template_name,
            template_text=text,
            version=1,
            description=entry.get("description"),
            type=entry.get("type"),
            is_default_for_type=entry.get("is_default_for_type", False),
            is_active=True,
        )
        self._db.add(org_tmpl)
        await self._db.flush()
        await self._db.refresh(org_tmpl)
        return org_tmpl

    async def list_system_grouped(self, org_id: UUID) -> list[dict]:
        """List all manifest-defined prompt templates grouped by type.

        Reads entirely from disk (manifest + .jinja2 files).  Each group
        is annotated with which template names the organisation has already
        imported into its scope.

        Args:
            org_id: The organisation UUID for cross-checking imports.

        Returns:
            A list of group dicts::

                [
                    {
                        "type": "fact_extraction",
                        "templates": [
                            {
                                "name": "extract_facts_v4",
                                "version": 1,
                                "type": "fact_extraction",
                                "is_active": true,
                                "is_default_for_type": true,
                                "is_system_default": true,
                                "description": "...",
                            },
                            ...
                        ],
                        "imported": ["extract_facts_v4"],
                    },
                    ...
                ]
        """
        MANIFEST = load_manifest()

        # Fetch template names the org already has.
        org_result = await self._db.execute(
            select(PromptTemplate.template_name)
            .where(PromptTemplate.organization_id == org_id)
            .distinct()
        )
        org_names = {row[0] for row in org_result.fetchall()}

        # Group manifest entries by type.
        groups: dict[str, list[dict]] = defaultdict(list)
        for entry in MANIFEST.templates:
            group_key = entry.get("type") or "other"
            groups[group_key].append({
                "name": entry["name"],
                "version": 1,
                "type": entry.get("type"),
                "is_active": True,
                "is_default_for_type": entry.get("is_default_for_type", False),
                "is_system_default": entry.get("is_default_for_type", False),
                "description": entry.get("description"),
            })

        result = []
        for group_key in sorted(groups):
            templates = groups[group_key]
            imported = sorted(
                t["name"] for t in templates if t["name"] in org_names
            )
            result.append({
                "type": group_key,
                "templates": templates,
                "imported": imported,
            })

        return result

    # ═══════════════════════════════════════════════════════════════════════════
    # Listing
    # ═══════════════════════════════════════════════════════════════════════════

    async def list_names(
        self,
        org_id: UUID,
    ) -> list[dict]:
        """List all distinct template names with metadata for an org.

        Returns one entry per template name that the org has a version
        for (seeded or custom).  All entries are ``is_customised = True``
        since the org owns every row.

        Args:
            org_id: The organisation UUID.

        Returns:
            A list of dicts with keys: ``name``, ``version``,
            ``description``, ``type``, ``is_default_for_type``,
            ``updated_at``, ``is_customised``.
        """
        result = await self._db.execute(
            select(
                PromptTemplate.template_name,
                PromptTemplate.version,
                PromptTemplate.description,
                PromptTemplate.type,
                PromptTemplate.is_default_for_type,
                PromptTemplate.updated_at,
            )
            .where(PromptTemplate.organization_id == org_id)
            .order_by(PromptTemplate.template_name, PromptTemplate.version.desc())
        )
        rows = result.all()

        # Collapse into one entry per name (latest version wins).
        seen: dict[str, dict] = {}
        for row in rows:
            name = row.template_name
            if name in seen:
                continue
            seen[name] = {
                "name": name,
                "version": row.version,
                "is_customised": True,  # always True — org owns all rows
                "description": row.description,
                "type": row.type,
                "is_default_for_type": row.is_default_for_type,
                "updated_at": row.updated_at,
            }

        return list(seen.values())

    async def list_versions(
        self,
        org_id: UUID,
        name: str,
    ) -> list[PromptTemplate]:
        """List all versions of a named template for an org.

        Args:
            org_id: The organisation UUID.
            name: Template name.

        Returns:
            All versions ordered by version descending (newest first).
        """
        result = await self._db.execute(
            select(PromptTemplate)
            .where(
                PromptTemplate.organization_id == org_id,
                PromptTemplate.template_name == name,
            )
            .order_by(PromptTemplate.version.desc())
        )
        return list(result.scalars().all())

    # ═══════════════════════════════════════════════════════════════════════════
    # Deprecated / removed
    # ═══════════════════════════════════════════════════════════════════════════

    # ``get_system_default()`` — removed.  System rows no longer exist.
    # ``promote_to_system_default()`` — removed.  Defaults come from disk.
    #   If hot-promotion is needed later, implement a ``promoted_defaults``
    #   table that overlays on top of the manifest at seeding time.
    # ``_get_version_in_scope()`` — removed.  There is only org scope.
