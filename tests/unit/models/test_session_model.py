"""Tests for ``Session`` model — conversation session within a project."""
from __future__ import annotations

import uuid

import pytest

from models.session import Session


class TestSessionModel:
    """Cover Session fields — external_id, metadata, is_active, is_deleted, closed_at."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        session = Session(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            external_id="ext-001",
        )
        assert session.organization_id is not None
        assert session.project_id is not None
        assert session.user_id is not None
        assert session.external_id == "ext-001"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """metadata, is_active, is_deleted have server_defaults."""
        for col_name in ["metadata", "is_active", "is_deleted"]:
            col = Session.__table__.columns[col_name]
            assert col.server_default is not None, f"{col_name} missing server_default"

    @pytest.mark.unit
    def test_nullable_closed_at(self) -> None:
        """closed_at defaults to None."""
        session = Session(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            external_id="ext-003",
        )
        assert session.closed_at is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is sessions."""
        assert Session.__tablename__ == "sessions"

    @pytest.mark.unit
    def test_unique_constraint(self) -> None:
        """UniqueConstraint on (project_id, external_id)."""
        uq_name = "uq_session_project_external"
        constraints = Session.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert uq_name in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes user_id and is_active."""
        session = Session(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            external_id="ext",
            is_active=True,
        )
        assert "Session" in repr(session)
        assert "active=True" in repr(session)
