"""Request ID middleware — ensures every request has a traceable X-Request-ID.

The middleware:
1. Reads ``X-Request-ID`` from the incoming request headers, or generates a
   UUID if absent.
2. Stores it on ``scope["state"]["request_id"]`` for use by downstream code.
3. Binds it to structlog contextvars so all log entries in the request lifecycle
   include the request ID.
4. Adds ``X-Request-ID`` to the response headers for client-side tracing.

Usage in ``main.py``:

    app.add_middleware(RequestIDMiddleware)  # must be innermost middleware
"""

from __future__ import annotations

import uuid

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestIDMiddleware:
    """Raw ASGI middleware that adds or propagates ``X-Request-ID``.

    Registered innermost (closest to the router) so every downstream layer
    (logging, auth, rate-limiting) has access to the request ID via
    ``scope["state"]["request_id"]``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # ── Resolve or generate request ID ────────────────────────────────
        headers = dict(scope.get("headers") or [])
        raw_id = headers.get(b"x-request-id", b"").decode()
        request_id: str = raw_id if raw_id else str(uuid.uuid4())

        # ── Store on scope state ──────────────────────────────────────────
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # ── Wrap send to inject X-Request-ID response header ──────────────
        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                headers_list.append(
                    (b"X-Request-ID", request_id.encode())
                )
                message["headers"] = headers_list
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:  # Never catch KeyboardInterrupt/SystemExit — let them propagate
            raise
        finally:
            structlog.contextvars.clear_contextvars()
