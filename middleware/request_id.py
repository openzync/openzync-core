"""Request ID middleware — ensures every request has a traceable X-Request-ID.

The middleware:
1. Reads ``X-Request-ID`` from the incoming request headers, or generates a
   UUID if absent.
2. Stores it on ``request.state.request_id`` for use by downstream code.
3. Binds it to structlog contextvars so all log entries in the request lifecycle
   include the request ID.
4. Adds ``X-Request-ID`` to the response headers for client-side tracing.

Usage in ``main.py``:

    app.add_middleware(RequestIDMiddleware)  # must be first middleware
"""

from __future__ import annotations

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add or propagate ``X-Request-ID`` across the request lifecycle.

    This middleware **must** be registered first in the middleware stack so
    that every downstream layer (logging, auth, rate-limiting) has access to
    the request ID via ``request.state.request_id`` or structlog context.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Extract or generate request ID, bind to context, and set response header.

        Args:
            request: Incoming HTTP request.
            call_next: The next middleware or route handler in the chain.

        Returns:
            HTTP response with ``X-Request-ID`` header set.
        """
        request_id: str = request.headers.get(
            "X-Request-ID",
            str(uuid.uuid4()),
        )
        request.state.request_id = request_id

        # Bind to structlog context so all downstream logs include this ID.
        structlog.contextvars.bind_contextvars(request_id=request_id)

        try:
            response = await call_next(request)
        except BaseException:
            # Ensure response still gets the header even on middleware failure.
            response = Response(status_code=500)
            response.headers["X-Request-ID"] = request_id
            raise

        response.headers["X-Request-ID"] = request_id
        return response
