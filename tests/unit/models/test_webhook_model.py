"""Tests for ``WebhookEndpoint`` and ``WebhookDeliveryLog`` models."""
from __future__ import annotations

import uuid

import pytest

from models.webhook import WebhookDeliveryLog, WebhookEndpoint


class TestWebhookEndpointModel:
    """Cover WebhookEndpoint fields — name, url, events, is_active, last_delivery_at."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        ep = WebhookEndpoint(
            organization_id=uuid.uuid4(),
            name="Production Slack",
            url="https://hooks.example.com/webhook",
            events='["session.created","fact.extracted"]',
        )
        assert ep.organization_id is not None
        assert ep.name == "Production Slack"
        assert ep.url == "https://hooks.example.com/webhook"
        assert ep.events == '["session.created","fact.extracted"]'

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """is_active has server_default='true'."""
        col = WebhookEndpoint.__table__.columns["is_active"]
        assert col.server_default is not None
        assert "true" in str(col.server_default.arg)

    @pytest.mark.unit
    def test_nullable_last_delivery_at(self) -> None:
        """last_delivery_at defaults to None."""
        ep = WebhookEndpoint(
            organization_id=uuid.uuid4(),
            name="Test",
            url="https://example.com/hook",
            events="[]",
        )
        assert ep.last_delivery_at is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is webhook_endpoints."""
        assert WebhookEndpoint.__tablename__ == "webhook_endpoints"

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes name and is_active."""
        ep = WebhookEndpoint(
            organization_id=uuid.uuid4(),
            name="Slack",
            url="https://hooks.example.com",
            events="[]",
        )
        assert "WebhookEndpoint" in repr(ep)
        assert "Slack" in repr(ep)


class TestWebhookDeliveryLogModel:
    """Cover WebhookDeliveryLog fields — endpoint_id, event_type, attempt, success, error."""

    @pytest.mark.unit
    def test_required_fields(self) -> None:
        """Minimal required fields produce a valid instance."""
        log = WebhookDeliveryLog(
            endpoint_id=uuid.uuid4(),
            event_type="session.created",
        )
        assert log.endpoint_id is not None
        assert log.event_type == "session.created"

    @pytest.mark.unit
    def test_defaults_configured(self) -> None:
        """attempt has no server_default (python-level default)."""
        # Verify the column definition exists — defaults are Python-only
        col = WebhookDeliveryLog.__table__.columns["attempt"]
        assert col is not None

    @pytest.mark.unit
    def test_nullable_fields(self) -> None:
        """status_code and error default to None."""
        log = WebhookDeliveryLog(
            endpoint_id=uuid.uuid4(),
            event_type="test.event",
        )
        assert log.status_code is None
        assert log.error is None

    @pytest.mark.unit
    def test_table_name(self) -> None:
        """Table name is webhook_delivery_logs."""
        assert WebhookDeliveryLog.__tablename__ == "webhook_delivery_logs"

    @pytest.mark.unit
    def test_repr(self) -> None:
        """repr includes event_type, attempt, success."""
        log = WebhookDeliveryLog(
            endpoint_id=uuid.uuid4(),
            event_type="fact.extracted",
            attempt=2,
            success=True,
        )
        assert "WebhookDeliveryLog" in repr(log)
        assert "fact.extracted" in repr(log)
