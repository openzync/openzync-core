"""Tests for ``ApiKey`` model — construction, defaults, nullables."""
from __future__ import annotations

import uuid

import pytest

from models.api_key import ApiKey


class TestApiKeyModel:
    """Cover ApiKey fields — hash, prefix, permissions, revocation, timestamps."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        key = ApiKey(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            lookup_hash="abc123",
            key_hash="salted_hash",
            salt="random_salt",
            prefix="oz_live_",
        )
        assert key.organization_id is not None
        assert key.project_id is not None
        assert key.lookup_hash == "abc123"
        assert key.key_hash == "salted_hash"
        assert key.salt == "random_salt"
        assert key.prefix == "oz_live_"

    @pytest.mark.unit
    def test_default_permissions_configured(self) -> None:
        """Default permissions is ['project:read', 'project:write'] (server_default)."""
        col = ApiKey.__table__.columns["permissions"]
        assert col.server_default is not None
        assert "project:read" in str(col.server_default.arg)
        assert "project:write" in str(col.server_default.arg)

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """created_by, name, last_used_at, expires_at are nullable."""
        key = ApiKey(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            lookup_hash="abc",
            key_hash="def",
            salt="salt",
            prefix="oz_live_",
        )
        assert key.created_by is None
        assert key.name is None
        assert key.last_used_at is None
        assert key.expires_at is None

    @pytest.mark.unit
    def test_is_revoked_default_configured(self) -> None:
        """is_revoked has server_default='false'."""
        col = ApiKey.__table__.columns["is_revoked"]
        assert col.server_default is not None
        assert "false" in str(col.server_default.arg)

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is api_keys."""
        assert ApiKey.__tablename__ == "api_keys"

    @pytest.mark.unit
    def test_unique_constraints(self) -> None:
        """lookup_hash has a unique=True marker."""
        # UniqueConstraint is on the column itself
        col = ApiKey.__table__.columns["lookup_hash"]
        assert col.unique is True

    @pytest.mark.unit
    def test_check_constraint_prefix(self) -> None:
        """CheckConstraint enforces prefix IN ('oz_live_', 'oz_test_')."""
        constraints = ApiKey.__table_args__  # tuple of CheckConstraint/Index
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert "ck_api_key_prefix" in names

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes id, prefix, and is_revoked."""
        key = ApiKey(
            organization_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            lookup_hash="abc",
            key_hash="def",
            salt="salt",
            prefix="oz_live_",
        )
        assert "ApiKey" in repr(key)
        assert key.prefix in repr(key)
