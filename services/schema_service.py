"""Service layer for extraction schema CRUD — business logic and validation.

Orgs define extraction schemas for two purposes:
1. **Structured extraction** (``type='structured'``) — JSON Schema documents
   that the LLM must conform to when extracting data.
2. **Classification labels** (``type='classification'``) — label sets that
   define the intent, emotion, valence, and arousal categories for dialog
   classification.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from core.exceptions import ConflictError, NotFoundError, ValidationError
from repositories.extraction_schema_repository import (
    ExtractionSchemaRepository,
)
from schemas.extraction_schemas import (
    CreateExtractionSchemaRequest,
    ExtractionSchemaResponse,
    UpdateExtractionSchemaRequest,
)

logger = logging.getLogger(__name__)

# Valid classification schema keys and their expected types
_CLASSIFICATION_SCHEMA_KEYS = {
    "intent": list,
    "emotion": list,
    "valence": list,
    "arousal": list,
}


class SchemaService:
    """Business logic for managing extraction schemas."""

    def __init__(self, repo: ExtractionSchemaRepository) -> None:
        self._repo = repo

    async def create_schema(
        self,
        org_id: UUID,
        payload: CreateExtractionSchemaRequest,
    ) -> ExtractionSchemaResponse:
        """Create a new extraction schema for an organization.

        Args:
            org_id: The authenticated organization UUID.
            payload: Validated creation request.

        Returns:
            The newly created schema as a response model.

        Raises:
            ConflictError: If a schema with the same name already exists.
            ValidationError: If the payload fails domain validation.
        """
        # Validate classification schema structure
        if payload.type == "classification":
            self._validate_classification_schema(payload.json_schema)

        # Validate structured schema is valid JSON Schema
        if payload.type == "structured":
            self._validate_json_schema(payload.json_schema)

        # Check uniqueness before insert
        existing = await self._repo.get_by_name(org_id, payload.name)
        if existing is not None:
            raise ConflictError(
                f"Schema '{payload.name}' already exists in this organization"
            )

        try:
            schema = await self._repo.create(
                org_id=org_id,
                name=payload.name,
                json_schema=payload.json_schema,
                type=payload.type,
                prompt_template=payload.prompt_template,
            )
        except IntegrityError:
            raise ConflictError(
                f"Schema '{payload.name}' already exists in this organization"
            ) from None

        return ExtractionSchemaResponse.model_validate(schema)

    async def list_schemas(
        self,
        org_id: UUID,
        schema_type: str | None = None,
        is_active: bool | None = None,
    ) -> list[ExtractionSchemaResponse]:
        """List schemas for an organization with optional filters."""
        schemas = await self._repo.get_all(
            org_id=org_id,
            schema_type=schema_type,
            is_active=is_active,
        )
        return [
            ExtractionSchemaResponse.model_validate(s) for s in schemas
        ]

    async def get_schema(
        self,
        org_id: UUID,
        schema_id: UUID,
    ) -> ExtractionSchemaResponse:
        """Get a single schema by ID.

        Raises:
            NotFoundError: If the schema does not exist or belongs to another org.
        """
        schema = await self._repo.get_by_id(org_id, schema_id)
        if schema is None:
            raise NotFoundError(
                f"Schema '{schema_id}' not found in this organization"
            )
        return ExtractionSchemaResponse.model_validate(schema)

    async def update_schema(
        self,
        org_id: UUID,
        schema_id: UUID,
        payload: UpdateExtractionSchemaRequest,
    ) -> ExtractionSchemaResponse:
        """Update an existing schema.

        The ``type`` field is immutable after creation and will be ignored
        if included in the update payload.

        Raises:
            NotFoundError: If the schema does not exist.
            ConflictError: If the new name conflicts with an existing schema.
        """
        schema = await self._repo.get_by_id(org_id, schema_id)
        if schema is None:
            raise NotFoundError(
                f"Schema '{schema_id}' not found in this organization"
            )

        # Build update dict from non-None fields (excluding type)
        update_kwargs: dict = {}
        for field in ("name", "json_schema", "prompt_template", "is_active"):
            value = getattr(payload, field, None)
            if value is not None:
                update_kwargs[field] = value

        if not update_kwargs:
            # Nothing to update — return current state
            return ExtractionSchemaResponse.model_validate(schema)

        # If renaming, check uniqueness
        if "name" in update_kwargs and update_kwargs["name"] != schema.name:
            existing = await self._repo.get_by_name(org_id, update_kwargs["name"])
            if existing is not None:
                raise ConflictError(
                    f"Schema name '{update_kwargs['name']}' already exists "
                    "in this organization"
                )

        # If updating json_schema with type=classification, validate
        if "json_schema" in update_kwargs and schema.type == "classification":
            self._validate_classification_schema(update_kwargs["json_schema"])

        try:
            updated = await self._repo.update(schema, **update_kwargs)
        except IntegrityError:
            raise ConflictError(
                f"Schema name '{update_kwargs.get('name', schema.name)}' "
                "already exists in this organization"
            ) from None

        return ExtractionSchemaResponse.model_validate(updated)

    async def delete_schema(
        self,
        org_id: UUID,
        schema_id: UUID,
    ) -> None:
        """Soft-delete a schema (set ``is_active=false``).

        Raises:
            NotFoundError: If the schema does not exist.
        """
        schema = await self._repo.get_by_id(org_id, schema_id)
        if schema is None:
            raise NotFoundError(
                f"Schema '{schema_id}' not found in this organization"
            )
        await self._repo.soft_delete(schema)

    # ── Private validation helpers ───────────────────────────────────────

    def _validate_classification_schema(self, json_schema: dict) -> None:
        """Validate that *json_schema* has the expected classification shape.

        Expected structure:
        .. code-block:: python
            {
                "intent": ["greeting", "question", ...],
                "emotion": ["joy", "frustration", ...],
                "valence": ["positive", "negative", "neutral"],
                "arousal": ["low", "medium", "high"]
            }

        All keys are optional — only those present are validated.
        """
        if not isinstance(json_schema, dict):
            raise ValidationError(
                "Classification schema must be a JSON object"
            )

        for key, expected_type in _CLASSIFICATION_SCHEMA_KEYS.items():
            value = json_schema.get(key)
            if value is not None:
                if not isinstance(value, expected_type):
                    raise ValidationError(
                        f"Classification schema field '{key}' must be a "
                        f"{expected_type.__name__}, got {type(value).__name__}"
                    )
                if expected_type is list:
                    if len(value) == 0:
                        raise ValidationError(
                            f"Classification schema field '{key}' must "
                            "contain at least one label"
                        )
                    for item in value:
                        if not isinstance(item, str) or not item.strip():
                            raise ValidationError(
                                f"Classification schema field '{key}' "
                                "must contain only non-empty strings"
                            )

    def _validate_json_schema(self, json_schema: dict) -> None:
        """Validate that *json_schema* is a valid JSON Schema draft-07.

        Uses the ``jsonschema`` library's draft-07 validator for schema
        validation.  This does **not** validate data against the schema —
        only that the schema itself is well-formed.
        """
        if not isinstance(json_schema, dict):
            raise ValidationError("JSON Schema must be a JSON object")

        try:
            import jsonschema

            jsonschema.Draft7Validator.check_schema(json_schema)
        except ImportError:
            # jsonschema is optional — skip validation if not installed
            logger.warning("jsonschema library not available — skipping schema validation")
        except jsonschema.SchemaError as exc:
            raise ValidationError(
                f"Invalid JSON Schema: {exc.message}"
            ) from exc
