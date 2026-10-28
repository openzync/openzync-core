"""Prompt template service — org-scoped overrides plus cache invalidation.

DB write first, Redis delete second (best-effort, log-and-continue) —
routers never touch Redis directly.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.prompt_template import PromptTemplate
from repositories.prompt_template_repository import PromptTemplateRepository

logger = logging.getLogger(__name__)


class PromptTemplateService:
    """Business logic for org-scoped prompt template overrides.

    Args:
        db: The request-scoped async session (RLS-tenanted).
        redis: Async Redis client, or ``None`` when unavailable.
    """

    def __init__(self, db: AsyncSession, redis: Any | None = None) -> None:
        self._db = db
        self._redis = redis

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
        template = await PromptTemplateRepository(self._db).set_for_org(
            org_id=org_id,
            name=name,
            text=text,
            desc=desc,
            template_type=template_type,
        )
        if self._redis is not None:
            try:
                await self._redis.delete(f"prompt_template:{org_id}:{name}")
            except Exception:
                logger.exception(
                    "prompt_template.cache_invalidate_failed",
                    extra={"org_id": str(org_id), "name": name},
                )
        return template
