# Error Handling Implementation Guide

> **Phase:** Phase 0 — Foundation (Week 1-2)
> **Priority:** P0
> **Requirements:** (Cross-cutting — all endpoints)
> **Handoff from:** Architect (ADR-002: Error Handling & RFC 7807)

---

## 1. Overview

MemGraph uses **RFC 7807 Problem Details** as the standard error response format across all API endpoints. This provides a consistent, machine-readable error structure that clients can parse programmatically.

All errors flow through a single global exception handler that:
1. Catches known `AppError` subclasses → maps to appropriate HTTP status
2. Catches Pydantic `ValidationError` → returns 422 with field-level detail
3. Catches unknown exceptions → logs full traceback, returns generic 500

---

## 2. Exception Hierarchy

Located at `packages/core/exceptions.py`.

```python
from typing import Optional, Any


class AppError(Exception):
    """Base exception for all application errors.

    All error instances carry structured context for the RFC 7807
    Problem Details response.
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
    """Resource not found."""

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
    """Request validation failed."""

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
    """Authentication failed (missing or invalid API key/JWT)."""

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
    """Authenticated but not permitted to access the resource."""

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
    """Resource conflict (e.g., duplicate external_id)."""

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
    """Rate limit exceeded."""

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
    """An external service (LLM, graph DB, etc.) returned an error."""

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
    """User/organization has insufficient credits."""

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
    """Request body exceeds maximum allowed size."""

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


class SessonClosedError(AppError):
    """Operation on a closed session."""

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

Located at `core/exceptions_handlers.py`.

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError as PydanticValidationError
from typing import Union
import logging
import traceback

logger = logging.getLogger("memgraph.api")


def register_exception_handlers(app: FastAPI) -> None:
    """Register all global exception handlers on the FastAPI app."""

    @app.exception_handler(AppError)
    async def app_error_handler(
        request: Request, exc: AppError,
    ) -> JSONResponse:
        """Handle all known application errors (AppError subclasses)."""
        return _problem_response(
            request=request,
            status_code=exc.status_code,
            title=exc.code.replace("_", " ").title(),
            detail=exc.detail,
            error_code=exc.code,
            extra=exc.context,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        """Handle FastAPI/Pydantic request validation errors.

        Returns 422 with field-level error details.
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

    @app.exception_handler(PydanticValidationError)
    async def pydantic_validation_handler(
        request: Request, exc: PydanticValidationError,
    ) -> JSONResponse:
        """Handle Pydantic model validation errors."""
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
        """Catch-all for unhandled exceptions.

        Logs the full traceback and returns a generic 500 response.
        NEVER includes stack trace or internal details in the response.
        """
        request_id = getattr(request.state, "request_id", "unknown")

        logger.error(
            "http.unhandled_exception",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )

        return _problem_response(
            request=request,
            status_code=500,
            title="Internal Server Error",
            detail="An unexpected error occurred. Please try again later.",
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

    Format:
    ```json
    {
        "type": "https://api.memgraph.dev/errors/{error_code}",
        "title": "Resource Not Found",
        "status": 404,
        "detail": "User user_123 not found in organization org_abc",
        "instance": "req_01j9xmf...",
        "request_id": "req_01j9xmf..."
    }
    ```
    """
    request_id = getattr(request.state, "request_id", None)

    body = {
        "type": f"https://api.memgraph.dev/errors/{error_code.lower()}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request_id,
        "request_id": request_id,
    }

    if extra:
        body.update(extra)

    headers = headers or {}
    # Ensure CORS headers are preserved
    if "access-control-allow-origin" not in headers:
        origin = request.headers.get("origin")
        if origin:
            headers["access-control-allow-origin"] = origin

    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=headers,
    )
```

---

## 4. Validation Error Formatting

