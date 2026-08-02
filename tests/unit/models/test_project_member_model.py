"""Tests for ``ProjectMember`` model."""
from __future__ import annotations

import uuid

import pytest

from models.project_member import ProjectMember


class TestProjectMemberModel:
    """Cover ProjectMember fields — project_id, user_id, role."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        pm = ProjectMember(
            project_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
        assert pm.project_id is not None
        assert pm.user_id is not None

    @pytest.mark.unit
    def test_default_role_configured(self) -> None:
        """role has server_default='member'."""
        col = ProjectMember.__table__.columns["role"]
        assert col.server_default is not None
        assert "member" in str(col.server_default.arg)

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is project_members."""
        assert ProjectMember.__tablename__ == "project_members"

    @pytest.mark.unit
    def test_unique_constraint(self) -> None:
        """UniqueConstraint on (project_id, user_id)."""
        uq_name = "uq_project_members_project_user"
        constraints = ProjectMember.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert uq_name in names

    @pytest.mark.unit
    def test_check_constraint(self) -> None:
        """CheckConstraint enforces role IN ('owner', 'member')."""
        constraints = ProjectMember.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert "ck_project_members_role" in names

    @pytest.mark.unit
    def test_indices(self) -> None:
        """Indices exist on user_id and project_id."""
        constraints = ProjectMember.__table_args__
        index_names = {
            c.name for c in constraints if hasattr(c, "name") and not c.name.startswith("ck_") and not c.name.startswith("uq_")
        }
        assert "ix_project_members_user_id" in index_names
        assert "ix_project_members_project_id" in index_names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes project_id, user_id, role."""
        pm = ProjectMember(
            project_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            role="owner",
        )
        assert "ProjectMember" in repr(pm)
        assert "owner" in repr(pm)
