"""Pydantic schemas for prompt template CRUD and versioning."""

from __future__ import annotations

from datetime import datetime

from typing import Any

from pydantic import BaseModel, Field, model_validator


class PromptTemplateSummary(BaseModel):
    """Lightweight representation of a template for list views."""

    name: str
    version: int
    is_customised: bool
    description: str | None
    type: str | None = None
    is_default_for_type: bool = False
    updated_at: datetime


class PromptTemplateDetail(BaseModel):
    """Full template detail including the prompt text.

    Used for version detail endpoints and version history.
    The ``is_system_default`` field is derived from the ORM model's
    ``@property`` — it's ``True`` when the template is the active
    system default (``organization_id IS NULL`` and ``is_active``).
    """

    name: str
    version: int
    template_text: str
    description: str | None
    type: str | None = None
    is_active: bool
    is_default_for_type: bool = False
    is_system_default: bool = False

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _norm_template_name(cls, data: Any) -> Any:
        """Map ORM ``template_name`` attribute to schema ``name`` field."""
        if isinstance(data, dict):
            if "template_name" in data and "name" not in data:
                data["name"] = data.pop("template_name")
            return data
        # ORM object — Pydantic's from_attributes reads by field name.
        # Since the ORM uses ``template_name`` and the schema uses ``name``,
        # we convert to a dict to let the validator handle the rename.
        if hasattr(data, "template_name") and not hasattr(data, "name"):
            return {
                "name": data.template_name,
                "version": data.version,
                "template_text": data.template_text,
                "description": data.description,
                "type": getattr(data, "type", None),
                "is_active": data.is_active,
                "is_default_for_type": getattr(data, "is_default_for_type", False),
                "is_system_default": getattr(data, "is_system_default", False),
            }
        return data


class PromptTemplateListResponse(BaseModel):
    """Wrapper for the list-names endpoint."""

    data: list[PromptTemplateSummary]


class PromptTemplateVersionsResponse(BaseModel):
    """All versions of a named template for the current org."""

    name: str
    current_version: int
    versions: list[PromptTemplateDetail]


class SetPromptTemplateRequest(BaseModel):
    """Request body to create or update a prompt template for an org."""

    template_text: str = Field(..., min_length=1)
    description: str | None = None
    type: str | None = Field(
        default=None,
        description="Type classifier for the template (e.g. fact_extraction, entity_extraction).",
    )


class SystemTemplateEntry(BaseModel):
    """A single system-default template version shown in the import browser."""

    name: str
    version: int
    type: str | None = None
    is_active: bool
    is_default_for_type: bool = False
    is_system_default: bool
    description: str | None = None


class SystemPromptGroup(BaseModel):
    """A group of system-default template versions sharing a type."""

    type: str
    templates: list[SystemTemplateEntry]
    imported: list[str]


class SystemPromptGroupsResponse(BaseModel):
    """Response wrapper for the system prompt browser."""

    groups: list[SystemPromptGroup]


class ImportPromptRequest(BaseModel):
    """Request body to import a system prompt template into the org."""

    template_name: str = Field(
        ...,
        min_length=1,
        description="The exact template name to import (e.g. extract_facts_v2).",
    )
