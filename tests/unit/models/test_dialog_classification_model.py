"""Tests for ``DialogClassification`` model."""
from __future__ import annotations

import uuid

import pytest

from models.dialog_classification import DialogClassification


class TestDialogClassificationModel:
    """Cover DialogClassification fields — labels, confidence, raw output."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        dc = DialogClassification(
            project_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
        )
        assert dc.project_id is not None
        assert dc.organization_id is not None
        assert dc.episode_id is not None

    @pytest.mark.unit
    def test_default_confidence_configured(self) -> None:
        """confidence has server_default='0.0'."""
        col = DialogClassification.__table__.columns["confidence"]
        assert col.server_default is not None
        assert "0.0" in str(col.server_default.arg)

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """intent, emotion, valence, arousal, raw default to None."""
        dc = DialogClassification(
            project_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
        )
        assert dc.intent is None
        assert dc.emotion is None
        assert dc.valence is None
        assert dc.arousal is None
        assert dc.raw is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is dialog_classifications."""
        assert DialogClassification.__tablename__ == "dialog_classifications"

    @pytest.mark.unit
    def test_unique_constraint(self) -> None:
        """UniqueConstraint on (organization_id, episode_id)."""
        uq_name = "uq_dialog_classifications_org_episode"
        constraints = DialogClassification.__table_args__
        uq_names = {c.name for c in constraints if hasattr(c, "name")}
        assert uq_name in uq_names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes episode_id, intent, emotion."""
        dc = DialogClassification(
            project_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            episode_id=uuid.uuid4(),
            intent="greeting",
            emotion="joy",
        )
        assert "DialogClassification" in repr(dc)
        assert "greeting" in repr(dc)
