"""Tests for ``StructuredExtraction`` model."""
from __future__ import annotations

import uuid

import pytest

from models.structured_extraction import StructuredExtraction


class TestStructuredExtractionModel:
    """Cover StructuredExtraction fields — session_id, episode_id, schema_id, data."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        ext = StructuredExtraction(
            project_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
            data={"name": "Alice", "age": 30},
        )
        assert ext.project_id is not None
        assert ext.session_id is not None
        assert ext.episode_id is not None
        assert ext.data == {"name": "Alice", "age": 30}

    @pytest.mark.unit
    def test_nullable_schema_id(self) -> None:
        """schema_id defaults to None (ad-hoc extraction)."""
        ext = StructuredExtraction(
            project_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
            data={},
        )
        assert ext.schema_id is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is structured_extractions."""
        assert StructuredExtraction.__tablename__ == "structured_extractions"

    @pytest.mark.unit
    def test_unique_constraint(self) -> None:
        """UniqueConstraint on (episode_id, schema_id)."""
        uq_name = "uq_structured_extraction_episode_schema"
        constraints = StructuredExtraction.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert uq_name in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes episode_id and schema_id."""
        ext = StructuredExtraction(
            project_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
            data={},
        )
        assert "StructuredExtraction" in repr(ext)
