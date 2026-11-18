"""Tests for ``Organization`` model — tenant entity with status, config, quotas."""
from __future__ import annotations

import pytest

from models.organization import Organization


class TestOrganizationModel:
    """Cover Organization fields — name, status, config, quotas, is_active."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        org = Organization(name="Acme Corp")
        assert org.name == "Acme Corp"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """config, llm_config, quotas, is_active have server_defaults."""
        for col_name in ["config", "llm_config", "quotas", "is_active"]:
            col = Organization.__table__.columns[col_name]
            assert col.server_default is not None, f"{col_name} missing server_default"

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is organizations."""
        assert Organization.__tablename__ == "organizations"

    @pytest.mark.unit
    def test_check_constraint(self) -> None:
        """CheckConstraint enforces status IN ('pending', 'approved', 'rejected')."""
        constraints = Organization.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert "ck_organization_status" in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes name and status."""
        org = Organization(name="Acme", status="approved")
        assert "Organization" in repr(org)
        assert "Acme" in repr(org)
        assert "approved" in repr(org)
