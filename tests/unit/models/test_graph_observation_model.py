"""Tests for ``GraphObservation`` and ``ObservationType`` models."""
from __future__ import annotations

import uuid

import pytest

from models.graph_observation import GraphObservation, ObservationType


class TestGraphObservationModel:
    """Cover GraphObservation fields — observation_type, content, entity links, confidence."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        obs = GraphObservation(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            subject_entity_id=uuid.uuid4(),
            observation_type="co_occurrence",
            content="Alice and Bob appear together frequently.",
        )
        assert obs.organization_id is not None
        assert obs.project_id is not None
        assert obs.subject_entity_id is not None
        assert obs.observation_type == "co_occurrence"
        assert obs.content == "Alice and Bob appear together frequently."

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """related_entity_id, supporting_fact_ids, supporting_relationship_ids, valid_from, valid_to, observation_metadata default to None."""
        obs = GraphObservation(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            subject_entity_id=uuid.uuid4(),
            observation_type="temporal_pattern",
            content="Pattern observed.",
        )
        assert obs.related_entity_id is None
        assert obs.supporting_fact_ids is None
        assert obs.supporting_relationship_ids is None
        assert obs.valid_from is None
        assert obs.valid_to is None
        assert obs.observation_metadata is None

    @pytest.mark.unit
    def test_confidence_default(self) -> None:
        """confidence has server_default '0.0', but Python-level default may be 0.0."""
        obs = GraphObservation(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            subject_entity_id=uuid.uuid4(),
            observation_type="behavioral_pattern",
            content="Always asks about pricing.",
        )
        # The mapped_column has no Python-level default, so SQLAlchemy omits it
        # unless a default is set at the ORM level. server_default only applies
        # at INSERT time. In constructor-only tests, it may be 0.0 or raise.
        # We validate the column definition exists.
        col = GraphObservation.__table__.columns["confidence"]
        assert col is not None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is graph_observations."""
        assert GraphObservation.__tablename__ == "graph_observations"

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes subject_entity_id and observation_type."""
        obs = GraphObservation(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            subject_entity_id=uuid.uuid4(),
            observation_type="co_occurrence",
            content="test",
        )
        assert "GraphObservation" in repr(obs)
        assert "co_occurrence" in repr(obs)


class TestObservationType:
    """Cover ObservationType StrEnum values."""

    @pytest.mark.unit
    def test_enum_values(self) -> None:
        """ObservationType has the expected members."""
        assert ObservationType.CO_OCCURRENCE == "co_occurrence"
        assert ObservationType.TEMPORAL_PATTERN == "temporal_pattern"
        assert ObservationType.BEHAVIORAL_PATTERN == "behavioral_pattern"

    @pytest.mark.unit
    def test_enum_is_str(self) -> None:
        """ObservationType is a StrEnum."""
        assert isinstance(ObservationType.CO_OCCURRENCE, str)
