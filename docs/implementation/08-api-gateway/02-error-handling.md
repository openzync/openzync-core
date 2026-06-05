# Error Handling Implementation Guide

> **Phase:** Phase 0 — Foundation (Week 1–2)
> **Priority:** P0
> **Requirements:** (Cross-cutting — all endpoints), SEC-05, SEC-09
> **Handoff from:** Architect (ADR-002: Error Handling & RFC 7807)
> **SRS Reference:** §8.4 Error Response Format, §12 Security Requirements

---

## 1. Overview

OpenZep uses **RFC 7807 Problem Details** (`application/problem+json`) as the standard error response format across all API endpoints. This provides a consistent, machine-readable error structure that clients can parse programmatically.

All errors flow through a single global exception handler that:
1. Catches known `AppError` subclasses → maps to appropriate HTTP status code
2. Catches Pydantic `ValidationError` → returns 422 with field-level detail
3. Catches unknown exceptions → logs full traceback, returns generic 500 (no stack trace leaked)

### 1.1 Response Format

```json
{
    "type": "https://api.OpenZep.dev/errors/resource_not_found",
    "title": "Resource Not Found",
    "status": 404,
    "detail": "User '550e8400-e29b-41d4-a716-446655440000' not found in organization 'org_abc'",
    "instance": "req_01j9xmfa2k",
    "request_id": "req_01j9xmfa2k"
}
```

| Field | Type | Always Present | Description |
|---|---|---|---|
| `type` | string | Yes | URI identifying the error type (machine-readable) |
| `title` | string | Yes | Short, human-readable summary |
| `status` | int | Yes | HTTP status code |
| `detail` | string | Yes | Human-readable explanation |
| `instance` | string | Yes | Request ID for correlation |
| `request_id` | string | Yes | Same as instance — duplicate for convenience |
| `fields` | array | 422 only | Field-level validation errors |
| Additional keys | varies | Per error type | Error-specific context (e.g., `retry_after` for 429) |

---

## 2. Exception Hierarchy

Located at `packages/core/exceptions.py`.

