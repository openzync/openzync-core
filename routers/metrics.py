"""Prometheus metrics endpoint.

Exposes ``GET /metrics`` that returns the application metrics in
Prometheus text-format, scraped from the isolated ``METRICS_REGISTRY``.

This endpoint is intentionally **unauthenticated** — Prometheus scrapers
cannot carry bearer tokens.  It exposes no PII or business data, only
aggregate performance counters.

The /metrics endpoint is registered **outside** the standard /v1 prefix
so it does not interfere with versioned API routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from middleware.metrics import METRICS_REGISTRY

router = APIRouter(tags=["Metrics"])


@router.get("/metrics")
async def get_metrics() -> Response:
    """Return Prometheus metrics in text format.

    Uses the isolated application registry (``METRICS_REGISTRY``),
    which excludes default process/GC metrics to keep the payload lean.
    """
    data = generate_latest(METRICS_REGISTRY)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
