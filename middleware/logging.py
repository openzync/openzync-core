"""Request logging middleware and structlog processor.

Provides two components:

1. ``LoggingMiddleware`` — a ``BaseHTTPMiddleware`` that records start time,
   calls the next handler, and logs a structured ``"request.completed"``
   message at INFO with method, path, status code, and duration.

2. ``add_request_context`` — a structlog processor that injects bound context
   (request_id, method, path, etc.) into every log entry automatically.

Usage:

    # In main.py:
    import structlog
    from middleware.logging import LoggingMiddleware, add_request_context

    app.add_middleware(LoggingMiddleware)

    # In structlog configuration:
    structlog.configure(
        processors=[
            add_request_context,
            # ... other processors
        ],
    )
"""

from __future__ import annotations

import logging
import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# structlog processor
# ═══════════════════════════════════════════════════════════════════════════════


def add_request_context(
    logger: logging.Logger,  # noqa: ARG001
    method_name: str,  # noqa: ARG001
    event_dict: dict,
) -> dict:
    """structlog processor that merges bound contextvars into every log entry.

    This processor reads context variables set by :class:`RequestIDMiddleware`
    and :class:`LoggingMiddleware` (e.g. ``request_id``) and adds them to
    every structured log message without explicit propagation.

    Register this as the **first** processor in your structlog configuration::

        structlog.configure(
            processors=[
                add_request_context,
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_log_level,
                structlog.dev.ConsoleRenderer(),
            ],
        )

    Args:
        logger: The stdlib logger instance (unused).
        method_name: The logging method name (unused).
        event_dict: The mutable event dictionary being built.

    Returns:
        The event dictionary with any bound context merged in.
    """
    # structlog.contextvars.bind_contextvars is set by middleware; this
    # processor ensures every log line picks up the bound values.
    return structlog.contextvars.merge_contextvars(logger, method_name, event_dict)  # type: ignore[no-any-return]


# ═══════════════════════════════════════════════════════════════════════════════
# Request logging middleware
# ═══════════════════════════════════════════════════════════════════════════════


class LoggingMiddleware(BaseHTTPMiddleware):
    """Structured request/response logging middleware.

    Logs every HTTP request at INFO level after completion with:
    - ``method`` (GET, POST, ...)
    - ``path`` (URL path)
    - ``status_code`` (HTTP response status)
    - ``duration_ms`` (wall-clock time in milliseconds)
    - ``request_id`` (from :class:`RequestIDMiddleware`)

    This middleware should be registered **after** ``RequestIDMiddleware`` so
    that ``request.state.request_id`` is available.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Log request completion after the response is generated.

        Args:
            request: Incoming HTTP request.
            call_next: The next middleware or route handler in the chain.

        Returns:
            HTTP response (unchanged).
        """
        start_time: float = time.monotonic()
        response = await call_next(request)
        duration_ms: float = (time.monotonic() - start_time) * 1000

        # We intentionally read these AFTER the response so that auth middleware
        # has had a chance to populate them.
        structlog.contextvars.bind_contextvars(
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 1),
        )

        logger.info(
            "request.completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 1),
        )

        structlog.contextvars.clear_contextvars()

        return response
