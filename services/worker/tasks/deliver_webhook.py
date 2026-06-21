"""ARQ task for delivering webhooks to external endpoints.

Enqueued by ``WebhookService.emit()`` for each subscribed endpoint.
Runs on the low-priority queue — webhook delivery must never block
real-time ingestion tasks.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from arq import Retry

from core.config import settings
from core.db import get_async_session, init_db_engine
from models.webhook import WebhookDeliveryLog

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
"""Maximum number of delivery attempts before giving up."""

HTTP_TIMEOUT = 15.0
"""Seconds to wait for a consumer to respond with a 2xx status."""


async def deliver_webhook(
    ctx: dict[str, Any],
    *,
    endpoint_id: str,
    endpoint_url: str,
    body: str,
    event_type: str,
    signature: str,
    attempt: int = 0,
) -> None:
    """Deliver a signed webhook payload to an external endpoint.

    Retries up to ``MAX_ATTEMPTS`` with exponential backoff on 5xx or
    network errors.  4xx responses are **not** retried (the consumer is
    rejecting the payload, retrying won't help).

    Args:
        ctx: ARQ worker context.
        endpoint_id: Webhook endpoint UUID string.
        endpoint_url: Target URL for the POST request.
        body: Raw JSON payload string.
        event_type: Event type string (e.g. ``session.created``).
        signature: ``X-Webhook-Signature`` header value (pre-signed).
        attempt: Zero-indexed attempt counter (passed explicitly since
            ARQ does not expose a built-in retry count).
    """
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
        "X-Webhook-Attempt": str(attempt),
        "X-Webhook-Event": event_type,
        "User-Agent": "OpenZep-Webhook/1.0",
    }

    status_code: int | None = None
    error: str | None = None
    success = False

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                endpoint_url,
                content=body,
                headers=headers,
            )
            status_code = resp.status_code
            success = 200 <= status_code < 300

            if not success:
                if 400 <= status_code < 500:
                    # Client error — retrying won't help
                    error = f"HTTP {status_code}: {resp.text[:200]}"
                    logger.warning(
                        "Webhook rejected by consumer (attempt %d): "
                        "%s → %s %s",
                        attempt,
                        endpoint_url,
                        status_code,
                        resp.text[:100],
                    )
                elif status_code >= 500 and attempt < MAX_ATTEMPTS:
                    # Server error — retry with backoff
                    error = f"HTTP {status_code}"
                    logger.warning(
                        "Webhook server error (attempt %d): %s → %s",
                        attempt,
                        endpoint_url,
                        status_code,
                    )
                    raise Retry(defer=2**attempt)
                else:
                    error = f"HTTP {status_code} (final)"

    except httpx.TimeoutException as exc:
        error = f"Timeout: {exc}"
        logger.warning("Webhook timeout (attempt %d): %s", attempt, endpoint_url)
        if attempt < MAX_ATTEMPTS:
            raise Retry(defer=2**attempt)

    except httpx.RequestError as exc:
        error = f"Network error: {exc}"
        logger.warning("Webhook network error (attempt %d): %s", attempt, exc)
        if attempt < MAX_ATTEMPTS:
            raise Retry(defer=2**attempt)

    finally:
        await _log_delivery(
            endpoint_id=endpoint_id,
            event_type=event_type,
            attempt=attempt,
            status_code=status_code,
            success=success,
            error=error,
        )


async def _log_delivery(
    endpoint_id: str,
    event_type: str,
    attempt: int,
    status_code: int | None,
    success: bool,
    error: str | None,
) -> None:
    """Persist a delivery log entry.

    Creates its own short-lived DB session so the task is self-contained
    and survives worker restarts.
    """
    _engine = init_db_engine(
        str(settings.DATABASE_URL),
        pool_size=2,
        max_overflow=2,
    )
    _session_factory = get_async_session(_engine)

    try:
        async with _session_factory() as session:
            session.add(WebhookDeliveryLog(
                endpoint_id=uuid.UUID(endpoint_id),
                event_type=event_type,
                attempt=attempt,
                status_code=status_code,
                success=success,
                error=error,
            ))
            await session.commit()
    except Exception:
        logger.exception("Failed to persist webhook delivery log")
    finally:
        await _engine.dispose()
