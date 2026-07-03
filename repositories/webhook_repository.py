"""Repository for webhook endpoint CRUD operations."""

from __future__ import annotations

import orjson
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.webhook import WebhookEndpoint


class WebhookRepository:
    """All database access for webhook endpoints."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_by_id(self, endpoint_id: uuid.UUID) -> WebhookEndpoint | None:
        """Fetch a single endpoint by ID."""
        result = await self._db.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint_id)
        )
        return result.scalar_one_or_none()

    async def get_by_organization(
        self,
        organization_id: uuid.UUID,
    ) -> list[WebhookEndpoint]:
        """Fetch all endpoints for an organization."""
        result = await self._db.execute(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.organization_id == organization_id)
            .order_by(WebhookEndpoint.created_at.desc())
        )
        return list(result.scalars().all())

    async def create(
        self,
        organization_id: uuid.UUID,
        name: str,
        url: str,
        events: list[str] | None = None,
    ) -> WebhookEndpoint:
        """Create a new webhook endpoint."""
        endpoint = WebhookEndpoint(
            organization_id=organization_id,
            name=name,
            url=url,
            events=orjson.dumps(events if events is not None else []),
        )
        self._db.add(endpoint)
        await self._db.flush()
        await self._db.refresh(endpoint)
        return endpoint

    async def update(
        self,
        endpoint_id: uuid.UUID,
        **kwargs: object,
    ) -> WebhookEndpoint | None:
        """Update fields on a webhook endpoint.

        Accepts any keyword argument matching a model column.
        The ``events`` field, if provided, must be a ``list[str]`` that
        will be JSON-serialised.
        """
        update_data: dict[str, object] = {}
        if "events" in kwargs:
            events_val = kwargs["events"]
            update_data["events"] = orjson.dumps(events_val) if isinstance(events_val, list) else events_val
        for key in ("name", "url", "is_active", "last_delivery_at"):
            if key in kwargs:
                update_data[key] = kwargs[key]

        if not update_data:
            return await self.get_by_id(endpoint_id)

        await self._db.execute(
            update(WebhookEndpoint)
            .where(WebhookEndpoint.id == endpoint_id)
            .values(**update_data)
        )
        await self._db.flush()
        return await self.get_by_id(endpoint_id)

    async def delete(self, endpoint_id: uuid.UUID) -> bool:
        """Delete a webhook endpoint. Returns True if deleted."""
        endpoint = await self.get_by_id(endpoint_id)
        if not endpoint:
            return False
        await self._db.delete(endpoint)
        await self._db.flush()
        return True

    async def get_active_endpoints_for_event(
        self,
        organization_id: uuid.UUID,
        event_type: str,
    ) -> list[WebhookEndpoint]:
        """Get all active endpoints that subscribe to a given event type."""
        result = await self._db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.organization_id == organization_id,
                WebhookEndpoint.is_active.is_(True),
            )
        )
        endpoints = result.scalars().all()
        # Filter by event subscription (JSON array stored as text)
        return [
            e for e in endpoints
            if self._endpoint_subscribes_to(e, event_type)
        ]

    def _endpoint_subscribes_to(
        self, endpoint: WebhookEndpoint, event_type: str
    ) -> bool:
        """Check if an endpoint subscribes to a given event type."""
        try:
            subscribed = orjson.loads(endpoint.events.encode())
        except (orjson.JSONDecodeError, TypeError):
            return False
        # Empty list = subscribe to all events
        if not subscribed:
            return True
        return event_type in subscribed
