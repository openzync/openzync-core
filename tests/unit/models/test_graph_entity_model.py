"""Tests for ``GraphEntity`` model — read-only stub for FK resolution."""
from __future__ import annotations

import uuid

import pytest

from models.graph_entity import GraphEntity


class TestGraphEntityModel:
    """Cover GraphEntity fields — name, entity_type, summary, attributes."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        entity = GraphEntity(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            name="Alice",
        )
        assert entity.organization_id is not None
        assert entity.project_id is not None
        assert entity.name == "Alice"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """entity_type has server_default='custom', attributes has server_default='{}'."""
        col_type = GraphEntity.__table__.columns["entity_type"]
        assert col_type.server_default is not None
        assert "custom" in str(col_type.server_default.arg)
        col_attrs = GraphEntity.__table__.columns["attributes"]
        assert col_attrs.server_default is not None
        assert "{}" in str(col_attrs.server_default.arg)

    @pytest.mark.unit
    def test_nullable_summary(self) -> None:
        """summary defaults to None."""
        entity = GraphEntity(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            name="Charlie",
        )
        assert entity.summary is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is graph_entities."""
        assert GraphEntity.__tablename__ == "graph_entities"

    @pytest.mark.unit
    def test_no_timestamp_mixin(self) -> None:
        """GraphEntity has its own created_at/updated_at, not from TimestampMixin."""
        entity = GraphEntity(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            name="test",
        )
        assert hasattr(entity, "created_at")
        assert hasattr(entity, "updated_at")

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes name and entity_type."""
        entity = GraphEntity(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            name="Alice",
            entity_type="person",
        )
        assert "GraphEntity" in repr(entity)
        assert "Alice" in repr(entity)