```python
from typing import Optional, Any


class AppError(Exception):
    """Base exception for all application errors.

    All error instances carry structured context for the RFC 7807
    Problem Details response. Subclasses set their own status_code
    and error code.

    Attributes:
        message: Human-readable error message.
        code: Machine-readable error code (e.g., "RESOURCE_NOT_FOUND").
        status_code: HTTP status code for the response.
        detail: Detailed description (may differ from message).
        context: Additional context for logging/debug (never exposed to client).
    """

    def __init__(
        self,
        message: str,
        code: str = "INTERNAL_ERROR",
        status_code: int = 500,
        detail: Optional[str] = None,
        context: Optional[dict] = None,
    ) -> None:
        self.message = message
        self.code = code
        self.status_code = status_code
        self.detail = detail or message
        self.context = context or {}
        super().__init__(self.message)


class NotFoundError(AppError):
    """Resource not found (HTTP 404)."""

    def __init__(
        self,
        detail: str,
        code: str = "RESOURCE_NOT_FOUND",
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(
            message=detail,
            code=code,
            status_code=404,
            detail=detail,
            context=context,
        )


class ValidationError(AppError):
    """Request validation failed (HTTP 422)."""

    def __init__(
        self,
        detail: str,
        code: str = "VALIDATION_ERROR",
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(
            message=detail,
            code=code,
            status_code=422,
            detail=detail,
            context=context,
        )


class AuthenticationError(AppError):
    """Authentication failed — missing or invalid API key/JWT (HTTP 401)."""

    def __init__(
        self,
        detail: str = "Authentication required",
        code: str = "UNAUTHORIZED",
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(
            message=detail,
            code=code,
            status_code=401,
            detail=detail,
            context=context,
        )


class AuthorizationError(AppError):
    """Authenticated but not permitted to access the resource (HTTP 403)."""

    def __init__(
        self,
        detail: str,
        code: str = "FORBIDDEN",
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(
            message=detail,
            code=code,
            status_code=403,
            detail=detail,
            context=context,
        )


class ConflictError(AppError):
    """Resource conflict (HTTP 409) — e.g., duplicate external_id."""

    def __init__(
        self,
        detail: str,
        code: str = "RESOURCE_CONFLICT",
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(
            message=detail,
            code=code,
            status_code=409,
            detail=detail,
            context=context,
        )


class RateLimitError(AppError):
    """Rate limit exceeded (HTTP 429)."""

    def __init__(
        self,
        detail: str = "Rate limit exceeded. Try again later.",
        code: str = "RATE_LIMIT_EXCEEDED",
        retry_after_seconds: int = 60,
        context: Optional[dict] = None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            message=detail,
            code=code,
            status_code=429,
            detail=detail,
            context=context,
        )


class ExternalServiceError(AppError):
    """An external service (LLM, graph DB, etc.) returned an error (HTTP 502)."""

    def __init__(
        self,
        detail: str,
        service_name: str,
        code: str = "EXTERNAL_SERVICE_ERROR",
        context: Optional[dict] = None,
    ) -> None:
        self.service_name = service_name
        context = context or {}
        context["service_name"] = service_name
        super().__init__(
            message=f"{service_name}: {detail}",
            code=code,
            status_code=502,
            detail=detail,
            context=context,
        )


class InsufficientCreditsError(AppError):
    """User/organization has insufficient credits (HTTP 402)."""

    def __init__(
        self,
        detail: str = "Insufficient credits to perform this operation.",
        code: str = "INSUFFICIENT_CREDITS",
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(
            message=detail,
            code=code,
            status_code=402,
            detail=detail,
            context=context,
        )


class PayloadTooLargeError(AppError):
    """Request body exceeds maximum allowed size (HTTP 413)."""

    def __init__(
        self,
        detail: str = "Request body exceeds maximum allowed size.",
        code: str = "PAYLOAD_TOO_LARGE",
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(
            message=detail,
            code=code,
            status_code=413,
            detail=detail,
            context=context,
        )


class SessionClosedError(AppError):
    """Operation attempted on a closed session (HTTP 409)."""

    def __init__(
        self,
        detail: str,
        code: str = "SESSION_CLOSED",
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(
            message=detail,
            code=code,
            status_code=409,
            detail=detail,
            context=context,
        )
```

---

## 3. Global Exception Handler

Located at `core/exceptions.py` (same file, after the hierarchy).

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError
from typing import Union
import structlog
import traceback

logger = structlog.get_logger("OpenZep.api")


