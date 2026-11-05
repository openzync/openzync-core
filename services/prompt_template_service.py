"""Prompt template service — org-scoped overrides plus cache invalidation.

DB write first, Redis delete second (best-effort, log-and-continue) —
routers never touch Redis or repositories directly.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.sorting import SortSpec
from models.prompt_template import PromptTemplate
from repositories.prompt_template_repository import PromptTemplateRepository

logger = logging.getLogger(__name__)


class PromptTemplateService:
    """Business logic for org-scoped prompt template management.

    Thin orchestration over :class:`PromptTemplateRepository`: reads
    delegate directly, mutations drop the template cache entry
    afterwards. Routers depend on this service via
    ``get_prompt_template_service``, never on the repository.

    Args:
        db: The request-scoped async session (RLS-tenanted).
        redis: Async Redis client, or ``None`` when unavailable.
    """

    def __init__(self, db: AsyncSession, redis: Any | None = None) -> None:
        self._db = db
        self._redis = redis
        self._repo = PromptTemplateRepository(db)

    async def set_override(
        self,
        org_id: UUID,
        name: str,
        text: str,
        desc: str | None = None,
        template_type: str | None = None,
    ) -> PromptTemplate:
        """Create a new org-scoped template version and drop its cache entry.

        Args:
            org_id: The organization UUID.
            name: Template name identifier.
            text: Jinja2 template body.
            desc: Optional description.
            template_type: Optional type classifier.

        Returns:
            The newly created ``PromptTemplate``.
        """
        template = await self._repo.set_for_org(
            org_id=org_id,
            name=name,
            text=text,
            desc=desc,
            template_type=template_type,
        )
        await self._drop_cache(org_id, name)
        return template

    async def list_names(
        self, org_id: UUID, sort: SortSpec | None = None
    ) -> list[dict[str, Any]]:
        """List template names with override status for an org.

        Args:
            org_id: The organization UUID.
            sort: Validated sort spec (forwarded opaque to the repository).

        Returns:
            One dict per template name with version/override metadata.
        """
        return await self._repo.list_names(org_id, sort=sort)

    async def list_system_grouped(self, org_id: UUID) -> list[dict[str, Any]]:
        """List system-default templates grouped by base name.

        Args:
            org_id: The organization UUID.

        Returns:
            Groups annotated with the org's imported template names.
        """
        return await self._repo.list_system_grouped(org_id=org_id)

    async def get_active(
        self, org_id: UUID, name: str
    ) -> PromptTemplate | None:
        """Get the active org-scoped template, or ``None``.

        Args:
            org_id: The organization UUID.
            name: Template name identifier.

        Returns:
            The active ``PromptTemplate``, or ``None`` if not found.
        """
        return await self._repo.get_active(org_id, name)

    async def list_versions(
        self, org_id: UUID, name: str
    ) -> list[PromptTemplate]:
        """List all versions of a named template, newest first.

        Args:
            org_id: The organization UUID.
            name: Template name identifier.

        Returns:
            Org-scoped versions ordered by version descending.
        """
        return await self._repo.list_versions(org_id, name)

    async def import_system_template(
        self, org_id: UUID, template_name: str
    ) -> PromptTemplate:
        """Import a system-default template as an org copy at version 1.

        Args:
            org_id: The organization UUID.
            template_name: System template name to import.

        Returns:
            The imported ``PromptTemplate``.

        Raises:
            ValueError: If already imported or no active default exists.
        """
        template = await self._repo.import_system_template(
            org_id=org_id,
            template_name=template_name,
        )
        await self._drop_cache(org_id, template_name)
        return template

    async def set_as_type_default(
        self, org_id: UUID, name: str
    ) -> PromptTemplate:
        """Mark a template as the active default for its type.

        Args:
            org_id: The organization UUID.
            name: Template name identifier.

        Returns:
            The updated ``PromptTemplate``.

        Raises:
            ValueError: If the template does not exist or has no type.
        """
        template = await self._repo.set_as_type_default(
            org_id=org_id, name=name
        )
        await self._drop_cache(org_id, name)
        return template

    async def rollback(
        self, org_id: UUID, name: str, version: int
    ) -> PromptTemplate:
        """Rollback to a previous version (copied into a new version).

        Args:
            org_id: The organization UUID.
            name: Template name identifier.
            version: Version number to copy from.

        Returns:
            The newly created ``PromptTemplate``.

        Raises:
            ValueError: If the target version does not exist in org scope.
        """
        template = await self._repo.rollback(
            org_id=org_id, name=name, version=version
        )
        await self._drop_cache(org_id, name)
        return template

    async def delete_for_org(self, org_id: UUID, name: str) -> None:
        """Delete all org-specific versions of a template.

        Args:
            org_id: The organization UUID.
            name: Template name identifier.
        """
        await self._repo.delete_for_org(org_id=org_id, name=name)
        await self._drop_cache(org_id, name)

    async def _drop_cache(self, org_id: UUID, name: str) -> None:
        """Best-effort cache invalidation for one template (never raises).

        Args:
            org_id: The organization UUID.
            name: Template name identifier.
        """
        if self._redis is None:
            return
        try:
            await self._redis.delete(f"prompt_template:{org_id}:{name}")
        except Exception:
            logger.exception(
                "prompt_template.cache_invalidate_failed",
                extra={"org_id": str(org_id), "name": name},
            )
