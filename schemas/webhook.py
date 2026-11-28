"""Pydantic schemas for webhook endpoint CRUD and event types."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl


class CreateWebhookRequest(BaseModel):
    """Request body for creating a new webhook endpoint."""

    name: str = Field(..., min_length=1, max_length=255, description="Human-readable label")
    url: HttpUrl = Field(..., description="Endpoint URL for webhook delivery (HTTPS recommended)")
    events: list[str] = Field(
        default_factory=list,
        description="List of event types to subscribe to (empty = all events)",
    )


class UpdateWebhookRequest(BaseModel):
    """Request body for updating a webhook endpoint.

    All fields are optional — only provided fields are updated.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: HttpUrl | None = None
    events: list[str] | None = None
    is_active: bool | None = None


class WebhookSecretResponse(BaseModel):
    """Response returned once after creating a webhook endpoint.

    The ``secret`` field is the endpoint's **own** raw signing secret,
    generated per endpoint and shown exactly once — the client must save
    it immediately.  It is never returned by subsequent reads.
    """

    id: UUID
    name: str
    url: str
    secret: str
    message: str = (
        "This is this endpoint's own webhook signing secret. "
        "Use it to verify HMAC-SHA256 signatures on webhooks received for "
        "this endpoint. It is shown only once — store it immediately. "
        "Rotate via the API if it is ever compromised."
    )
