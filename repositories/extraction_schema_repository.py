"""Repository for extraction schemas — all DB access for ``extraction_schemas``.

All methods accept an ``org_id`` parameter that must match the authenticated
organization's UUID.  RLS ensures cross-tenant isolation at the database level.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.sorting import SortSpec, resolve_order_by
from models.extraction_schema import ExtractionSchema

EXTRACTION_SCHEMA_SORTABLE_COLUMNS = {
    "name": ExtractionSchema.name,
    "created_at": ExtractionSchema.created_at,
    "type": ExtractionSchema.type,
}
"""Sortable columns for admin schemas (default created_at/desc)."""


class ExtractionSchemaRepository:
    """Data access for ``extraction_schemas``."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_id(self, org_id: UUID, schema_id: UUID) -> ExtractionSchema | None:
        """Fetch a single schema by ID, scoped to the organization."""
        result = await self._db.execute(
            select(ExtractionSchema).where(
                ExtractionSchema.id == schema_id,
                ExtractionSchema.organization_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_name(
        self, org_id: UUID, name: str
    ) -> ExtractionSchema | None:
        """Fetch a single schema by name within an organization."""
        result = await self._db.execute(
            select(ExtractionSchema).where(
                ExtractionSchema.organization_id == org_id,
                ExtractionSchema.name == name,
            )
        )
        return result.scalar_one_or_none()

    async def get_all(
        self,
        org_id: UUID,
        schema_type: str | None = None,
        is_active: bool | None = None,
        sort: SortSpec | None = None,
    ) -> list[ExtractionSchema]:
        """List schemas for an organization, with optional type/active filters.

        Default ``created_at/desc``; whitelist ``name``, ``created_at``,
        ``type``.
        """
        query = select(ExtractionSchema).where(
            ExtractionSchema.organization_id == org_id
        )
        if schema_type is not None:
            query = query.where(ExtractionSchema.type == schema_type)
        if is_active is not None:
            query = query.where(ExtractionSchema.is_active == is_active)
        spec = sort if sort is not None else SortSpec()
        req_sort, req_dir = spec.effective("created_at", "desc")
        query = query.order_by(
            *resolve_order_by(
                EXTRACTION_SCHEMA_SORTABLE_COLUMNS,
                ExtractionSchema.id,
                req_sort,
                req_dir,
                default_sort_by="created_at",
                default_dir="desc",
            )
        )
        result = await self._db.execute(query)
        return list(result.scalars().all())

    async def create(
        self,
        org_id: UUID,
        name: str,
        json_schema: dict,
        type: str = "structured",
        prompt_template: str | None = None,
    ) -> ExtractionSchema:
        """Create a new extraction schema.

        Args:
            org_id: Owning organization UUID.
            name: Schema name (must be unique per org).
            json_schema: JSON Schema or classification label definitions.
            type: Schema type — ``'structured'`` or ``'classification'``.
            prompt_template: Optional prompt template override.

        Returns:
            The newly created ``ExtractionSchema`` instance.

        Raises:
            sqlalchemy.exc.IntegrityError: If a schema with the same name
                already exists in this organization.
        """
        schema = ExtractionSchema(
            organization_id=org_id,
            name=name,
            type=type,
            json_schema=json_schema,
            prompt_template=prompt_template,
        )
        self._db.add(schema)
        await self._db.flush()
        await self._db.refresh(schema)
        return schema

    async def update(
        self,
        schema: ExtractionSchema,
        **kwargs: dict,
    ) -> ExtractionSchema:
        """Update an existing schema with the given keyword arguments.

        Only the provided fields are updated.  The ``type`` field is
        intentionally excluded — it is immutable after creation.
        """
        for key, value in kwargs.items():
            if key == "type":
                continue  # type is immutable
            setattr(schema, key, value)
        await self._db.flush()
        await self._db.refresh(schema)
        return schema

    async def soft_delete(self, schema: ExtractionSchema) -> None:
        """Set ``is_active`` to ``False`` on the given schema."""
        schema.is_active = False
        await self._db.flush()

    async def count_for_org(
        self, org_id: UUID, schema_type: str | None = None
    ) -> int:
        """Count schemas for an org, optionally filtered by type."""
        query = select(func.count()).select_from(ExtractionSchema).where(
            ExtractionSchema.organization_id == org_id
        )
        if schema_type is not None:
            query = query.where(ExtractionSchema.type == schema_type)
        result = await self._db.execute(query)
        return result.scalar_one()

    async def get_classification_labels(
        self, org_id: UUID
    ) -> list[dict]:
        """Fetch all active classification schema JSON definitions.

        Used by the ``classify_dialog`` worker to determine label sets.
        """
        result = await self._db.execute(
            select(ExtractionSchema.json_schema).where(
                ExtractionSchema.organization_id == org_id,
                ExtractionSchema.type == "classification",
                ExtractionSchema.is_active == True,
            )
        )
        return [row[0] for row in result.all()]
