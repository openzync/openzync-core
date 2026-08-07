"""Webhook service — manages endpoints and emits events via ARQ background jobs.

This service handles two concerns:

1. **Endpoint management** — CRUD for webhook endpoints (DB only).
2. **Event emission** — ``emit()`` fans out to all subscribed endpoints
   by enqueuing ARQ ``deliver_webhook`` jobs.  Delivery is async with
   per-endpoint HMAC-SHA256 signing, retries, and delivery logging.

Each endpoint carries its **own** signing secret, stored on the endpoint
row and returned exactly once at create/rotate time.  Signing with the
endpoint's own secret means one tenant can never forge signatures that
another tenant's consumers trust.  The legacy global
``WEBHOOK_SIGNING_SECRET`` config field is **deprecated** for signing —
kept for backward-compat reads, no longer used here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from collections.abc import Mapping

import orjson

from core.arq import get_arq
from core.config import get_settings
from models.webhook import WebhookEndpoint
from repositories.webhook_repository import WebhookRepository
from services.worker.worker_settings import get_queue_name

logger = logging.getLogger("openzync.webhooks")

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
        """Create a webhook endpoint with a fresh per-endpoint signing secret.

        The secret is generated once and returned so the consumer can
        verify HMAC-SHA256 signatures.  It is never returned again —
        subsequent reads (``list_endpoints``/``get_endpoint``/``update_endpoint``)
        do not expose it.  Rotate via :meth:`rotate_endpoint_secret`.

        Args:
            organization_id: The owning organization's UUID.
            name: Human-readable endpoint label.
            url: HTTPS endpoint URL that receives POST deliveries.
            events: Subscribed event types; empty/None subscribes to all.

        Returns:
            A tuple of ``(endpoint_dict, one_time_signing_secret)``.
        """
        signing_secret = secrets.token_urlsafe(43)
        endpoint = await self._repo.create(
            organization_id=organization_id,
            name=name,
            url=url,
            events=events,
            signing_secret=signing_secret,
        )
        # ponytail: per-org secret stored in DB; upgrade to Transit-at-rest
        # encryption with core.transit WEBHOOK_SECRET_KEY when TransitManager is wired
        return self._serialize(endpoint), signing_secret

    async def rotate_endpoint_secret(
        self,
        endpoint_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> str | None:
        """Rotate an endpoint's signing secret, returning the new value once.

        Args:
            endpoint_id: The endpoint whose secret should be rotated.
            organization_id: The owning organization (ownership check).

        Returns:
            The new signing secret, or ``None`` if the endpoint does not
            exist or belongs to a different organization.
        """
        endpoint = await self._repo.get_by_id(endpoint_id)
        if not endpoint or endpoint.organization_id != organization_id:
            return None

        new_secret = secrets.token_urlsafe(43)
        await self._repo.update(endpoint_id, signing_secret=new_secret)
        return new_secret

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

        payload = payload if payload is not None else {}
        body_bytes = orjson.dumps({"type": event_type, "payload": payload})
        body = body_bytes.decode()

        try:
            arq_pool = get_arq()
        except RuntimeError as exc:
            logger.error("Webhook emit failed — ARQ not available: %s", exc)
            raise

        queue_name = get_queue_name(get_settings().ENVIRONMENT, ARQ_WEBHOOK_QUEUE)

        async def _enqueue_one(ep: WebhookEndpoint) -> None:
            """Enqueue a single delivery, signed with the endpoint's own secret."""
            signing_secret = ep.signing_secret
            if not signing_secret:
                # Legacy row migrated with a NULL secret — backfill one now so
                # we never fall back to a shared/global secret. Single
                # conditional UPDATE (WHERE signing_secret IS NULL) so two
                # concurrent emitters cannot race and write different secrets —
                # the loser re-reads the winner's secret instead of overwriting.
                signing_secret = secrets.token_urlsafe(43)
                if await self._repo.set_signing_secret_if_null(
                    ep.id, signing_secret=signing_secret,
                ) == 0:
                    refreshed = await self._repo.get_by_id(ep.id)
                    if refreshed is None or not refreshed.signing_secret:
                        logger.warning(
                            "webhook.secret_backfill_conflict",
                            extra={"endpoint_id": str(ep.id)},
                        )
                        return  # endpoint deleted mid-emit — nothing to deliver
                    signing_secret = refreshed.signing_secret

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
    def _serialize(endpoint: WebhookEndpoint) -> dict:
        """Convert a WebhookEndpoint ORM model to a dict for API responses.

        Never includes ``signing_secret`` — the secret is shown exactly
        once at create/rotate time and is not retrievable afterwards.
        """
        if not isinstance(endpoint, WebhookEndpoint):
            raise TypeError(f"Expected WebhookEndpoint, got {type(endpoint).__name__}")
        try:
            events_list = orjson.loads(endpoint.events.encode()) if endpoint.events else []
        except (orjson.JSONDecodeError, TypeError):
            logger.error(
                "webhook.events_deserialization_failed",
                extra={"endpoint_id": str(endpoint.id)},
            )
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
