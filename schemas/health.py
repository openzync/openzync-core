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
        service: Service name identifier (``"openzync-api"``).
        version: Application version from ``importlib.metadata``.
    """

    status: str
    service: str = "openzync-api"
    version: str
