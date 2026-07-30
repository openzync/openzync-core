"""Tests for ``Organization`` model — tenant entity with plan, config, quotas."""
from __future__ import annotations

import uuid

import pytest

from models.organization import Organization


class TestOrganizationModel:
    """Cover Organization fields — name, plan, config, quotas, is_active."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        org = Organization(name="Acme Corp")
        assert org.name == "Acme Corp"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """plan, config, llm_config, quotas, is_active have server_defaults."""
        for col_name in ["plan", "config", "llm_config", "quotas", "is_active"]:
            col = Organization.__table__.columns[col_name]
            assert col.server_default is not None, f"{col_name} missing server_default"

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is organizations."""
        assert Organization.__tablename__ == "organizations"

    @pytest.mark.unit
    def test_check_constraint(self) -> None:
        """CheckConstraint enforces plan IN ('free', 'pro', 'enterprise')."""
        constraints = Organization.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert "ck_organization_plan" in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes name and plan."""
        org = Organization(name="Acme", plan="pro")
        assert "Organization" in repr(org)
        assert "Acme" in repr(org)
        assert "pro" in repr(org)
