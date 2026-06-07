"""Schema CRUD API — manage extraction and classification schemas per organization.

Endpoints:
    POST   /v1/admin/schemas        — Create a new schema (requires ``admin`` scope)
    GET    /v1/admin/schemas        — List schemas for the org (authenticated)
    GET    /v1/admin/schemas/{id}   — Get a single schema by ID
    PUT    /v1/admin/schemas/{id}   — Update a schema (requires ``admin`` scope)
    DELETE /v1/admin/schemas/{id}   — Soft-delete a schema (requires ``admin`` scope)
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import require_org_id, require_scope
from dependencies.db import get_db
from repositories.extraction_schema_repository import (
    ExtractionSchemaRepository,
)
from schemas.extraction_schemas import (
    CreateExtractionSchemaRequest,
    ExtractionSchemaListResponse,
    ExtractionSchemaResponse,
    UpdateExtractionSchemaRequest,
)
from services.schema_service import SchemaService

router = APIRouter(
    prefix="/v1/admin/schemas",
    tags=["Admin - Schemas"],
)


def _get_schema_service(
    db: AsyncSession = Depends(get_db),
) -> SchemaService:
    """Dependency factory for ``SchemaService``."""
    return SchemaService(repo=ExtractionSchemaRepository(db))


@router.post(
    "",
    response_model=ExtractionSchemaResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_schema(
    payload: CreateExtractionSchemaRequest,
    service: SchemaService = Depends(_get_schema_service),
    org_id: str = Depends(require_scope("admin")),
) -> ExtractionSchemaResponse:
    """Create a new extraction or classification schema.

    Requires ``admin`` scope.  The schema name must be unique within the
    organization.  For ``type='classification'``, the ``json_schema`` must
    follow the expected classification label structure.
    """
    return await service.create_schema(
        org_id=UUID(org_id),
        payload=payload,
    )


@router.get(
    "",
    response_model=ExtractionSchemaListResponse,
)
async def list_schemas(
    type: str | None = Query(
        default=None,
        pattern=r"^(structured|classification)$",
        description="Filter by schema type",
    ),
    is_active: bool | None = Query(
        default=None,
        description="Filter by active status",
    ),
    service: SchemaService = Depends(_get_schema_service),
    org_id: str = Depends(require_org_id),
) -> ExtractionSchemaListResponse:
    """List all schemas for the authenticated organization.

    Supports optional filtering by ``type`` (structured/classification) and
    ``is_active`` status.
    """
    schemas = await service.list_schemas(
        org_id=UUID(org_id),
        schema_type=type,
        is_active=is_active,
    )
    return ExtractionSchemaListResponse(
        data=schemas,
        total=len(schemas),
    )


@router.get(
    "/{schema_id}",
    response_model=ExtractionSchemaResponse,
)
async def get_schema(
    schema_id: UUID,
    service: SchemaService = Depends(_get_schema_service),
    org_id: str = Depends(require_org_id),
) -> ExtractionSchemaResponse:
    """Get a single schema by ID.  Scoped to the authenticated organization."""
    return await service.get_schema(
        org_id=UUID(org_id),
        schema_id=schema_id,
    )


@router.put(
    "/{schema_id}",
    response_model=ExtractionSchemaResponse,
)
async def update_schema(
    schema_id: UUID,
    payload: UpdateExtractionSchemaRequest,
    service: SchemaService = Depends(_get_schema_service),
    org_id: str = Depends(require_scope("admin")),
) -> ExtractionSchemaResponse:
    """Update an existing schema.

    Requires ``admin`` scope.  The ``type`` field is immutable after creation.
    Name uniqueness is enforced within the organization.
    """
    return await service.update_schema(
        org_id=UUID(org_id),
        schema_id=schema_id,
        payload=payload,
    )


@router.delete(
    "/{schema_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_schema(
    schema_id: UUID,
    service: SchemaService = Depends(_get_schema_service),
    org_id: str = Depends(require_scope("admin")),
) -> None:
    """Soft-delete a schema (set ``is_active`` to ``false``).

    Requires ``admin`` scope.  Existing extractions referencing this schema
    are preserved (FK uses ``ON DELETE SET NULL``).
    """
    await service.delete_schema(
        org_id=UUID(org_id),
        schema_id=schema_id,
    )
