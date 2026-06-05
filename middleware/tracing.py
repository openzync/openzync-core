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
- ``MG_SERVICE_NAME`` — OpenTelemetry service name.  Default: ``memgraph``.
- ``MG_ENVIRONMENT`` — Deployment environment, added as a span attribute.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

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

        service_name: str = os.getenv("MG_SERVICE_NAME", "memgraph")
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
    except Exception:
        logger.exception("Failed to initialise OpenTelemetry — tracing disabled")
        _tracer = None

    return _tracer


# ═══════════════════════════════════════════════════════════════════════════════
# Tracing middleware
# ═══════════════════════════════════════════════════════════════════════════════


class TracingMiddleware(BaseHTTPMiddleware):
    """OpenTelemetry tracing middleware.

    Creates a span for each request with HTTP semantic convention attributes.
    If OpenTelemetry is not configured (``MG_OTLP_ENDPOINT`` not set), this
    middleware is a zero-overhead pass-through.
    """

    def __init__(self, app: Any, **kwargs: Any) -> None:
        """Initialise the middleware and attempt OpenTelemetry setup.

        Args:
            app: The ASGI application.
            **kwargs: Additional arguments for ``BaseHTTPMiddleware``.
        """
        super().__init__(app, **kwargs)
        # Initialise tracer once at startup.
        global _tracer, _tracer_initialised  # noqa: PLW0603
        if not _tracer_initialised:
            _tracer = _init_tracer()

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Create an OpenTelemetry span for the request if tracing is enabled.

        Args:
            request: Incoming HTTP request.
            call_next: The next middleware or route handler.

        Returns:
            HTTP response (unchanged).
        """
        if _tracer is None:
            # OpenTelemetry not configured — pass through with zero overhead.
            return await call_next(request)

        with _tracer.start_as_current_span(
            name=f"{request.method} {request.url.path}",
            kind=1,  # SpanKind.SERVER = 1
        ) as span:
            # ── Span attributes (OpenTelemetry HTTP semantic conventions) ─
            span.set_attribute("HTTPMethod", request.method)
            span.set_attribute("HTTPUrl", str(request.url))
            span.set_attribute("HTTPTarget", request.url.path)
            span.set_attribute("HTTPHost", request.url.hostname or "")
            span.set_attribute("HTTPRequestId", getattr(request.state, "request_id", ""))

            org_id: str | None = getattr(request.state, "org_id", None)
            if org_id:
                span.set_attribute("org_id", org_id)

            try:
                response = await call_next(request)
                span.set_attribute("HTTPStatusCode", response.status_code)

                if response.status_code >= 500:
                    span.set_status(
                        1,  # StatusCode.ERROR
                        f"Server error: {response.status_code}",
                    )
                elif response.status_code >= 400:
                    span.set_status(
                        1,  # StatusCode.ERROR
                        f"Client error: {response.status_code}",
                    )

                return response
            except Exception as exc:
                span.set_attribute("HTTPStatusCode", 500)
                span.set_status(1, f"Unhandled exception: {exc}")  # StatusCode.ERROR
                raise
