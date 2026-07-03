"""Domain exception hierarchy and FastAPI exception-handler registration.

Every custom exception carries a ``status_code``, a machine-readable ``code``
string, a human-readable ``message``, and an optional ``detail`` dict for
additional context.

Exception handlers are registered via ``register_exception_handlers(app)`` and
return response bodies conforming to **RFC 7807** (Problem Details for HTTP
APIs).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


# ═══════════════════════════════════════════════════════════════════════════════
# Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


class AppError(Exception):
    """Base exception for all OpenZep application errors.

    Subclass this to create domain-specific errors.  Every subclass **must**
    set :attr:`status_code` and :attr:`code`.
    """

    status_code: int = 500
    """HTTP status code returned to the client."""

    code: str = "internal_error"
    """Machine-readable error-code string (e.g. ``"not_found"``)."""

    def __init__(
        self,
        message: str = "An unexpected error occurred.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.detail = detail or {}
        super().__init__(self.message)


class NotFoundError(AppError):
    """Requested resource does not exist."""

    status_code: int = 404
    code: str = "not_found"

    def __init__(
        self,
        message: str = "The requested resource was not found.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class ValidationError(AppError):
    """Request payload failed validation (business-rule level)."""

    status_code: int = 422
    code: str = "validation_error"

    def __init__(
        self,
        message: str = "The request payload is invalid.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class AuthenticationError(AppError):
    """Missing or invalid authentication credentials."""

    status_code: int = 401
    code: str = "authentication_error"

    def __init__(
        self,
        message: str = "Authentication is required.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class AuthorizationError(AppError):
    """Authenticated but insufficient permissions."""

    status_code: int = 403
    code: str = "authorization_error"

    def __init__(
        self,
        message: str = "You do not have permission to perform this action.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class ConflictError(AppError):
    """Resource already exists or is in a conflicting state."""

    status_code: int = 409
    code: str = "conflict"

    def __init__(
        self,
        message: str = "The request conflicts with the current state of the resource.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class RateLimitError(AppError):
    """Client exceeded rate-limit allowance."""

    status_code: int = 429
    code: str = "rate_limit_exceeded"

    def __init__(
        self,
        message: str = "Too many requests.  Please slow down.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class InsufficientCreditsError(AppError):
    """Account balance too low to perform the requested operation."""

    status_code: int = 402
    code: str = "insufficient_credits"

    def __init__(
        self,
        message: str = "Insufficient credits to complete this request.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class ExternalServiceError(AppError):
    """External dependency (LLM, DB, S3, etc.) returned an error or timed out."""

    status_code: int = 502
    code: str = "external_service_error"

    def __init__(
        self,
        message: str = "An external service error occurred.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class LLMConfigurationError(AppError):
    """LLM backend cannot be resolved due to missing or invalid configuration.

    Raised when no LLM backend can be resolved because the per-org config
    is missing required fields (API keys, model names, endpoints, etc.).
    """

    status_code: int = 502
    code: str = "llm_configuration_error"

    def __init__(
        self,
        message: str = "No LLM backend configured.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class PayloadTooLargeError(AppError):
    """Request body exceeds the maximum allowed size."""

    status_code: int = 413
    code: str = "payload_too_large"

    def __init__(
        self,
        message: str = "The request body is too large.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class EntityNotFoundError(AppError):
    """Requested graph entity node does not exist."""

    status_code: int = 404
    code: str = "entity_not_found"

    def __init__(
        self,
        message: str = "The requested entity was not found in the knowledge graph.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class EdgeNotFoundError(AppError):
    """Requested graph edge does not exist."""

    status_code: int = 404
    code: str = "edge_not_found"

    def __init__(
        self,
        message: str = "The requested edge was not found in the knowledge graph.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class EpisodeNotFoundError(AppError):
    """Requested episode does not exist.

    Raised by ARQ workers when an episode is not yet visible due to
    transaction visibility races.  The ``@with_retry`` decorator re-raises
    this so the worker can retry after the transaction commits.
    """

    status_code: int = 404
    code: str = "episode_not_found"

    def __init__(
        self,
        message: str = "The requested episode was not found.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class GraphTimeoutError(AppError):
    """Graph database operation exceeded the configured timeout."""

    status_code: int = 504
    code: str = "graph_timeout"

    def __init__(
        self,
        message: str = "The graph database operation timed out.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class LLMStructuredOutputError(ExternalServiceError):
    """LLM output failed validation against the expected Pydantic model.

    Raised when ``LLMBackend.chat()`` is called with a ``response_model``
    and the response cannot be parsed into that model after exhausting all
    validation retries.  Carries just enough detail to diagnose the failure
    without leaking the full conversation.
    """

    code: str = "llm_structured_output_error"

    def __init__(
        self,
        message: str = "LLM output failed to match the expected schema.",
        *,
        model_name: str = "",
        content_preview: str = "",
        validation_error: str = "",
    ) -> None:
        detail: dict[str, Any] = {}
        if model_name:
            detail["model_name"] = model_name
        if content_preview:
            detail["content_preview"] = content_preview
        if validation_error:
            detail["validation_error"] = validation_error
        super().__init__(message=message, detail=detail)


# ═══════════════════════════════════════════════════════════════════════════════
# Infrastructure failures — zero-fallback domain
# ═══════════════════════════════════════════════════════════════════════════════


class ServiceUnavailableError(AppError):
    """A shared infrastructure component is unavailable.

    Raised when a core internal service (cache, database, rate-limiter,
    metrics backend, graph store, etc.) cannot be reached.  These errors
    are never silently swallowed — they propagate as HTTP 503 so that
    load-balancers and orchestrators can react appropriately.
    """

    status_code: int = 503
    code: str = "service_unavailable"

    def __init__(
        self,
        message: str = "A service dependency is unavailable.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class CacheUnavailableError(ServiceUnavailableError):
    """Cache service (Redis/Memcached) cannot be reached."""

    code: str = "cache_unavailable"

    def __init__(
        self,
        message: str = "Cache service is unavailable.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class GraphBackendUnavailableError(ServiceUnavailableError):
    """Graph database backend cannot be reached."""

    code: str = "graph_backend_unavailable"

    def __init__(
        self,
        message: str = "Graph database backend is unavailable.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class RateLimitUnavailableError(ServiceUnavailableError):
    """Rate-limiting infrastructure (Redis/backend) cannot be reached."""

    code: str = "rate_limit_unavailable"

    def __init__(
        self,
        message: str = "Rate limiting infrastructure is unavailable.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class MetricsUnavailableError(ServiceUnavailableError):
    """Metrics collection backend cannot be reached."""

    code: str = "metrics_unavailable"

    def __init__(
        self,
        message: str = "Metrics service is unavailable.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class DatabaseUnavailableError(ServiceUnavailableError):
    """Primary or replica database cannot be reached."""

    code: str = "database_unavailable"

    def __init__(
        self,
        message: str = "Database is unavailable.",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, detail=detail)


class SearchLegFailedError(ServiceUnavailableError):
    """A single search retrieval leg (vector, keyword, graph, etc.) failed.

    Carries the leg name and the original error detail so callers can
    decide whether to fail the entire multi-leg search or proceed with
    degraded results (the default is to fail — zero fallback).
    """

    code: str = "search_leg_failed"

    def __init__(
        self,
        leg_name: str,
        message: str | None = None,
        original_error: str = "",
    ) -> None:
        detail: dict[str, Any] = {"leg": leg_name}
        if original_error:
            detail["original_error"] = original_error
        super().__init__(
            message=message or f"Search leg '{leg_name}' failed.",
            detail=detail,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# RFC 7807 Problem Details
# ═══════════════════════════════════════════════════════════════════════════════


def _to_problem_json(request: Request, exc: AppError) -> JSONResponse:
    """Convert an ``AppError`` to an RFC 7807 Problem Details response.

    https://www.rfc-editor.org/rfc/rfc7807
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": f"https://errors.openzep.dev/{exc.code}",
            "title": exc.code.replace("_", " ").title(),
            "status": exc.status_code,
            "detail": exc.message,
            "instance": str(request.url.path),
            **exc.detail,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Registration — call once during app creation