```python
def _format_validation_errors(errors: list[dict]) -> list[dict]:
    """Format Pydantic validation errors into a client-friendly structure.

    Input (Pydantic internal):
    ```python
    [
        {
            "loc": ("body", "external_id"),
            "msg": "String should have at most 255 characters",
            "type": "string_too_long",
            "input": "..."
        }
    ]
    ```

    Output:
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
        # Extract field name from location tuple
        loc = error.get("loc", [])
        # Skip "body" prefix in locations like ("body", "external_id")
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

| HTTP | Error Code | type URI | Description | When it occurs |
|---|---|---|---|---|
| 400 | `INVALID_CURSOR` | `/errors/invalid_cursor` | Cursor format is invalid or malformed | Pagination cursor decode failure |
| 400 | `INVALID_REQUEST` | `/errors/invalid_request` | Request is malformed | General bad request |
| 401 | `UNAUTHORIZED` | `/errors/unauthorized` | Missing or invalid API key | Auth header missing, malformed, or key invalid |
| 401 | `KEY_EXPIRED` | `/errors/key_expired` | API key has expired | `api_keys.expires_at` has passed |
| 401 | `KEY_REVOKED` | `/errors/key_revoked` | API key has been revoked | Key soft-deleted or marked inactive |
| 403 | `FORBIDDEN` | `/errors/forbidden` | Authenticated but not permitted | Cross-tenant access attempt |
| 402 | `INSUFFICIENT_CREDITS` | `/errors/insufficient_credits` | No credits remaining for operation | Credit/billing check fails |
| 404 | `RESOURCE_NOT_FOUND` | `/errors/resource_not_found` | Requested resource does not exist | User, session, fact, or node lookup miss |
| 409 | `RESOURCE_CONFLICT` | `/errors/resource_conflict` | Resource already exists | Duplicate `external_id` on create |
| 409 | `SESSION_CLOSED` | `/errors/session_closed` | Operation on a closed session | Adding messages to an auto-closed session |
| 413 | `PAYLOAD_TOO_LARGE` | `/errors/payload_too_large` | Request body exceeds size limit | Message content > 64KB or body > 5MB |
| 422 | `VALIDATION_ERROR` | `/errors/validation_error` | Request validation failed | Schema validation, content length, metadata depth |
| 429 | `RATE_LIMIT_EXCEEDED` | `/errors/rate_limit_exceeded` | Rate limit exceeded | Per-key or per-IP rate limit hit |
| 500 | `INTERNAL_ERROR` | `/errors/internal_error` | Unexpected server error | Unhandled exception (no details leaked) |
| 502 | `EXTERNAL_SERVICE_ERROR` | `/errors/external_service_error` | Upstream service failure | LLM API down, graph DB unreachable |
| 503 | `SERVICE_UNAVAILABLE` | `/errors/service_unavailable` | Service temporarily unavailable | Readiness check failed, DB connection lost |

---

## 6. Rate Limit Error with Retry-After

```python
# middleware/rate_limit.py

