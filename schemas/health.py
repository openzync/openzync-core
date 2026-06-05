"""Pydantic schemas for health-check endpoints.

Corresponds to the ``/v1/health`` and ``/v1/ready`` routes defined in
``routers/health.py``.
"""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response body for the liveness probe (``GET /v1/health``).

    Attributes:
        status: Always ``"ok"`` when the service process is alive.
        service: Service name identifier (``"memgraph-api"``).
    """

    status: str
    service: str = "memgraph-api"


class ReadinessResponse(BaseModel):
    """Response body for the readiness probe (``GET /v1/ready``).

    Attributes:
        status: ``"ok"`` when all checks pass, ``"degraded"`` otherwise.
        checks: Per-dependency health result (``True`` / ``False``).
    """

    status: str
    checks: dict[str, bool]
