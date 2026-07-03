"""Request logging middleware and structlog processor.

Provides two components:

1. ``LoggingMiddleware`` — a raw ASGI middleware that records start time,
   processes the request, and logs a structured ``"request.completed"``
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
from starlette.types import ASGIApp, Receive, Scope, Send

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


class LoggingMiddleware:
    """Structured request/response logging middleware.

    Logs every HTTP request at INFO level after completion with:
    - ``method`` (GET, POST, ...)
    - ``path`` (URL path)
    - ``status_code`` (HTTP response status)
    - ``duration_ms`` (wall-clock time in milliseconds)
    - ``request_id`` (from :class:`RequestIDMiddleware`)
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time: float = time.monotonic()
        status_code: int = 200

        async def send_wrapper(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:  # Never catch KeyboardInterrupt/SystemExit — let them propagate
            status_code = 500
            raise
        finally:
            duration_ms: float = (time.monotonic() - start_time) * 1000
            method = scope.get("method", "UNKNOWN")
            path = scope.get("path", "/unknown")

            structlog.contextvars.bind_contextvars(
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=round(duration_ms, 1),
            )

            logger.info(
                "request.completed",
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=round(duration_ms, 1),
            )

            structlog.contextvars.clear_contextvars()