def register_exception_handlers(app: FastAPI) -> None:
    """Register all global exception handlers on the FastAPI app.

    Call this during app creation after middleware is registered.
    The order matters: more specific handlers first, catch-all last.
    """

    @app.exception_handler(AppError)
    async def app_error_handler(
        request: Request, exc: AppError,
    ) -> JSONResponse:
        """Handle all known application errors (AppError subclasses).

        Maps to the appropriate HTTP status code and formats the
        response as RFC 7807 Problem Details.

        Logs the error with context for observability, but does NOT
        expose internal context in the response body.
        """
        logger.warning(
            "http.app_error",
            error_code=exc.code,
            status_code=exc.status_code,
            request_id=getattr(request.state, "request_id", None),
            detail=exc.detail,
            **exc.context,
        )
        return _problem_response(
            request=request,
            status_code=exc.status_code,
            title=_code_to_title(exc.code),
            detail=exc.detail,
            error_code=exc.code,
            extra=_get_extra_for_code(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        """Handle FastAPI/Pydantic request validation errors (HTTP 422).

        Returns field-level error details so clients can identify
        exactly which fields failed validation and why.
        """
        errors = _format_validation_errors(exc.errors())
        logger.info(
            "http.validation_error",
            request_id=getattr(request.state, "request_id", None),
            field_errors=errors,
        )
        return _problem_response(
            request=request,
            status_code=422,
            title="Validation Error",
            detail="One or more fields failed validation.",
            error_code="VALIDATION_ERROR",
            extra={"fields": errors},
        )

    @app.exception_handler(PydanticValidationError)
    async def pydantic_validation_handler(
        request: Request, exc: PydanticValidationError,
    ) -> JSONResponse:
        """Handle Pydantic model validation errors (HTTP 422).

        Separate handler for Pydantic's native ValidationError
        (used in service layer for manual validation).
        """
        errors = _format_validation_errors(exc.errors())
        return _problem_response(
            request=request,
            status_code=422,
            title="Validation Error",
            detail="One or more fields failed validation.",
            error_code="VALIDATION_ERROR",
            extra={"fields": errors},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception,
    ) -> JSONResponse:
        """Catch-all for unhandled exceptions (HTTP 500).

        CRITICAL:
        - Logs the FULL traceback for debugging
        - Returns a GENERIC message to the client
        - NEVER exposes stack traces, internal paths, or exception details
        """
        request_id = getattr(request.state, "request_id", "unknown")

        logger.error(
            "http.unhandled_exception",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
        )

        return _problem_response(
            request=request,
            status_code=500,
            title="Internal Server Error",
            detail="An unexpected error occurred. Please try again later. "
                   f"Reference: {request_id}",
            error_code="INTERNAL_ERROR",
        )


def _problem_response(
    request: Request,
    status_code: int,
    title: str,
    detail: str,
    error_code: str,
    extra: Union[dict, None] = None,
    headers: Union[dict, None] = None,
) -> JSONResponse:
    """Build an RFC 7807 Problem Details response.

    The response format is:
    ```json
    {
        "type": "https://api.OpenZep.dev/errors/{error_code}",
        "title": "Resource Not Found",
        "status": 404,
        "detail": "User ... not found ...",
        "instance": "req_01j9xmf...",
        "request_id": "req_01j9xmf..."
    }
    ```

    Always includes:
    - type (URI to error documentation)
    - title (human-readable)
    - status (HTTP status code)
    - detail (human-readable explanation)
    - instance (request_id for correlation)
    - request_id (duplicate for convenience)
    """
    request_id = getattr(request.state, "request_id", None)

    body = {
        "type": f"https://api.OpenZep.dev/errors/{error_code.lower()}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request_id,
        "request_id": request_id,
    }

    if extra:
        body.update(extra)

    headers = headers or {}

    # Preserve CORS headers for error responses
    if "access-control-allow-origin" not in headers:
        origin = request.headers.get("origin")
        if origin:
            # Reflect the origin (safer than wildcard for credentialed requests)
            headers["access-control-allow-origin"] = origin
            headers["vary"] = "Origin"

    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=headers,
    )


def _code_to_title(error_code: str) -> str:
    """Convert an error code like 'RESOURCE_NOT_FOUND' to
    a human-readable title like 'Resource Not Found'."""
    return error_code.replace("_", " ").title()


def _get_extra_for_code(exc: AppError) -> dict:
    """Add error-specific extra fields to the response body.

    For example, RateLimitError adds retry_after_seconds.
    """
    extra = {}
    if isinstance(exc, RateLimitError):
        extra["retry_after_seconds"] = exc.retry_after_seconds
    return extra
```

---

## 4. Validation Error Formatting

```python
def _format_validation_errors(errors: list[dict]) -> list[dict]:
    """Format Pydantic validation errors into a client-friendly structure.

    Input (Pydantic internal format):
    ```python
    [
        {
            "loc": ("body", "external_id"),
            "msg": "String should have at most 255 characters",
            "type": "string_too_long",
            "input": "a" * 256
        }
    ]
    ```

    Output (client-friendly):
    ```python
    [
        {
            "field": "external_id",
            "message": "String should have at most 255 characters",
            "code": "string_too_long"
        }
    ]
    ```
    """
    formatted = []
    for error in errors:
        # Extract field name from location tuple like ("body", "external_id")
        loc = error.get("loc", [])
        field_parts = [str(p) for p in loc if p != "body"]
        field = ".".join(field_parts) if field_parts else "unknown"

        formatted.append({
            "field": field,
            "message": error.get("msg", "Validation error"),
            "code": error.get("type", "invalid"),
        })
    return formatted
```

---

## 5. Error Code Catalogue

Every error response includes a `type` URI and a machine-readable error code. Below is the complete catalogue.

| HTTP | Error Code | type URI | Description | When It Occurs |
|---|---|---|---|---|
| 400 | `INVALID_CURSOR` | `/errors/invalid_cursor` | Cursor format is invalid or malformed | Pagination cursor decode failure |
| 400 | `INVALID_REQUEST` | `/errors/invalid_request` | Request is malformed in a way not covered by schema validation | General bad request that passes schema but fails domain validation |
| 401 | `UNAUTHORIZED` | `/errors/unauthorized` | Missing or invalid API key | Auth header missing, malformed, or key not found |
| 401 | `KEY_EXPIRED` | `/errors/key_expired` | API key has expired | `api_keys.expires_at` has passed |
| 401 | `KEY_REVOKED` | `/errors/key_revoked` | API key has been revoked | Key soft-deleted or marked inactive |
| 402 | `INSUFFICIENT_CREDITS` | `/errors/insufficient_credits` | No credits remaining for operation | Credit/billing check fails |
| 403 | `FORBIDDEN` | `/errors/forbidden` | Authenticated but not permitted | Cross-tenant access attempt or missing scope |
| 404 | `RESOURCE_NOT_FOUND` | `/errors/resource_not_found` | Requested resource does not exist | User, session, fact, or node lookup miss |
| 409 | `RESOURCE_CONFLICT` | `/errors/resource_conflict` | Resource already exists | Duplicate `external_id` on create |
| 409 | `SESSION_CLOSED` | `/errors/session_closed` | Operation on a closed session | Adding messages to an auto-closed session |
| 413 | `PAYLOAD_TOO_LARGE` | `/errors/payload_too_large` | Request body exceeds size limit | Message content > 64KB or body > 5MB |
| 422 | `VALIDATION_ERROR` | `/errors/validation_error` | Request validation failed | Schema validation, content length, metadata depth |
| 429 | `RATE_LIMIT_EXCEEDED` | `/errors/rate_limit_exceeded` | Rate limit exceeded | Per-key or per-IP rate limit hit |
| 500 | `INTERNAL_ERROR` | `/errors/internal_error` | Unexpected server error | Unhandled exception (no details leaked to client) |
| 502 | `EXTERNAL_SERVICE_ERROR` | `/errors/external_service_error` | Upstream service failure | LLM API down, graph DB unreachable |
| 503 | `SERVICE_UNAVAILABLE` | `/errors/service_unavailable` | Service temporarily unavailable | Readiness check failed, DB connection lost |

---

## 6. 429 Rate Limit Response with Retry-After

When a rate limit is exceeded, the response includes:
- HTTP 429 status code
- `Retry-After` header (seconds until the window resets)
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers

```python
# middleware/rate_limit.py (excerpt)

from fastapi import Request
from fastapi.responses import JSONResponse
import time


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token bucket rate limiter per API key.

    Uses Redis as the backing store with a sliding window counter.
    """

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for public endpoints
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # Determine rate limit key (API key if authenticated, else IP)
        api_key = getattr(request.state, "api_key", None)
        client_ip = request.client.host if request.client else "unknown"
        key = api_key or f"ip:{client_ip}"

        # Check rate limit
        allowed, remaining, reset_at = await self._check_rate_limit(key)

        if not allowed:
            retry_after = max(1, int(reset_at - time.time()))
            return JSONResponse(
                status_code=429,
                content={
                    "type": "https://api.OpenZep.dev/errors/rate_limit_exceeded",
                    "title": "Rate Limit Exceeded",
                    "status": 429,
                    "detail": f"Rate limit exceeded. Retry after {retry_after} seconds.",
                    "instance": getattr(request.state, "request_id", None),
                    "request_id": getattr(request.state, "request_id", None),
                    "retry_after_seconds": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self._default_limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(reset_at)),
                },
            )

        response = await call_next(request)

        # Add rate limit headers to successful responses
        response.headers["X-RateLimit-Limit"] = str(self._default_limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining - 1))
        response.headers["X-RateLimit-Reset"] = str(int(reset_at))

        return response

    async def _check_rate_limit(self, key: str) -> tuple[bool, int, float]:
        """Check if request is within rate limit.

        Uses a sliding window counter in Redis.
        Window: 60 seconds (configurable).
        Limit: configurable per key type.
        """
        redis = self._redis
        now = time.time()
        window = 60
        limit = self._default_limit

        bucket = f"ratelimit:{key}:{int(now / window)}"
        count = await redis.incr(bucket)
        if count == 1:
            await redis.expire(bucket, window * 2)

        reset_at = (int(now / window) + 1) * window
        return count <= limit, max(0, limit - count), reset_at
```

---

## 7. 500 Error Handling — No Stack Trace in Response

The catch-all handler ensures that **no internal details** are ever leaked to the client:

```python
# ✅ Correct: logs full traceback, returns generic message
logger.error("http.unhandled_exception", extra={
    "request_id": request_id,
    "method": request.method,
    "path": request.url.path,
    "error_type": type(exc).__name__,
    "error": str(exc),
    "traceback": traceback.format_exc(),  # Logged internally, NOT returned
})

return _problem_response(
    status_code=500,
    title="Internal Server Error",
    detail="An unexpected error occurred. Please try again later. "
           f"Reference: {request_id}",  # Only the request_id for correlation
    error_code="INTERNAL_ERROR",
)

# ❌ NEVER DO THIS:
# return JSONResponse({"detail": str(exc), "traceback": traceback.format_exc()})
```

---

## 8. Security: Never Log Sensitive Data

```python
# middleware/logging.py — request body redaction helpers

import re

SECRET_PATTERNS = [
    re.compile(r"(?i)(password|secret|token|api_key|authorization|credit_card|ssn|pan|dob)"),
]

# List of headers that are NEVER logged
SENSITIVE_HEADERS = frozenset({
    "authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
    "x-csrf-token",
})


def sanitize_headers_for_logging(headers: dict) -> dict:
    """Redact sensitive headers before logging.

    Authorization values are always redacted.
    Other headers matching secret patterns are redacted.
    """
    sanitized = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADERS:
            sanitized[k] = "[REDACTED]"
        elif SECRET_PATTERNS.search(k):
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = v
    return sanitized


def should_log_body(content_type: str) -> bool:
    """Determine if a request body can be safely logged.

    Only log structured data types. Never log raw text
    that may contain PII or conversation content.
    """
    safe_types = {
        "application/json",
        "application/x-www-form-urlencoded",
    }
    return content_type.lower() in safe_types


def sanitize_body_for_logging(body: Any) -> dict:
    """Sanitize a request body for logging.

    Rules:
    - Never log the full body content (may contain PII/messages)
    - Only log: content-type, size, and field names (not values)
    - Log a marker so operators know body was intentionally omitted
    """
    return {
        "_logged_body": False,
        "_note": "Request body not logged for security — see structured logs",
        "_content_type": getattr(body, "content_type", None),
        "_size_bytes": len(str(body)) if body else 0,
    }
```

---

## 9. Service Layer Usage Pattern

Services raise typed exceptions and the global handler converts them to RFC 7807 responses:

```python
# In service layer (example from UserService)

from core.exceptions import NotFoundError, ConflictError


class UserService:
    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo

    async def get_user(self, user_id: UUID, organization_id: UUID) -> UserResponse:
        user = await self._repo.get_by_id(user_id, organization_id)
        if not user:
            # Raise a typed exception — the global handler converts to RFC 7807
            raise NotFoundError(
                detail=(
                    f"User '{user_id}' not found "
                    f"in organization '{organization_id}'"
                ),
                context={
                    "user_id": str(user_id),
                    "organization_id": str(organization_id),
                },
            )
        return UserResponse.model_validate(user)

    async def create_user(
        self, request: CreateUserRequest, organization_id: UUID,
    ) -> UserResponse:
        existing = await self._repo.get_by_external_id(
            request.external_id, organization_id,
        )
        if existing:
            raise ConflictError(
                detail=(
                    f"User with external_id '{request.external_id}' "
                    f"already exists in this organization."
                ),
                context={
                    "external_id": request.external_id,
                    "existing_user_id": str(existing.id),
                },
            )
        user = await self._repo.create(
            external_id=request.external_id,
            organization_id=organization_id,
            name=request.name,
            email=request.email,
            metadata=request.metadata or {},
        )
        return UserResponse.model_validate(user)
```

---

## 10. Dependency for Request ID Injection

```python
# dependencies/request_context.py

from fastapi import Request


def get_request_id(request: Request) -> str:
    """FastAPI dependency: inject request_id into service layer.

    Usage:
        @router.get("/users/{user_id}")
        async def get_user(
            user_id: UUID,
            service: UserService = Depends(get_user_service),
            request_id: str = Depends(get_request_id),
        ) -> UserResponse:
            ...
    """
    return getattr(request.state, "request_id", "unknown")
```

---

## 11. Error Response Examples

### 11.1 404 — Resource Not Found

```json
{
    "type": "https://api.OpenZep.dev/errors/resource_not_found",
    "title": "Resource Not Found",
    "status": 404,
    "detail": "User '550e8400-e29b-41d4-a716-446655440000' not found in organization 'org_abc'",
    "instance": "req_01j9xmfa2k",
    "request_id": "req_01j9xmfa2k"
}
```

### 11.2 422 — Validation Error

```json
{
    "type": "https://api.OpenZep.dev/errors/validation_error",
    "title": "Validation Error",
    "status": 422,
    "detail": "One or more fields failed validation.",
    "instance": "req_01j9xmfabc",
    "request_id": "req_01j9xmfabc",
    "fields": [
        {
            "field": "external_id",
            "message": "String should have at most 255 characters",
            "code": "string_too_long"
        },
        {
            "field": "metadata",
            "message": "Metadata exceeds maximum depth of 5 levels",
            "code": "metadata_too_deep"
        }
    ]
}
```

### 11.3 429 — Rate Limit Exceeded

```json
{
    "type": "https://api.OpenZep.dev/errors/rate_limit_exceeded",
    "title": "Rate Limit Exceeded",
    "status": 429,
    "detail": "Rate limit exceeded. Retry after 45 seconds.",
    "instance": "req_01j9xmfd12",
    "request_id": "req_01j9xmfd12",
    "retry_after_seconds": 45
}
```

### 11.4 500 — Internal Error

```json
{
    "type": "https://api.OpenZep.dev/errors/internal_error",
    "title": "Internal Server Error",
    "status": 500,
    "detail": "An unexpected error occurred. Please try again later. Reference: req_01j9xmfxyz",
    "instance": "req_01j9xmfxyz",
    "request_id": "req_01j9xmfxyz"
}
```

---

## 12. Error Flow Through the System

```
Client                    FastAPI                    Middleware              Service Layer
  │                         │                          │                       │
  │ GET /v1/users/nonexist  │                          │                       │
  │ ──────────────────────► │                          │                       │
  │                         │ RequestID middleware      │                       │
  │                         │ ────────────────────────► │                      │
  │                         │ ◄── request_id assigned ─ │                       │
  │                         │                          │                       │
  │                         │ Auth middleware           │                       │
  │                         │ ────────────────────────► │                      │
  │                         │ ◄── OK (authenticated) ── │                       │
  │                         │                          │                       │
  │                         │ Routing to handler        │                       │
  │                         │ ────────────────────────────────────────────────► │
  │                         │                          │                       │
  │                         │                          │   raise NotFoundError  │
  │                         │ ◄──────────────────────────────────────────────── │
  │                         │                          │                       │
  │                         │ Global exception handler  │                       │
  │                         │ catches NotFoundError     │                       │
  │                         │ maps to 404 + RFC 7807   │                       │
  │                         │ logs with context        │                       │
  │                         │                          │                       │
  │ ◄── 404 Problem JSON ── │                          │                       │
  │    {type, title,        │                          │                       │
  │     status, detail,     │                          │                       │
  │     instance}           │                          │                       │
```

---

## 13. Testing Error Handling

```python
@pytest.mark.asyncio
async def test_404_error_format(async_client: AsyncClient, auth_headers: dict) -> None:
    """Verify 404 error follows RFC 7807 Problem Details format."""
    response = await async_client.get(
        "/v1/users/00000000-0000-0000-0000-000000000000",
        headers=auth_headers,
    )
    assert response.status_code == 404
    data = response.json()

    # RFC 7807 fields
    assert data["type"] == "https://api.OpenZep.dev/errors/resource_not_found"
    assert data["title"] == "Resource Not Found"
    assert data["status"] == 404
    assert "detail" in data
    assert "instance" in data
    assert data["instance"].startswith("req_")


@pytest.mark.asyncio
async def test_validation_error_with_field_detail(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify 422 returns field-level error details."""
    response = await async_client.post(
        "/v1/users",
        json={"external_id": ""},  # Empty string — fails min_length
        headers=auth_headers,
    )
    assert response.status_code == 422
    data = response.json()

    assert data["type"] == "https://api.OpenZep.dev/errors/validation_error"
    assert "fields" in data
    assert len(data["fields"]) > 0
    assert data["fields"][0]["field"] == "external_id"


@pytest.mark.asyncio
async def test_401_missing_auth(async_client: AsyncClient) -> None:
    """Verify missing auth returns 401 with correct error type."""
    response = await async_client.get("/v1/users")
    assert response.status_code == 401
    data = response.json()
    assert data["type"] == "https://api.OpenZep.dev/errors/unauthorized"


@pytest.mark.asyncio
async def test_500_no_stack_trace_leak(
    async_client: AsyncClient, auth_headers: dict, monkeypatch,
) -> None:
    """CRITICAL: Verify 500 errors don't leak internal details."""

    async def broken_handler(*args, **kwargs):
        raise RuntimeError("Internal sensitive detail: DB password=secret123")

    # Inject the broken handler
    # (exact path depends on router structure; adjust as needed)
    import services.api.routers.users as users_router
    monkeypatch.setattr(users_router, "list_users", broken_handler)

    response = await async_client.get("/v1/users", headers=auth_headers)
    assert response.status_code == 500
    data = response.json()
    assert "password" not in data["detail"].lower()
    assert "traceback" not in data
    assert "RuntimeError" not in data["detail"]
    assert data["type"] == "https://api.OpenZep.dev/errors/internal_error"


@pytest.mark.asyncio
async def test_rate_limit_retry_after_header(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify 429 includes Retry-After and rate limit headers."""
    # Fire requests until rate limited (assuming limit is 100/min)
    for _ in range(101):
        await async_client.get("/v1/users", headers=auth_headers)

    response = await async_client.get("/v1/users", headers=auth_headers)
    assert response.status_code == 429
    data = response.json()

    # Retry-After header
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) > 0

    # Rate limit headers
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert "X-RateLimit-Limit" in response.headers
    assert "X-RateLimit-Reset" in response.headers

    # RFC 7807 body
    assert data["type"] == "https://api.OpenZep.dev/errors/rate_limit_exceeded"
    assert data["status"] == 429
    assert "retry_after_seconds" in data


@pytest.mark.asyncio
async def test_409_conflict_error(async_client: AsyncClient, auth_headers: dict) -> None:
    """Verify duplicate resource creation returns 409."""
    # Create a user
    response = await async_client.post(
        "/v1/users",
        json={"external_id": "duplicate_test"},
        headers=auth_headers,
    )
    assert response.status_code == 201

    # Attempt to create the same user again
    response = await async_client.post(
        "/v1/users",
        json={"external_id": "duplicate_test"},
        headers=auth_headers,
    )
    assert response.status_code == 409
    data = response.json()
    assert data["type"] == "https://api.OpenZep.dev/errors/resource_conflict"
    assert "already exists" in data["detail"].lower()


@pytest.mark.asyncio
async def test_413_payload_too_large(async_client: AsyncClient, auth_headers: dict) -> None:
    """Verify oversized payloads return 413 at ingress level."""
    oversized = {"data": "x" * (6 * 1024 * 1024)}  # 6MB (over 5MB limit)
    response = await async_client.post(
        "/v1/users",
        json=oversized,
        headers=auth_headers,
    )
    # This should be caught by the ingress layer or FastAPI body limit
    assert response.status_code in (413, 422)
```

---

## 14. Post-Mortem Template for Error Incidents

When investigating production errors:

1. **Find the request_id** in error reports → search logs
2. **Check the traceback** in structured logs (logged, not in response)
3. **Identify the error code** to determine error category
4. **Check upstream services** if `EXTERNAL_SERVICE_ERROR`
5. **Verify rate limit config** if `RATE_LIMIT_EXCEEDED`
6. **Check for schema migrations** if `VALIDATION_ERROR` on previously working endpoints

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
