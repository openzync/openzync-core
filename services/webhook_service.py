"""Webhook service — manages endpoints and emits events via ARQ background jobs.

This service handles two concerns:

1. **Endpoint management** — CRUD for webhook endpoints (DB only).
2. **Event emission** — ``emit()`` fans out to all subscribed endpoints
   by enqueuing ARQ ``deliver_webhook`` jobs.  Delivery is async with
   HMAC-SHA256 signing, retries, and delivery logging.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Mapping

from core.arq import get_arq
from core.config import settings
from repositories.webhook_repository import WebhookRepository

logger = logging.getLogger("openzep.webhooks")

ARQ_WEBHOOK_QUEUE = "low"
"""Webhook delivery runs on the low-priority queue so it never blocks
real-time ingestion tasks (classify, embed, extract)."""


def sign_payload(secret: str, payload: bytes) -> str:
    """Return a Svix-compatible HMAC-SHA256 signature header value.

    Format: ``t=<unix_timestamp>,v1=<hex_signature>``

    Consumers verify by recomputing::

        HMAC-SHA256("<timestamp>.<payload_body>")

    Args:
        secret: The shared signing secret (``whsec_``-prefixed).
        payload: The raw JSON body to sign.

    Returns:
        A signature string suitable for the ``X-Webhook-Signature`` header.
    """
    timestamp = str(int(time.time()))
    to_sign = f"{timestamp}.{payload.decode()}".encode()
    signature = hmac.new(secret.encode(), to_sign, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


class WebhookService:
    """Manages webhook endpoints and emits events via ARQ jobs.

    Args:
        repo: The webhook repository for endpoint CRUD.
    """

    def __init__(self, repo: WebhookRepository) -> None:
        self._repo = repo

    # ── Endpoint management ─────────────────────────────────────────────────

    async def list_endpoints(
        self, organization_id: uuid.UUID,
    ) -> list[dict]:
        """List all webhook endpoints for an organization."""
        endpoints = await self._repo.get_by_organization(organization_id)
        return [self._serialize(e) for e in endpoints]

    async def get_endpoint(
        self, endpoint_id: uuid.UUID, organization_id: uuid.UUID,
    ) -> dict | None:
        """Get a single webhook endpoint by ID, verifying ownership."""
        endpoint = await self._repo.get_by_id(endpoint_id)
        if not endpoint or endpoint.organization_id != organization_id:
            return None
        return self._serialize(endpoint)

    async def create_endpoint(
        self,
        organization_id: uuid.UUID,
        name: str,
        url: str,
        events: list[str] | None = None,
    ) -> tuple[dict, str]:
        """Create a webhook endpoint.

        Returns a tuple of ``(endpoint_dict, global_signing_secret)``.
        The global ``WEBHOOK_SIGNING_SECRET`` is returned so the consumer
        can verify HMAC-SHA256 signatures.  All endpoints share the same
        secret — rotate via environment variable to cycle all consumers.
        """
        endpoint = await self._repo.create(
            organization_id=organization_id,
            name=name,
            url=url,
            events=events,
        )

        return self._serialize(endpoint), settings.WEBHOOK_SIGNING_SECRET

    async def update_endpoint(
        self,
        endpoint_id: uuid.UUID,
        organization_id: uuid.UUID,
        updates: Mapping[str, object],
    ) -> dict | None:
        """Update a webhook endpoint. Returns updated endpoint or None."""
        endpoint = await self._repo.get_by_id(endpoint_id)
        if not endpoint or endpoint.organization_id != organization_id:
            return None

        updated = await self._repo.update(endpoint_id, **dict(updates))
        return self._serialize(updated) if updated else None

    async def toggle_endpoint(
        self,
        endpoint_id: uuid.UUID,
        organization_id: uuid.UUID,
        is_active: bool,
    ) -> dict | None:
        """Enable or disable a webhook endpoint."""
        endpoint = await self._repo.get_by_id(endpoint_id)
        if not endpoint or endpoint.organization_id != organization_id:
            return None

        updated = await self._repo.update(
            endpoint_id, is_active=is_active,
        )
        return self._serialize(updated) if updated else None

    async def delete_endpoint(
        self,
        endpoint_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> bool:
        """Delete a webhook endpoint. Returns True if deleted."""
        endpoint = await self._repo.get_by_id(endpoint_id)
        if not endpoint or endpoint.organization_id != organization_id:
            return False

        return await self._repo.delete(endpoint_id)

    # ── Event emission ─────────────────────────────────────────────────────

    async def emit(
        self,
        organization_id: uuid.UUID,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        """Emit an event to all subscribed webhook endpoints via ARQ.

        Finds active endpoints subscribed to ``event_type`` and enqueues a
        ``deliver_webhook`` job for each.  Delivery is async — errors are
        logged but never propagated to the caller (the event has already
        happened).

        Args:
            organization_id: The organization emitting the event.
            event_type: The event type string (e.g. ``session.created``).
            payload: Optional event payload dict.
        """
        endpoints = await self._repo.get_active_endpoints_for_event(
            organization_id, event_type,
        )
        if not endpoints:
            return

        payload = payload or {}
        body = json.dumps({"type": event_type, "payload": payload})
        signing_secret = settings.WEBHOOK_SIGNING_SECRET

        try:
            arq_pool = get_arq()
        except RuntimeError as exc:
            logger.error("Webhook emit failed — ARQ not available: %s", exc)
            return

        queue_name = _arq_queue_name("low")
        body_bytes = body.encode()

        async def _enqueue_one(ep: object) -> None:
            """Enqueue a single webhook delivery."""
            from models.webhook import WebhookEndpoint as WE

            signature = sign_payload(signing_secret, body_bytes)
            await arq_pool.enqueue(
                "deliver_webhook",
                queue_name=queue_name,
                endpoint_id=str(ep.id),
                endpoint_url=ep.url,
                body=body,
                event_type=event_type,
                signature=signature,
                attempt=0,
            )

        await asyncio.gather(*[_enqueue_one(ep) for ep in endpoints])

        logger.info(
            "Webhook emit: %s → %d endpoint(s)",
            event_type,
            len(endpoints),
        )

    # ── Serialization ────────────────────────────────────────────────────────

    @staticmethod
    def _serialize(endpoint: object) -> dict:
        """Convert a WebhookEndpoint ORM model to a dict for API responses."""
        from models.webhook import WebhookEndpoint as WE

        if not isinstance(endpoint, WE):
            return {}
        try:
            events_list = json.loads(endpoint.events) if endpoint.events else []
        except (json.JSONDecodeError, TypeError):
            events_list = []

        return {
            "id": str(endpoint.id),
            "organization_id": str(endpoint.organization_id),
            "name": endpoint.name,
            "url": endpoint.url,
            "events": events_list,
            "is_active": endpoint.is_active,
            "last_delivery_at": (
                endpoint.last_delivery_at.isoformat()
                if endpoint.last_delivery_at
                else None
            ),
            "created_at": endpoint.created_at.isoformat() if endpoint.created_at else None,
            "updated_at": endpoint.updated_at.isoformat() if endpoint.updated_at else None,
        }


def _arq_queue_name(queue_type: str) -> str:
    """Build the fully qualified ARQ queue name.

    Args:
        queue_type: Queue type (``"high"`` or ``"low"``).

    Returns:
        Fully qualified queue name for the current environment.
    """
    env = settings.ENVIRONMENT if hasattr(settings, "ENVIRONMENT") else "development"
    return f"OpenZep:{env}:queue:{queue_type}"
