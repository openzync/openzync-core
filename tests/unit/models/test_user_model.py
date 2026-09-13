"""Tests for ``User`` model."""
from __future__ import annotations

import uuid

import pytest

from models.user import User


class TestUserModel:
    """Cover User fields — external_id, name, email, metadata, role, auth, flags."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        user = User(
            organization_id=uuid.uuid4(),
            external_id="customer-abc-123",
        )
        assert user.organization_id is not None
        assert user.external_id == "customer-abc-123"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """metadata, role, is_active, is_deleted, is_email_verified, mfa_enabled have server_defaults."""
        for col_name in ["metadata", "role", "is_active", "is_deleted", "is_email_verified", "mfa_enabled"]:
            col = User.__table__.columns[col_name]
            assert col.server_default is not None, f"{col_name} missing server_default"

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """name, email, password_hash, summary, summary_updated_at, email_verified_at default to None."""
        user = User(
            organization_id=uuid.uuid4(),
            external_id="nullable-test",
        )
        assert user.name is None
        assert user.email is None
        assert user.password_hash is None
        assert user.summary is None
        assert user.summary_updated_at is None
        assert user.email_verified_at is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is users."""
        assert User.__tablename__ == "users"

    @pytest.mark.unit
    def test_unique_constraint(self) -> None:
        """UniqueConstraint on (organization_id, external_id)."""
        uq_name = "uq_user_organization_external"
        constraints = User.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert uq_name in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes org and external_id."""
        user = User(
            organization_id=uuid.uuid4(),
            external_id="customer-xyz",
        )
        assert "User" in repr(user)
        assert "customer-xyz" in repr(user)
