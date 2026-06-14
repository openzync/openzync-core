"""Repository for prompt template CRUD and versioning.

System-level templates (``organization_id IS NULL``) serve as defaults.
Organizations can create scoped overrides with monotonically increasing
version numbers.
"""

from __future__ import annotations

import re
from collections import defaultdict
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

        Groups active system defaults by base name (strips ``_v\\d+`` suffix)
        and copies **only the highest-versioned template per group** into the
        org's scope at ``version = 1``.  This means ``extract_facts_v4`` is
        seeded but ``extract_facts_v1``, ``v2``, ``v3`` are not.

        Template names that the org already has are skipped — idempotent.

        Args:
            org_id: UUID of the newly created organisation.

        Returns:
            Number of templates seeded.
        """
        _V_RE = re.compile(r"_v(\d+)$")

        # Fetch all active system defaults.
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id.is_(None),
                PromptTemplate.is_active.is_(True),
            )
        )
        defaults = list(result.scalars().all())

        # Group by base name, keep highest-versioned template per group.
        groups: dict[str, list[tuple[int, PromptTemplate]]] = defaultdict(list)
        for tmpl in defaults:
            m = _V_RE.search(tmpl.template_name)
            if m:
                base = tmpl.template_name[: m.start()]
                groups[base].append((int(m.group(1)), tmpl))
            else:
                # No version suffix — treat the name itself as the base.
                groups[tmpl.template_name].append((0, tmpl))

        latest_templates = [max(entries, key=lambda x: x[0])[1] for entries in groups.values()]

        count = 0
        for tmpl in latest_templates:
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

        # Cross-check: if the org has ANY row for a template name, it's
        # customised — even if a higher-version system default shadows it.
        org_result = await self._db.execute(
            select(PromptTemplate.template_name)
            .where(PromptTemplate.organization_id == org_id)
            .distinct()
        )
        org_names = {row[0] for row in org_result.fetchall()}
        for entry in seen.values():
            if entry["name"] in org_names:
                entry["is_customised"] = True

        # Only return templates the org actually owns (seeded or imported).
        return [v for v in seen.values() if v["is_customised"]]

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

    async def list_system_grouped(self, org_id: UUID) -> list[dict]:
        """List all system-default templates grouped by base name.

        Returns groups annotated with which template names the org already
        has imported.  Each group includes **all** system-default versions
        (not just active ones), so users can see old versions too.

        Args:
            org_id: The organisation UUID for cross-checking imports.

        Returns:
            A list of group dicts::

                [
                    {
                        "base_name": "extract_facts",
                        "templates": [
                            {"name": "extract_facts_v1", "version": 1,
                             "is_active": false, "is_system_default": false,
                             "description": None},
                            ...
                        ],
                        "imported": ["extract_facts_v4"],
                    },
                    ...
                ]
        """
        import re
        from collections import defaultdict

        _V_RE = re.compile(r"_v(\d+)$")

        # Fetch all system-default rows (all versions).
        sys_result = await self._db.execute(
            select(PromptTemplate)
            .where(PromptTemplate.organization_id.is_(None))
            .order_by(PromptTemplate.template_name, PromptTemplate.version.desc())
        )
        system_rows = list(sys_result.scalars().all())

        # Fetch template names the org already has.
        org_result = await self._db.execute(
            select(PromptTemplate.template_name)
            .where(PromptTemplate.organization_id == org_id)
            .distinct()
        )
        org_names = {row[0] for row in org_result.fetchall()}

        # Group by base name.
        groups: dict[str, list[PromptTemplate]] = defaultdict(list)
        for tmpl in system_rows:
            m = _V_RE.search(tmpl.template_name)
            base = tmpl.template_name[: m.start()] if m else tmpl.template_name
            groups[base].append(tmpl)

        result = []
        for base_name in sorted(groups):
            templates = [
                {
                    "name": t.template_name,
                    "version": t.version,
                    "is_active": t.is_active,
                    "is_system_default": t.is_system_default,
                    "description": t.description,
                }
                for t in groups[base_name]
            ]
            # Which template names from this family has the org imported?
            imported = sorted(
                t["name"] for t in templates if t["name"] in org_names
            )
            result.append({
                "base_name": base_name,
                "templates": templates,
                "imported": imported,
            })

        return result

    async def import_system_template(
        self,
        org_id: UUID,
        template_name: str,
    ) -> PromptTemplate:
        """Import a system-default prompt template into the org's scope.

        Creates an org copy at ``version = 1`` with the template text
        from the active system default.  Idempotent — if the org already
        has this template name, it's a no-op.

        Args:
            org_id: The organisation UUID.
            template_name: The template name to import (e.g.
                ``"extract_facts_v2"``).

        Returns:
            The newly created org ``PromptTemplate``.

        Raises:
            ValueError: If no active system default exists for the
                given template name.
        """
        # Find the active system default for this template name.
        result = await self._db.execute(
            select(PromptTemplate).where(
                PromptTemplate.organization_id.is_(None),
                PromptTemplate.template_name == template_name,
                PromptTemplate.is_active.is_(True),
            )
        )
        system_tmpl = result.scalar_one_or_none()
        if system_tmpl is None:
            raise ValueError(
                f"No active system default found for template "
                f"{template_name!r}",
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

        org_tmpl = PromptTemplate(
            organization_id=org_id,
            template_name=template_name,
            template_text=system_tmpl.template_text,
            version=1,
            description=system_tmpl.description,
            is_active=True,
        )
        self._db.add(org_tmpl)
        await self._db.flush()
        await self._db.refresh(org_tmpl)
        return org_tmpl

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
