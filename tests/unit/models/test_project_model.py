"""Tests for ``Project`` model."""
from __future__ import annotations

import uuid

import pytest

from models.project import Project


class TestProjectModel:
    """Cover Project fields — name, description, metadata, is_archived, created_by."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        project = Project(
            organization_id=uuid.uuid4(),
            name="My Project",
        )
        assert project.organization_id is not None
        assert project.name == "My Project"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """metadata and is_archived have server_defaults."""
        col_meta = Project.__table__.columns["metadata"]
        assert col_meta.server_default is not None
        col_archived = Project.__table__.columns["is_archived"]
        assert col_archived.server_default is not None

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """description and created_by default to None."""
        project = Project(
            organization_id=uuid.uuid4(),
            name="Nullables",
        )
        assert project.description is None
        assert project.created_by is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is projects."""
        assert Project.__tablename__ == "projects"

    @pytest.mark.unit
    def test_unique_constraint(self) -> None:
        """UniqueConstraint on (organization_id, name)."""
        uq_name = "uq_projects_org_name"
        constraints = Project.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert uq_name in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes org, name, is_archived."""
        project = Project(
            organization_id=uuid.uuid4(),
            name="My Project",
            is_archived=True,
        )
        assert "Project" in repr(project)
        assert "My Project" in repr(project)
