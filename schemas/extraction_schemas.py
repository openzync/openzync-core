"""Pydantic schemas for extraction schema CRUD operations.

Schemas define the JSON Schema contracts an organization uses for structured
extractions (``type='structured'``) or classification label sets
(``type='classification'``).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateExtractionSchemaRequest(BaseModel):
    """Request body for creating a new extraction schema.

    Attributes:
        name: Human-readable name, unique within the organization.
            Must start with a letter and contain only alphanumeric,
            underscore, hyphen, or space characters.
        json_schema: For ``type='structured'``, a valid JSON Schema document.
            For ``type='classification'``, a dict with keys ``intent``,
            ``emotion``, ``valence``, ``arousal`` each containing a list of
            allowed label strings.
        type: Schema type — ``'structured'`` (default) or ``'classification'``.
        prompt_template: Optional per-schema prompt template override.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_\- ]*$",
        examples=["invoice_extraction", "customer_intent_labels"],
    )
    json_schema: dict = Field(..., examples=[{"type": "object", "properties": {}}])
    type: str = Field(
        default="structured",
        pattern=r"^(structured|classification)$",
    )
    prompt_template: str | None = Field(default=None, max_length=10000)


class UpdateExtractionSchemaRequest(BaseModel):
    """Request body for updating an existing extraction schema.

    All fields are optional.  The ``type`` field is immutable after creation.
    """

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_\- ]*$",
    )
    json_schema: dict | None = None
    prompt_template: str | None = Field(default=None, max_length=10000)
    is_active: bool | None = None


class ExtractionSchemaResponse(BaseModel):
    """Response model for a single extraction schema."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    name: str
    type: str
    json_schema: dict
    prompt_template: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ExtractionSchemaListResponse(BaseModel):
    """Response model for listing extraction schemas."""

    data: list[ExtractionSchemaResponse]
    total: int
