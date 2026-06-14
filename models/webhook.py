"""Webhook endpoint model — per-organization webhook configuration.

Each row represents a webhook endpoint that receives POST requests for
subscribed event types.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin


class WebhookEndpoint(TimestampMixin, Base):
    """A webhook endpoint configured by an organization.

    Attributes:
        id: UUID primary key.
        organization_id: Foreign key to the owning organization.
        name: Human-readable label (e.g. "Production Slack").
        url: HTTPS endpoint URL that receives POST requests.
        events: JSON array of subscribed event type strings.
        is_active: Whether this endpoint is currently accepting deliveries.
        last_delivery_at: Timestamp of the most recent delivery attempt.
    """

    __tablename__ = "webhook_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    events: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment='JSON array of subscribed event types, e.g. ["session.created","fact.extracted"]',
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    last_delivery_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index("ix_webhook_endpoints_org", "organization_id"),
    )

    def __repr__(self) -> str:
        return f"<WebhookEndpoint id={self.id} name={self.name!r} active={self.is_active}>"


class WebhookDeliveryLog(TimestampMixin, Base):
    """Log of a webhook delivery attempt.

    Every attempt (including retries) creates a row for observability.
    """

    __tablename__ = "webhook_delivery_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(nullable=False, default=0)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<WebhookDeliveryLog id={self.id} event={self.event_type!r} "
            f"attempt={self.attempt} success={self.success}>"
        )
