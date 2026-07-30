"""Tests for ``AuditLog`` model — immutable, append-only, with CreatedAtMixin."""
from __future__ import annotations

import uuid

import pytest

from models.audit_log import AuditLog


class TestAuditLogModel:
    """Cover AuditLog fields — action, resource_type, details, timestamps."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        log = AuditLog(
            action="session.create",
            resource_type="session",
        )
        assert log.action == "session.create"
        assert log.resource_type == "session"

    @pytest.mark.unit
    def test_default_details_configured(self) -> None:
        """details has server_default='{}'."""
        col = AuditLog.__table__.columns["details"]
        assert col.server_default is not None
        assert "{}" in str(col.server_default.arg)

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """organization_id, actor_id, actor_type, resource_id, ip_address default to None."""
        log = AuditLog(action="test.action", resource_type="test")
        assert log.organization_id is None
        assert log.actor_id is None
        assert log.actor_type is None
        assert log.resource_id is None
        assert log.ip_address is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is audit_logs."""
        assert AuditLog.__tablename__ == "audit_logs"

    @pytest.mark.unit
    def test_check_constraint_actor_type(self) -> None:
        """CheckConstraint enforces actor_type IN ('user', 'api_key', 'system')."""
        constraints = AuditLog.__table_args__
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert "ck_audit_log_actor_type" in names

    @pytest.mark.unit
    def test_uses_created_at_mixin(self) -> None:
        """AuditLog has created_at but NOT updated_at."""
        log = AuditLog(action="test", resource_type="test")
        assert hasattr(log, "created_at")
        assert not hasattr(log, "updated_at")

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes action, actor_id, actor_type."""
        log = AuditLog(
            action="test.action",
            resource_type="test",
            actor_id="user-1",
            actor_type="user",
        )
        assert "AuditLog" in repr(log)
        assert "test.action" in repr(log)
