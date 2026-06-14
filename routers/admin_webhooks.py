"""Admin router for webhook endpoint management.

All endpoints are scoped to the authenticated user's organization.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from dependencies.auth import get_dashboard_user, require_org_id
from dependencies.services import get_webhook_service
from schemas.webhook import (
    CreateWebhookRequest,
    UpdateWebhookRequest,
    WebhookSecretResponse,
)
from services.webhook_service import WebhookService

router = APIRouter(prefix="/v1/admin/webhooks", tags=["Admin Webhooks"])


# ── GET /events — list available event types (public) ──────────────────────


@router.get("/events")
async def list_event_types() -> dict:
    """Return all subscribable webhook event types, grouped by category."""
    from core.events import event_categories

    categories = event_categories()
    result: dict[str, list[dict]] = {}
    for cat, metas in categories.items():
        result[cat] = [
            {
                "type": m.type,
                "label": m.label,
                "category": m.category,
                "description": m.description,
            }
            for m in metas
        ]
    return {"data": result}


# ── GET / — list endpoints ────────────────────────────────────────────────


@router.get("")
async def list_webhooks(
    service: WebhookService = Depends(get_webhook_service),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> dict:
    """List all webhook endpoints for the authenticated organization."""
    endpoints = await service.list_endpoints(uuid.UUID(org_id))
    return {"data": endpoints}


# ── GET /{id} — get single endpoint ──────────────────────────────────────


@router.get("/{endpoint_id}")
async def get_webhook(
    endpoint_id: uuid.UUID,
    service: WebhookService = Depends(get_webhook_service),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> dict:
    """Get a single webhook endpoint by ID."""
    endpoint = await service.get_endpoint(endpoint_id, uuid.UUID(org_id))
    if not endpoint:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    return {"data": endpoint}


# ── POST / — create endpoint ─────────────────────────────────────────────


@router.post("", status_code=201)
async def create_webhook(
    body: CreateWebhookRequest,
    service: WebhookService = Depends(get_webhook_service),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> WebhookSecretResponse:
    """Create a new webhook endpoint.

    Returns the global webhook signing secret so the consumer can verify
    HMAC-SHA256 signatures.
    """
    endpoint, secret = await service.create_endpoint(
        organization_id=uuid.UUID(org_id),
        name=body.name,
        url=str(body.url),
        events=body.events if body.events else None,
    )
    return WebhookSecretResponse(
        id=uuid.UUID(endpoint["id"]),
        name=endpoint["name"],
        url=endpoint["url"],
        secret=secret,
    )


# ── PATCH /{id} — update endpoint ────────────────────────────────────────


@router.patch("/{endpoint_id}")
async def update_webhook(
    endpoint_id: uuid.UUID,
    body: UpdateWebhookRequest,
    service: WebhookService = Depends(get_webhook_service),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> dict:
    """Update a webhook endpoint's name, URL, events, or active status."""
    updates: dict = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    endpoint = await service.update_endpoint(
        endpoint_id, uuid.UUID(org_id), updates,
    )
    if not endpoint:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    return {"data": endpoint}


# ── DELETE /{id} — delete endpoint ───────────────────────────────────────


@router.delete("/{endpoint_id}", status_code=204)
async def delete_webhook(
    endpoint_id: uuid.UUID,
    service: WebhookService = Depends(get_webhook_service),
    org_id: str = Depends(require_org_id),
    _user_id: str = Depends(get_dashboard_user),
) -> None:
    """Delete a webhook endpoint."""
    deleted = await service.delete_endpoint(endpoint_id, uuid.UUID(org_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
