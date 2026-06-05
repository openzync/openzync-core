"""Structured logging configuration with structlog.

Provides:
- ``setup_logging()`` — one-time initialisation that configures structlog
  processors and standard-library ``logging`` integration.
- ``add_pii_redaction()`` — processor that redacts sensitive fields before
  they reach a renderer.
- ``bind_request_context()`` — convenience for binding global context vars
  (request_id, org_id, user_id).

Usage:

    from core.logging import setup_logging, bind_request_context
    from core.config import settings

    setup_logging(
        environment=settings.ENVIRONMENT,
        log_level=settings.LOG_LEVEL,
    )

    # Inside a middleware / dependency:
    bind_request_context(
        request_id="abc-123",
        org_id="org-42",
        user_id="usr-7",
    )

    # Any subsequent structlog.get_logger() call will include those fields.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog
from structlog.dev import ConsoleRenderer
from structlog.processors import JSONRenderer

# ── PII redaction ─────────────────────────────────────────────────────────────

SENSITIVE_KEYS: set[str] = {
    "key",
    "secret",
    "password",
    "token",
    "auth",
    "authorization",
    "api_key",
    "api_key_name",
    "access_token",
    "refresh_token",
    "client_secret",
    "private_key",
}
"""Lowercased set of field-name fragments that indicate sensitive data."""

_sensitive_pattern = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in SENSITIVE_KEYS) + r")\b",
    re.IGNORECASE,
)


def add_pii_redaction(
    logger: structlog.types.WrappedLogger,  # noqa: ARG001
    method_name: str,  # noqa: ARG001
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Redact sensitive fields from log events before rendering.

    Any key whose name contains a recognised sensitive fragment (e.g. "key",
    "secret", "password", "token", "auth") will have its value replaced with
    ``"***REDACTED***"``.

    Args:
        logger: The wrapped logger instance (unused).
        method_name: The log method called (unused).
        event_dict: The mutable event dictionary.

    Returns:
        The *event_dict* with sensitive values redacted in-place.
    """
    for key in list(event_dict.keys()):
        if _sensitive_pattern.search(key):
            event_dict[key] = "***REDACTED***"

    return event_dict


# ── Context vars ──────────────────────────────────────────────────────────────

import contextvars  # noqa: E402 (import after _sensitive_keys for clarity)

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
_org_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "org_id", default=""
)
_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "user_id", default=""
)


def bind_request_context(
    request_id: str,
    org_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Bind global request-scoped context variables.

    These values are automatically included in every log entry emitted during
    the request.

    Args:
        request_id: Unique identifier for the current request.
        org_id: Organisation identifier (optional).
        user_id: Authenticated user identifier (optional).
    """
    _request_id.set(request_id)
    if org_id is not None:
        _org_id.set(org_id)
    if user_id is not None:
        _user_id.set(user_id)


def _add_context_from_vars(
    logger: structlog.types.WrappedLogger,  # noqa: ARG001
    method_name: str,  # noqa: ARG001
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Inject context-variable values into the event dict.

    Registered as a processor so that ``request_id``, ``org_id``, and
    ``user_id`` appear on every log line automatically.
    """
    rid = _request_id.get()
    if rid:
        event_dict["request_id"] = rid
    oid = _org_id.get()
    if oid:
        event_dict["org_id"] = oid
    uid = _user_id.get()
    if uid:
        event_dict["user_id"] = uid
    return event_dict


# ── Setup ─────────────────────────────────────────────────────────────────────


def setup_logging(environment: str, log_level: str) -> None:
    """Configure structlog and standard-library logging once at startup.

    Call this **once** during application initialisation (e.g. in the
    lifespan).  Subsequent calls are idempotent.

    * In **production** / **staging**: structured JSON output via
      ``JSONRenderer`` — suitable for log aggregation (CloudWatch, ELK, etc.).
    * In **development**: coloured human-readable output via
      ``ConsoleRenderer``.

    PII redaction runs **before** the renderer so that sensitive data never
    reaches the output stream.

    Args:
        environment: One of ``"development"``, ``"staging"``, ``"production"``.
        log_level: Minimum log level string (e.g. ``"INFO"``, ``"DEBUG"``).
    """
    use_json = environment in ("production", "staging")

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        add_pii_redaction,
        _add_context_from_vars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if use_json:
        renderer: structlog.types.Processor = JSONRenderer()
    else:
        renderer = ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.PositionalArgumentsFormatter(),
            renderer,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Route standard-library logging through structlog so that third-party
    # libraries (uvicorn, sqlalchemy, httpx, etc.) also produce structured
    # output.
    logging.basicConfig(format="%(message)s", level=log_level.upper())
    logging.captureWarnings(True)
