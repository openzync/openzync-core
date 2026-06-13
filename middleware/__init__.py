"""OpenZep HTTP middleware — request ID, logging, audit, auth, rate limiting, tracing.

All middleware classes in this package are Starlette ``BaseHTTPMiddleware``
subclasses.  They are registered in ``main.py`` via ``app.add_middleware(...)``.
The order of registration matters:

1. ``RequestIDMiddleware`` — earliest, captures X-Request-ID before any logic.
2. ``LoggingMiddleware`` — logs every request after completion.
3. ``AuthMiddleware`` — authenticates API keys / JWTs.
4. ``AuditMiddleware`` — records every request to audit_logs (post-response).
5. ``RateLimitMiddleware`` — enforces per-IP and per-org rate limits.
6. ``TracingMiddleware`` — OpenTelemetry span management (outermost).
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
