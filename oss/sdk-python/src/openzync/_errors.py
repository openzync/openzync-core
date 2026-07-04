"""Exception hierarchy matching the OpenZync API's RFC 7807 error responses.

Every API error is mapped to a typed exception so callers can handle
specific error conditions without inspecting raw response bodies.
"""

from __future__ import annotations

from typing import Any


class OpenZyncError(Exception):
    """Base exception for all OpenZync SDK errors."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."
    detail: dict[str, Any] | None = None

    def __init__(
        self,
        message: str | None = None,
        detail: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        if message is not None:
            self.message = message
        if detail is not None:
            self.detail = detail
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


class AuthenticationError(OpenZyncError):
    """Missing or invalid API key."""

    status_code: int = 401
    code: str = "authentication_error"
    message: str = "Authentication is required."


class AuthorizationError(OpenZyncError):
    """Authenticated but insufficient permissions."""

    status_code: int = 403
    code: str = "authorization_error"
    message: str = "You do not have permission to perform this action."


class NotFoundError(OpenZyncError):
    """Requested resource does not exist."""

    status_code: int = 404
    code: str = "not_found"
    message: str = "The requested resource was not found."


class ConflictError(OpenZyncError):
    """Resource already exists or is in a conflicting state."""

    status_code: int = 409
    code: str = "conflict"
    message: str = "The request conflicts with the current state of the resource."


class PayloadTooLargeError(OpenZyncError):
    """Request body exceeds the maximum allowed size."""

    status_code: int = 413
    code: str = "payload_too_large"
    message: str = "The request body is too large."


class ValidationError(OpenZyncError):
    """Request payload failed validation."""

    status_code: int = 422
    code: str = "validation_error"
    message: str = "The request payload is invalid."


class RateLimitError(OpenZyncError):
    """Client exceeded rate-limit allowance."""

    status_code: int = 429
    code: str = "rate_limit_exceeded"
    message: str = "Too many requests. Please slow down."


class ExternalServiceError(OpenZyncError):
    """External dependency (LLM, DB, etc.) returned an error or timed out."""

    status_code: int = 502
    code: str = "external_service_error"
    message: str = "An external service error occurred."


class EntityNotFoundError(OpenZyncError):
    """Requested graph entity node does not exist."""

    status_code: int = 404
    code: str = "entity_not_found"
    message: str = "The requested entity was not found in the knowledge graph."


class GraphTimeoutError(OpenZyncError):
    """Graph database operation exceeded the configured timeout."""

    status_code: int = 504
    code: str = "graph_timeout"
    message: str = "The graph database operation timed out."


# ── Error mapping ──────────────────────────────────────────────────────────

STATUS_TO_EXCEPTION: dict[int, type[OpenZyncError]] = {
    401: AuthenticationError,
    403: AuthorizationError,
    404: NotFoundError,
    409: ConflictError,
    413: PayloadTooLargeError,
    422: ValidationError,
    429: RateLimitError,
    502: ExternalServiceError,
    504: GraphTimeoutError,
}


def raise_on_error(status_code: int, body: dict) -> None:
    """Map an RFC 7807 error response to the appropriate exception.

    Args:
        status_code: HTTP status code.
        body: Parsed JSON response body (should contain ``detail``).

    Raises:
        The mapped ``OpenZyncError`` subclass, or ``OpenZyncError`` as fallback.
    """
    exc_cls = STATUS_TO_EXCEPTION.get(status_code, OpenZyncError)
    raise exc_cls(
        message=body.get("detail", exc_cls.message),
        detail={k: v for k, v in body.items() if k not in ("detail", "status")},
        status_code=status_code,
    )
