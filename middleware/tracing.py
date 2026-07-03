"""OpenTelemetry distributed tracing middleware.

Provides an ``TracingMiddleware`` that creates OpenTelemetry spans for every
HTTP request and attaches relevant attributes (method, URL, status code).

The middleware initialises OpenTelemetry on first use if the environment
variable ``MG_OTLP_ENDPOINT`` is set.  If OpenTelemetry is not configured,
the middleware is a no-op pass-through — no spans are created, no data is
exported.

Configuration environment variables (via ``pydantic-settings`` / ``.env``):

- ``MG_OTLP_ENDPOINT`` — OTLP gRPC endpoint (e.g. ``http://localhost:4317``).
  If unset or empty, tracing is disabled.
- ``MG_OTLP_HEADERS`` — Optional comma-separated ``key=value`` headers for
  the OTLP exporter (e.g. ``Authorization=Bearer token123``).
- ``MG_TRACE_SAMPLE_RATE`` — Sampling rate as a float between 0.0 and 1.0.
  Default: ``0.05`` (5 %).  Set to ``1.0`` for full tracing in dev/staging.
- ``MG_SERVICE_NAME`` — OpenTelemetry service name.  Default: ``openzep``.
- ``MG_ENVIRONMENT`` — Deployment environment, added as a span attribute.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from core.config import settings

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Module-level OpenTelemetry state
# ═══════════════════════════════════════════════════════════════════════════════

_tracer: Any = None
"""Module-level tracer instance.  ``None`` if OpenTelemetry is not configured."""

_tracer_initialised: bool = False
"""Whether we have attempted OpenTelemetry initialisation (to avoid retries)."""


def _init_tracer() -> Any | None:
    """Initialise the OpenTelemetry tracer provider and exporter.

    Reads configuration from environment variables / pydantic-settings.
    Returns ``None`` if ``MG_OTLP_ENDPOINT`` is not set (graceful skip).

    Returns:
        An OpenTelemetry tracer instance, or ``None``.
    """
    global _tracer, _tracer_initialised  # noqa: PLW0603

    if _tracer_initialised:
        return _tracer

    _tracer_initialised = True

    otlp_endpoint: str = os.getenv("MG_OTLP_ENDPOINT", "")
    if not otlp_endpoint:
        logger.info(
            "MG_OTLP_ENDPOINT not set — OpenTelemetry tracing is disabled."
        )
        _tracer = None
        return None

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

        service_name: str = os.getenv("MG_SERVICE_NAME", "openzep")
        sample_rate_str: str = os.getenv("MG_TRACE_SAMPLE_RATE", "0.05")

        try:
            sample_rate = float(sample_rate_str)
        except ValueError:
            logger.warning(
                "Invalid MG_TRACE_SAMPLE_RATE=%r, falling back to 0.05",
                sample_rate_str,
            )
            sample_rate = 0.05

        # Clamp to [0.0, 1.0].
        sample_rate = max(0.0, min(1.0, sample_rate))

        resource = Resource.create(
            attributes={
                "service.name": service_name,
                "deployment.environment": settings.ENVIRONMENT,
            }
        )

        provider = TracerProvider(
            resource=resource,
            sampler=TraceIdRatioBased(sample_rate),
        )

        # Build OTLP exporter.
        exporter_headers: dict[str, str] = {}
        headers_str = os.getenv("MG_OTLP_HEADERS", "")
        if headers_str:
            for pair in headers_str.split(","):
                pair = pair.strip()
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    exporter_headers[key.strip()] = value.strip()

        exporter = OTLPSpanExporter(
            endpoint=otlp_endpoint,
            headers=exporter_headers,
            timeout=10,
        )

        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)

        logger.info(
            "OpenTelemetry initialised",
            endpoint=otlp_endpoint,
            sample_rate=sample_rate,
            service_name=service_name,
        )
    except ImportError:
        logger.warning(
            "OpenTelemetry packages not installed — tracing disabled. "
            "Install with: pip install opentelemetry-api opentelemetry-sdk "
            "opentelemetry-exporter-otlp-proto-grpc"
        )
        _tracer = None
    except Exception as exc:
        logger.error("tracing.span_failed", exc_info=True)
        # Don't fail the request for tracing infrastructure failure
        _tracer = None

    return _tracer


# ═══════════════════════════════════════════════════════════════════════════════
# Tracing middleware
# ═══════════════════════════════════════════════════════════════════════════════


class TracingMiddleware:
    """OpenTelemetry tracing middleware — raw ASGI, no BaseHTTPMiddleware.

    Creates a span for each request with HTTP semantic convention attributes.
    If OpenTelemetry is not configured (``MG_OTLP_ENDPOINT`` not set), this
    middleware is a zero-overhead pass-through.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # Initialise tracer once at startup.
        global _tracer, _tracer_initialised  # noqa: PLW0603
        if not _tracer_initialised:
            _tracer = _init_tracer()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if _tracer is None:
            # OpenTelemetry not configured — pass through with zero overhead.
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "UNKNOWN")
        path = scope.get("path", "/unknown")

        status_code: int = 200

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
            await send(message)

        with _tracer.start_as_current_span(
            name=f"{method} {path}",
            kind=1,  # SpanKind.SERVER = 1
        ) as span:
            # ── Span attributes (OpenTelemetry HTTP semantic conventions) ─
            span.set_attribute("HTTPMethod", method)
            span.set_attribute("HTTPTarget", path)

            # Extract host from scope headers
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode()
            span.set_attribute("HTTPHost", host)

            request_id = (scope.get("state") or {}).get("request_id", "")
            span.set_attribute("HTTPRequestId", str(request_id))

            org_id = (scope.get("state") or {}).get("org_id", None)
            if org_id:
                span.set_attribute("org_id", str(org_id))

            try:
                await self.app(scope, receive, send_wrapper)
                span.set_attribute("HTTPStatusCode", status_code)

                if status_code >= 500:
                    span.set_status(
                        1,  # StatusCode.ERROR
                        f"Server error: {status_code}",
                    )
                elif status_code >= 400:
                    span.set_status(
                        1,  # StatusCode.ERROR
                        f"Client error: {status_code}",
                    )
            except Exception as exc:
                span.set_attribute("HTTPStatusCode", 500)
                span.set_status(1, f"Unhandled exception: {exc}")  # StatusCode.ERROR
                raise
