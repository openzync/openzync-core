"""Tests for ``ExtractionSchema`` model."""
from __future__ import annotations

import uuid

import pytest

from models.extraction_schema import ExtractionSchema


class TestExtractionSchemaModel:
    """Cover ExtractionSchema fields — name, json_schema, type, is_active."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        schema = ExtractionSchema(
            organization_id=uuid.uuid4(),
            name="customer_profile",
            json_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        )
        assert schema.organization_id is not None
        assert schema.name == "customer_profile"
        assert schema.json_schema == {"type": "object", "properties": {"name": {"type": "string"}}}

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """type has server_default='structured', is_active has server_default='true'."""
        col_type = ExtractionSchema.__table__.columns["type"]
        assert col_type.server_default is not None
        assert "structured" in str(col_type.server_default.arg)
        col_active = ExtractionSchema.__table__.columns["is_active"]
        assert col_active.server_default is not None
        assert "true" in str(col_active.server_default.arg)

    @pytest.mark.unit
    def test_nullable_prompt_template(self) -> None:
        """prompt_template defaults to None."""
        schema = ExtractionSchema(
            organization_id=uuid.uuid4(),
            name="test",
            json_schema={},
        )
        assert schema.prompt_template is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is extraction_schemas."""
        assert ExtractionSchema.__tablename__ == "extraction_schemas"

    @pytest.mark.unit
    def test_unique_constraint(self) -> None:
        """UniqueConstraint on (organization_id, name)."""
        uq_name = "uq_extraction_schema_org_name"
        constraints = ExtractionSchema.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert uq_name in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes id, org, name."""
        schema = ExtractionSchema(
            organization_id=uuid.uuid4(),
            name="test",
            json_schema={},
        )
        assert "ExtractionSchema" in repr(schema)
        assert "test" in repr(schema)
