"""Unit tests for SchemaService — extraction schema CRUD and validation.

All external dependencies (the extraction schema repository) are mocked at the
service boundary.  Private validation helpers are tested directly — pure logic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from core.exceptions import ConflictError, NotFoundError, ValidationError
from models.extraction_schema import ExtractionSchema
from schemas.extraction_schemas import (
    CreateExtractionSchemaRequest,
    ExtractionSchemaResponse,
    UpdateExtractionSchemaRequest,
)
from services.schema_service import SchemaService


@pytest.mark.unit
class TestSchemaService:
    """Unit tests for ``SchemaService`` — CRUD and validation."""

    ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
    SCHEMA_ID = UUID("00000000-0000-0000-0000-000000000010")
    OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_service(self) -> tuple[SchemaService, AsyncMock]:
        """Create SchemaService with a mocked repository."""
        mock_repo = AsyncMock()
        mock_repo.get_by_id = AsyncMock()
        mock_repo.get_by_name = AsyncMock()
        mock_repo.get_all = AsyncMock()
        mock_repo.create = AsyncMock()
        mock_repo.update = AsyncMock()
        mock_repo.soft_delete = AsyncMock()
        service = SchemaService(repo=mock_repo)
        return service, mock_repo

    def _make_schema(
        self,
        schema_id: UUID | None = None,
        org_id: UUID | None = None,
        name: str = "Test Schema",
        type: str = "structured",
        json_schema: dict | None = None,
        prompt_template: str | None = None,
        is_active: bool = True,
    ) -> MagicMock:
        """Build a MagicMock mimicking an ExtractionSchema ORM model."""
        schema = MagicMock(spec=ExtractionSchema)
        schema.id = schema_id or self.SCHEMA_ID
        schema.organization_id = org_id or self.ORG_ID
        schema.name = name
        schema.type = type
        schema.json_schema = json_schema or {"type": "object", "properties": {}}
        schema.prompt_template = prompt_template
        schema.is_active = is_active
        schema.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        schema.updated_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        return schema

    def _make_classification_schema(self) -> dict:
        """Return a valid classification schema dict."""
        return {
            "intent": ["greeting", "question"],
            "emotion": ["joy", "frustration"],
            "valence": ["positive", "negative", "neutral"],
            "arousal": ["low", "medium", "high"],
        }

    # ── create_schema ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_create_schema_structured_validates_json_schema(self) -> None:
        """``create_schema`` with structured type validates JSON schema."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_name.return_value = None
        mock_repo.create.return_value = self._make_schema()

        payload = CreateExtractionSchemaRequest(
            name="invoice_extraction",
            json_schema={"type": "object", "properties": {"amount": {"type": "number"}}},
            type="structured",
        )
        result = await service.create_schema(self.ORG_ID, payload)

        assert isinstance(result, ExtractionSchemaResponse)
        assert result.name == "Test Schema"
        mock_repo.get_by_name.assert_awaited_once_with(self.ORG_ID, "invoice_extraction")
        mock_repo.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_schema_classification_validates_schema(self) -> None:
        """``create_schema`` with classification type validates label schema."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_name.return_value = None
        mock_repo.create.return_value = self._make_schema()

        payload = CreateExtractionSchemaRequest(
            name="intent_labels",
            json_schema=self._make_classification_schema(),
            type="classification",
        )
        result = await service.create_schema(self.ORG_ID, payload)

        assert isinstance(result, ExtractionSchemaResponse)
        mock_repo.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_schema_duplicate_name_raises_conflict(self) -> None:
        """``create_schema`` with existing name raises ConflictError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_name.return_value = self._make_schema(name="existing")

        payload = CreateExtractionSchemaRequest(
            name="existing",
            json_schema={"type": "object"},
        )
        with pytest.raises(ConflictError, match="already exists"):
            await service.create_schema(self.ORG_ID, payload)

        mock_repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_schema_non_dict_json_schema_raises_validation_error(
        self,
    ) -> None:
        """``create_schema`` rejects non-dict json_schema for classification."""
        service, mock_repo = self._make_service()
        # Use model_construct to bypass Pydantic's dict-type validation so we
        # can test the service-layer validation of the json_schema content.
        payload = CreateExtractionSchemaRequest.model_construct(
            name="bad_labels",
            json_schema="not_a_dict",  # type: ignore[arg-type]
            type="classification",
        )
        with pytest.raises(ValidationError, match="JSON object"):
            await service.create_schema(self.ORG_ID, payload)

        mock_repo.get_by_name.assert_not_awaited()
        mock_repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_schema_empty_list_field_raises_validation_error(
        self,
    ) -> None:
        """``create_schema`` rejects classification schema with empty label list."""
        service, mock_repo = self._make_service()

        payload = CreateExtractionSchemaRequest(
            name="empty_labels",
            json_schema={"intent": []},
            type="classification",
        )
        with pytest.raises(ValidationError, match="at least one label"):
            await service.create_schema(self.ORG_ID, payload)

        mock_repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_schema_non_string_items_raises_validation_error(
        self,
    ) -> None:
        """``create_schema`` rejects classification schema with non-string items."""
        service, mock_repo = self._make_service()

        payload = CreateExtractionSchemaRequest(
            name="bad_items",
            json_schema={"emotion": [1, 2, 3]},
            type="classification",
        )
        with pytest.raises(ValidationError, match="non-empty strings"):
            await service.create_schema(self.ORG_ID, payload)

        mock_repo.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_schema_integrity_error_raises_conflict(self) -> None:
        """``create_schema`` wraps IntegrityError from repo as ConflictError."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_name.return_value = None
        mock_repo.create.side_effect = IntegrityError("stmt", "params", BaseException())

        payload = CreateExtractionSchemaRequest(
            name="duplicate",
            json_schema={"type": "object"},
        )
        with pytest.raises(ConflictError, match="already exists"):
            await service.create_schema(self.ORG_ID, payload)

    # ── list_schemas ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_list_schemas_without_filters(self) -> None:
        """``list_schemas`` returns all schemas when no filters provided."""
        service, mock_repo = self._make_service()
        mock_repo.get_all.return_value = [
            self._make_schema(name="Schema A"),
            self._make_schema(name="Schema B"),
        ]

        result = await service.list_schemas(self.ORG_ID)

        assert len(result) == 2
        assert result[0].name == "Schema A"
        assert result[1].name == "Schema B"
        mock_repo.get_all.assert_awaited_once_with(
            org_id=self.ORG_ID, schema_type=None, is_active=None,
        )

    @pytest.mark.asyncio
    async def test_list_schemas_with_filters(self) -> None:
        """``list_schemas`` filters by type and active flag."""
        service, mock_repo = self._make_service()
        mock_repo.get_all.return_value = [self._make_schema(name="Classification")]

        result = await service.list_schemas(self.ORG_ID, schema_type="classification", is_active=True)

        assert len(result) == 1
        assert result[0].name == "Classification"
        mock_repo.get_all.assert_awaited_once_with(
            org_id=self.ORG_ID, schema_type="classification", is_active=True,
        )

    @pytest.mark.asyncio
    async def test_list_schemas_empty(self) -> None:
        """``list_schemas`` returns empty list when no schemas exist."""
        service, mock_repo = self._make_service()
        mock_repo.get_all.return_value = []

        result = await service.list_schemas(self.ORG_ID)
        assert result == []

    # ── get_schema ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_schema_returns_response(self) -> None:
        """``get_schema`` returns ExtractionSchemaResponse for existing schema."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_schema()

        result = await service.get_schema(self.ORG_ID, self.SCHEMA_ID)

        assert isinstance(result, ExtractionSchemaResponse)
        assert result.name == "Test Schema"
        mock_repo.get_by_id.assert_awaited_once_with(self.ORG_ID, self.SCHEMA_ID)

    @pytest.mark.asyncio
    async def test_get_schema_not_found_raises_not_found(self) -> None:
        """``get_schema`` raises NotFoundError when schema does not exist."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError, match="not found"):
            await service.get_schema(self.ORG_ID, self.SCHEMA_ID)

    # ── update_schema ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_schema_success(self) -> None:
        """``update_schema`` updates fields and returns the updated schema."""
        service, mock_repo = self._make_service()
        original = self._make_schema(name="Original")
        mock_repo.get_by_id.return_value = original
        mock_repo.get_by_name.return_value = None  # name is unique
        updated = self._make_schema(name="Updated")
        mock_repo.update.return_value = updated

        payload = UpdateExtractionSchemaRequest(name="Updated", prompt_template="New template")
        result = await service.update_schema(self.ORG_ID, self.SCHEMA_ID, payload)

        assert result.name == "Updated"
        mock_repo.update.assert_awaited_once_with(original, name="Updated", prompt_template="New template")

    @pytest.mark.asyncio
    async def test_update_schema_nothing_to_update_returns_current(self) -> None:
        """``update_schema`` returns current schema when no fields provided."""
        service, mock_repo = self._make_service()
        original = self._make_schema(name="Unchanged")
        mock_repo.get_by_id.return_value = original

        payload = UpdateExtractionSchemaRequest()
        result = await service.update_schema(self.ORG_ID, self.SCHEMA_ID, payload)

        assert result.name == "Unchanged"
        mock_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_schema_not_found_raises_not_found(self) -> None:
        """``update_schema`` raises NotFoundError when schema missing."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = None

        payload = UpdateExtractionSchemaRequest(name="Anything")
        with pytest.raises(NotFoundError, match="not found"):
            await service.update_schema(self.ORG_ID, self.SCHEMA_ID, payload)

        mock_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_schema_name_conflict_raises_conflict(self) -> None:
        """``update_schema`` raises ConflictError when new name is taken."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = self._make_schema(name="Current")
        mock_repo.get_by_name.return_value = self._make_schema(name="Taken")

        payload = UpdateExtractionSchemaRequest(name="Taken")
        with pytest.raises(ConflictError, match="already exists"):
            await service.update_schema(self.ORG_ID, self.SCHEMA_ID, payload)

        mock_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_schema_classification_json_schema_validated(self) -> None:
        """``update_schema`` validates classification json_schema when updating."""
        service, mock_repo = self._make_service()
        original = self._make_schema(name="Labels", type="classification",
                                     json_schema=self._make_classification_schema())
        mock_repo.get_by_id.return_value = original

        payload = UpdateExtractionSchemaRequest(
            json_schema={"intent": [], "emotion": ["joy"]},
        )
        with pytest.raises(ValidationError, match="at least one label"):
            await service.update_schema(self.ORG_ID, self.SCHEMA_ID, payload)

        mock_repo.update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_schema_integrity_error_raises_conflict(self) -> None:
        """``update_schema`` wraps IntegrityError from repo as ConflictError."""
        service, mock_repo = self._make_service()
        original = self._make_schema(name="Current")
        mock_repo.get_by_id.return_value = original
        mock_repo.get_by_name.return_value = None  # name is unique
        mock_repo.update.side_effect = IntegrityError("stmt", "params", BaseException())

        payload = UpdateExtractionSchemaRequest(name="Conflict")
        with pytest.raises(ConflictError, match="already exists"):
            await service.update_schema(self.ORG_ID, self.SCHEMA_ID, payload)

    # ── delete_schema ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_schema_success(self) -> None:
        """``delete_schema`` soft-deletes an existing schema."""
        service, mock_repo = self._make_service()
        schema = self._make_schema()
        mock_repo.get_by_id.return_value = schema

        await service.delete_schema(self.ORG_ID, self.SCHEMA_ID)

        mock_repo.get_by_id.assert_awaited_once_with(self.ORG_ID, self.SCHEMA_ID)
        mock_repo.soft_delete.assert_awaited_once_with(schema)

    @pytest.mark.asyncio
    async def test_delete_schema_not_found_raises_not_found(self) -> None:
        """``delete_schema`` raises NotFoundError when schema does not exist."""
        service, mock_repo = self._make_service()
        mock_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError, match="not found"):
            await service.delete_schema(self.ORG_ID, self.SCHEMA_ID)

        mock_repo.soft_delete.assert_not_awaited()

    # ── _validate_classification_schema ─────────────────────────────────────

    def test_validate_classification_schema_non_dict_raises(self) -> None:
        """``_validate_classification_schema`` rejects non-dict input."""
        service, _mock_repo = self._make_service()

        with pytest.raises(ValidationError, match="JSON object"):
            service._validate_classification_schema("not_a_dict")  # type: ignore[arg-type]

    def test_validate_classification_schema_wrong_type_raises(self) -> None:
        """``_validate_classification_schema`` rejects field with wrong type."""
        service, _mock_repo = self._make_service()

        with pytest.raises(ValidationError, match="must be a list"):
            service._validate_classification_schema({"intent": "not_a_list"})

    def test_validate_classification_schema_empty_string_item_raises(self) -> None:
        """``_validate_classification_schema`` rejects empty string in list."""
        service, _mock_repo = self._make_service()

        with pytest.raises(ValidationError, match="non-empty strings"):
            service._validate_classification_schema({"emotion": ["joy", ""]})

    def test_validate_classification_schema_empty_list_raises(self) -> None:
        """``_validate_classification_schema`` rejects empty list."""
        service, _mock_repo = self._make_service()

        with pytest.raises(ValidationError, match="at least one label"):
            service._validate_classification_schema({"valence": []})

    def test_validate_classification_schema_valid_passes(self) -> None:
        """``_validate_classification_schema`` passes for valid input."""
        service, _mock_repo = self._make_service()

        # Should not raise
        service._validate_classification_schema(self._make_classification_schema())

    def test_validate_classification_schema_partial_keys_valid(self) -> None:
        """``_validate_classification_schema`` passes with only some keys."""
        service, _mock_repo = self._make_service()

        # Only the keys present are validated — missing keys are skipped
        service._validate_classification_schema({"intent": ["hello", "bye"]})

    # ── _validate_json_schema ───────────────────────────────────────────────

    def test_validate_json_schema_non_dict_raises(self) -> None:
        """``_validate_json_schema`` rejects non-dict input."""
        service, _mock_repo = self._make_service()

        with pytest.raises(ValidationError, match="JSON object"):
            service._validate_json_schema("not_a_dict")  # type: ignore[arg-type]

    @patch("services.schema_service.logger")
    def test_validate_json_schema_without_jsonschema_skips(
        self, mock_logger: MagicMock,
    ) -> None:
        """``_validate_json_schema`` skips validation when jsonschema not installed."""
        service, _mock_repo = self._make_service()

        # Simulate ImportError by making jsonschema import raise
        with patch("builtins.__import__", side_effect=ImportError("no jsonschema")):
            service._validate_json_schema({"type": "object"})

        mock_logger.warning.assert_called_once()

    def test_validate_json_schema_valid_schema_passes(self) -> None:
        """``_validate_json_schema`` passes for a valid JSON Schema."""
        service, _mock_repo = self._make_service()

        # Should not raise (jsonschema is available in test env)
        service._validate_json_schema({"type": "object", "properties": {}})

    def test_validate_json_schema_invalid_schema_raises(self) -> None:
        """``_validate_json_schema`` raises ValidationError for invalid schema."""
        service, _mock_repo = self._make_service()

        with pytest.raises(ValidationError, match="Invalid JSON Schema"):
            service._validate_json_schema({"type": 123})
