"""Path-scoped ``Origin: null`` CORS exception for addon panels.

Addon panels run in ``sandbox="allow-scripts"`` iframes (opaque origin),
so their fetches send ``Origin: null`` and Starlette's ``CORSMiddleware``
explicit allowlist rejects the preflight.  This shim handles exactly that
case — ``Origin: null`` on paths under ``/v1/ext/`` only — and leaves
every other path to the explicit allowlist (fail-closed).

Safety basis: addon APIs authenticate via bearer tokens only; no cookies
are involved, so no ``Allow-Credentials`` is emitted here.  Non-goal:
never extend beyond ``/v1/ext/*`` — any wider null-origin allowance
would let arbitrary sandboxed content ride ambient authority.
"""

from __future__ import annotations

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger("OpenZync.cors")

EXT_PREFIX: str = "/v1/ext/"
EXT_CORS_ALLOW_METHODS: list[str] = [
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
]
EXT_CORS_ALLOW_HEADERS: list[str] = [
    "Authorization",
    "Content-Type",
    "X-Request-ID",
    "Idempotency-Key",
]
_EXT_CORS_MAX_AGE: str = "600"


class ExtNullOriginCorsMiddleware:
    """Allow opaque-origin panels onto ``/v1/ext/*`` and nothing else.

    Must be registered OUTSIDE (after) ``CORSMiddleware`` so its
    preflight short-circuit runs first.  Handles only requests whose
    path starts with ``/v1/ext/`` and whose ``Origin`` header is
    exactly ``null``; everything else passes through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(
            EXT_PREFIX
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if headers.get(b"origin") != b"null":
            await self.app(scope, receive, send)
            return

        if scope.get("method") == "OPTIONS" and (
            b"access-control-request-method" in headers
        ):
            logger.debug("ext.null_origin_preflight_handled")
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"access-control-allow-origin", b"null"),
                        (
                            b"access-control-allow-methods",
                            ", ".join(EXT_CORS_ALLOW_METHODS).encode(),
                        ),
                        (
                            b"access-control-allow-headers",
                            ", ".join(EXT_CORS_ALLOW_HEADERS).encode(),
                        ),
                        (b"access-control-max-age", _EXT_CORS_MAX_AGE.encode()),
                        (b"vary", b"Origin"),
                        (b"content-length", b"0"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                keys = {k.lower() for k, _ in response_headers}
                if b"access-control-allow-origin" not in keys:
                    response_headers.append(
                        (b"access-control-allow-origin", b"null")
                    )
                vary_idx = next(
                    (
                        i
                        for i, (k, _) in enumerate(response_headers)
                        if k.lower() == b"vary"
                    ),
                    None,
                )
                if vary_idx is None:
                    response_headers.append((b"vary", b"Origin"))
                elif b"origin" not in [
                    p.strip()
                    for p in response_headers[vary_idx][1].lower().split(b",")
                ]:
                    key, value = response_headers[vary_idx]
                    response_headers[vary_idx] = (
                        key,
                        value + b", Origin" if value else b"Origin",
                    )
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
