"""OpenZync HTTP middleware — request ID, logging, audit, auth, rate limiting, tracing.

All middleware classes in this package are raw ASGI middleware: each
implements ``__call__(scope, receive, send)`` directly rather than subclassing
Starlette's ``BaseHTTPMiddleware``.  They are registered in
``services/api/main.py`` via ``app.add_middleware(...)``.

Starlette middleware is LIFO — the last ``add_middleware()`` call wraps the
outermost layer and runs first.  The runtime execution order below
(outermost → innermost) is documented in the ``main.py`` registration block
(``services/api/main.py``, lines ~194-239); registration order is the reverse.

Runtime order (outermost → innermost):

1. ``MetricsMiddleware`` — records RED metrics, wrapping everything including 404s.
2. ``CORSMiddleware`` — intercepts OPTIONS preflight before auth can reject it.
3. ``LoggingMiddleware`` — logs the request/response lifecycle.
4. ``TracingMiddleware`` — manages OpenTelemetry spans for the request.
5. ``AuthMiddleware`` — extracts/validates JWT & API key, sets ``org_id`` state.
6. ``RateLimitMiddleware`` — enforces per-IP / per-org sliding-window limits.
7. ``AuditMiddleware`` — records every request to audit_logs (post-response).
8. ``GZipMiddleware`` — compresses responses >= 1 KB.
9. ``TrustedHostMiddleware`` — prevents host-header attacks.
10. ``RequestIDMiddleware`` — assigns ``request_id`` (innermost, closest to the router).
"""

from __future__ import annotations

from middleware.audit import AuditMiddleware
from middleware.auth import AuthMiddleware
from middleware.logging import LoggingMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.request_id import RequestIDMiddleware
from middleware.tracing import TracingMiddleware

__all__: list[str] = [
    "AuditMiddleware",
    "AuthMiddleware",
    "LoggingMiddleware",
    "RateLimitMiddleware",
    "RequestIDMiddleware",
    "TracingMiddleware",
]