# ═══════════════════════════════════════════════════════════════════════════════


def register_exception_handlers(app: FastAPI) -> None:
    """Register exception handlers for the complete ``AppError`` hierarchy.

    Every exception above is mapped to an RFC 7807 JSON body so that API
    consumers receive structured, machine-readable error information.

    Args:
        app: The FastAPI application instance.
    """
    handlers: dict[type[AppError], int] = {
        NotFoundError: 404,
        ValidationError: 422,
        AuthenticationError: 401,
        AuthorizationError: 403,
        ConflictError: 409,
        RateLimitError: 429,
        InsufficientCreditsError: 402,
        ExternalServiceError: 502,
        LLMConfigurationError: 502,
        PayloadTooLargeError: 413,
        EntityNotFoundError: 404,
        EdgeNotFoundError: 404,
        EpisodeNotFoundError: 404,
        GraphTimeoutError: 504,
        # ── Infrastructure failures (503) ────────────────────────────────
        ServiceUnavailableError: 503,
        CacheUnavailableError: 503,
        GraphBackendUnavailableError: 503,
        RateLimitUnavailableError: 503,
        MetricsUnavailableError: 503,
        DatabaseUnavailableError: 503,
        SearchLegFailedError: 503,
    }

    for exc_type, _status in handlers.items():
        # FastAPI expects a callable with signature (request, exc) -> response.
        # We capture exc_type by using it as a default argument in the closure.
        def _handler(
            request: Request,
            exc: AppError,
            _exc_type: type[AppError] = exc_type,  # type: ignore[assignment]
        ) -> JSONResponse:
            if not isinstance(exc, _exc_type):
                # If a subclass matched, fall through to the base handler.
                return _handler(request, exc, AppError)
            return _to_problem_json(request, exc)

        app.add_exception_handler(exc_type, _handler)  # type: ignore[arg-type]

    # Catch-all for any other AppError subclasses that may be defined later.
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _to_problem_json(request, exc)