from fastapi import Request
from fastapi.responses import JSONResponse
from core.exceptions import RateLimitError


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token bucket rate limiter per API key.

    Adds Retry-After header on 429 responses.
    Uses Redis as the backing store for rate limit counters.
    """

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for public endpoints
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # Determine rate limit key
        api_key = getattr(request.state, "api_key", None)
        client_ip = request.client.host if request.client else "unknown"
        key = api_key or client_ip

        # Check rate limit
        allowed, remaining, reset_at = await self._check_rate_limit(key)

        if not allowed:
            retry_after = int((reset_at - time.time()))
            return JSONResponse(
                status_code=429,
                content={
                    "type": "https://api.memgraph.dev/errors/rate_limit_exceeded",
                    "title": "Rate Limit Exceeded",
                    "status": 429,
                    "detail": f"Rate limit exceeded. Retry after {retry_after} seconds.",
                    "instance": getattr(request.state, "request_id", None),
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self._default_limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(reset_at)),
                },
            )

        response = await call_next(request)

        # Add rate limit headers
        response.headers["X-RateLimit-Limit"] = str(self._default_limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining - 1)
        response.headers["X-RateLimit-Reset"] = str(int(reset_at))

        return response

    async def _check_rate_limit(self, key: str) -> tuple[bool, int, float]:
        """Check if request is within rate limit.

        Uses a sliding window counter in Redis.
        """
        redis = self._redis
        now = time.time()
        window = 60  # 1-minute window
        limit = self._default_limit  # e.g., 100

        bucket = f"ratelimit:{key}:{int(now / window)}"
        count = await redis.incr(bucket)
        if count == 1:
            await redis.expire(bucket, window * 2)

        reset_at = (int(now / window) + 1) * window
        return count <= limit, max(0, limit - count), reset_at
```

---

## 7. 500 Error Handling — No Stack Trace in Response

The catch-all handler in `register_exception_handlers` (above) ensures:

```python
# ✅ Correct: logs full traceback, returns generic message
logger.error("http.unhandled_exception", extra={
    "traceback": traceback.format_exc(),  # Logged, not returned
})
return _problem_response(
    status_code=500,
    title="Internal Server Error",
    detail="An unexpected error occurred. Please try again later.",
    error_code="INTERNAL_ERROR",
)

# ❌ Never: includes internal details in response
# return JSONResponse({"detail": str(exc), "traceback": ...})
```

---

## 8. Security: Never Log Request Bodies with Secrets

```python
# middleware/logging.py

import re

SECRET_PATTERNS = [
    re.compile(r"(?i)(password|secret|token|api_key|authorization|credit_card|ssn)"),
]


def sanitize_request_for_logging(headers: dict, body: Any) -> dict:
    """Redact sensitive headers and body fields before logging."""
    sanitized_headers = {
        k: "[REDACTED]" if SECRET_PATTERNS.search(k) else v
        for k, v in headers.items()
    }

    # Never log full request body — only log content-type and size
    sanitized_body = {
        "_size": len(str(body)) if body else 0,
        "_note": "Request body not logged for security",
    }

    return {"headers": sanitized_headers, "body": sanitized_body}
```

---

## 9. Testing Error Handling

```python
@pytest.mark.asyncio
async def test_404_error_format(async_client: AsyncClient, auth_headers: dict) -> None:
    response = await async_client.get(
        "/v1/users/00000000-0000-0000-0000-000000000000",
        headers=auth_headers,
    )
    assert response.status_code == 404
    data = response.json()

    # RFC 7807 format
    assert data["type"] == "https://api.memgraph.dev/errors/resource_not_found"
    assert data["title"] == "Resource Not Found"
    assert data["status"] == 404
    assert "detail" in data
    assert "instance" in data
    assert data["instance"].startswith("req_")


@pytest.mark.asyncio
async def test_validation_error_with_field_detail(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    response = await async_client.post(
        "/v1/users",
        json={"external_id": ""},  # Empty string — fails min_length
        headers=auth_headers,
    )
    assert response.status_code == 422
    data = response.json()

    assert data["type"] == "https://api.memgraph.dev/errors/validation_error"
    assert "fields" in data
    assert len(data["fields"]) > 0
    assert data["fields"][0]["field"] == "external_id"


@pytest.mark.asyncio
async def test_401_missing_auth(async_client: AsyncClient) -> None:
    response = await async_client.get("/v1/users")
    assert response.status_code == 401
    data = response.json()
    assert data["type"] == "https://api.memgraph.dev/errors/unauthorized"


@pytest.mark.asyncio
async def test_500_no_stack_trace_leak(
    async_client: AsyncClient, auth_headers: dict, monkeypatch,
) -> None:
    """Verify 500 errors don't leak internal details."""

    async def broken_handler(*args, **kwargs):
        raise RuntimeError("Internal sensitive detail")

    monkeypatch.setattr("services.api.routers.users.router", broken_handler)

    # This should return 500 without exposing the RuntimeError detail
    response = await async_client.get("/v1/users", headers=auth_headers)
    assert response.status_code == 500
    data = response.json()
    assert "sensitive" not in data["detail"].lower()
    assert "traceback" not in data
    assert "RuntimeError" not in data["detail"]


