"""Tests for ``Fact`` model."""
from __future__ import annotations

import uuid

import pytest

from models.fact import Fact


class TestFactModel:
    """Cover Fact fields — triplet, confidence, entity links, temporal validity."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        fact = Fact(
            project_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            content="Alice likes hiking",
        )
        assert fact.project_id is not None
        assert fact.user_id is not None
        assert fact.organization_id is not None
        assert fact.content == "Alice likes hiking"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """subject_type, object_type, confidence have server_defaults."""
        for col_name in ["subject_type", "object_type", "confidence"]:
            col = Fact.__table__.columns[col_name]
            assert col.server_default is not None, f"{col_name} missing server_default"

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """subject, predicate, object, entity FKs, temporal fields, embedding default to None."""
        fact = Fact(
            project_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            content="test",
        )
        assert fact.subject is None
        assert fact.predicate is None
        assert fact.object is None
        assert fact.source_episode_id is None
        assert fact.subject_entity_id is None
        assert fact.object_entity_id is None
        assert fact.valid_from is None
        assert fact.valid_to is None
        assert fact.invalid_at is None
        assert fact.embedding is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is facts."""
        assert Fact.__tablename__ == "facts"

    @pytest.mark.unit
    def test_indices(self) -> None:
        """Indices exist on user_id and (user_id, valid_from, valid_to)."""
        constraints = Fact.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert "ix_fact_user_id" in names
        assert "ix_fact_user_valid_range" in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes subject, predicate, object."""
        fact = Fact(
            project_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            content="test",
            subject="Alice",
            predicate="likes",
            object="hiking",
        )
        assert "Fact" in repr(fact)
        assert "Alice" in repr(fact)