@pytest.mark.asyncio
async def test_rate_limit_retry_after_header(
    async_client: AsyncClient, auth_headers: dict,
) -> None:
    """Verify 429 includes Retry-After header."""
    # Make requests until rate limited
    for _ in range(101):  # Assuming limit is 100/min
        await async_client.get("/v1/users", headers=auth_headers)

    response = await async_client.get("/v1/users", headers=auth_headers)
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert response.headers["X-RateLimit-Remaining"] == "0"
```

---

## 10. Integration with Service Layer

Services raise exceptions and the global handler maps them:

```python
# In service layer (example from UserService):

async def get_user(self, user_id: UUID, organization_id: UUID) -> UserResponseWithStats:
    user = await self._repo.get_by_id(user_id, organization_id)
    if not user:
        # Raise a typed exception — the global handler converts to RFC 7807
        raise NotFoundError(
            f"User '{user_id}' not found in organization '{organization_id}'",
            context={"user_id": str(user_id), "organization_id": str(organization_id)},
        )
    return await self._build_response_with_stats(user)


async def create_user(self, ...) -> UserResponseWithStats:
    existing = await self._repo.get_by_external_id(request.external_id, organization_id)
    if existing:
        raise ConflictError(
            f"User with external_id '{request.external_id}' already exists.",
            context={"external_id": request.external_id},
        )
    # ...
```

---

## 11. FastAPI Dependency for Exception Context

```python
# dependencies/error_context.py

from fastapi import Request


def get_request_id(request: Request) -> str:
    """FastAPI dependency: inject request_id into service layer."""
    return getattr(request.state, "request_id", "unknown")
```

---

## 12. Error Response Examples

### 12.1 404 — User Not Found

```json
{
    "type": "https://api.memgraph.dev/errors/resource_not_found",
    "title": "Resource Not Found",
    "status": 404,
    "detail": "User '550e8400-e29b-41d4-a716-446655440000' not found in organization 'org_abc'",
    "instance": "req_01j9xmfa2k",
    "request_id": "req_01j9xmfa2k"
}
```

### 12.2 422 — Validation Error

```json
{
    "type": "https://api.memgraph.dev/errors/validation_error",
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

### 12.3 429 — Rate Limit Exceeded

```json
{
    "type": "https://api.memgraph.dev/errors/rate_limit_exceeded",
    "title": "Rate Limit Exceeded",
    "status": 429,
    "detail": "Rate limit exceeded. Retry after 45 seconds.",
    "instance": "req_01j9xmfd12",
    "request_id": "req_01j9xmfd12"
}
```

### 12.4 500 — Internal Error

```json
{
    "type": "https://api.memgraph.dev/errors/internal_error",
    "title": "Internal Server Error",
    "status": 500,
    "detail": "An unexpected error occurred. Please try again later.",
    "instance": "req_01j9xmfxyz",
    "request_id": "req_01j9xmfxyz"
}
```

---

## 13. Sequence: Error Through the System

```
Client                    FastAPI                    Middleware              Service Layer
  │                         │                          │                       │
  │ GET /v1/users/nonexist  │                          │                       │
  │ ──────────────────────► │                          │                       │
  │                         │ Auth middleware           │                       │
  │                         │ ────────────────────────► │                      │
  │                         │ ◄── OK (authenticated) ── │                       │
  │                         │                          │                       │
  │                         │ Routing to handler        │                       │
  │                         │ ────────────────────────────────────────────────► │
  │                         │                          │                       │
  │                         │                          │    NotFoundError      │
  │                         │ ◄──────────────────────────────────────────────── │
  │                         │                          │                       │
  │                         │ Global exception handler  │                       │
  │                         │ catches NotFoundError     │                       │
  │                         │ maps to 404 + RFC 7807    │                       │
  │                         │                          │                       │
  │ ◄── 404 Problem JSON ── │                          │                       │
  │    {type, title,        │                          │                       │
  │     status, detail,     │                          │                       │
  │     instance}           │                          │                       │
```

---

*Document maintained by Rohan · TheLinkAI · Last updated: 2026-06-05*
