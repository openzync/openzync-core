"""OpenBao-specific exception hierarchy.

All OpenBao errors raised by :mod:`core.openbao` inherit from
:class:`OpenBaoError` and carry a ``status_code`` attribute for
HTTP-level error mapping.

Usage:

    from core.openbao_exceptions import OpenBaoAuthError

    try:
        await client.read_system_config()
    except OpenBaoAuthError:
        # Token expired / invalid — re-authenticate
        ...
"""

from __future__ import annotations


class OpenBaoError(Exception):
    """Base exception for all OpenBao-related errors.

    Attributes:
        message: Human-readable error description.
        status_code: Optional HTTP status code from the OpenBao API response.
    """

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class OpenBaoConnectionError(OpenBaoError):
    """OpenBao is unreachable, or a network-level error occurred.

    Raised when:
    - The OpenBao server is down or unreachable.
    - The REST API returns a 5xx status.
    - A request times out.
    """


class OpenBaoAuthError(OpenBaoError):
    """Authentication or authorization failure.

    Raised when:
    - AppRole login fails (wrong role_id / secret_id).
    - The client token is expired or invalid.
    - The token lacks sufficient ACL permissions (HTTP 403).
    """


class OpenBaoSecretNotFoundError(OpenBaoError):
    """The requested secret path does not exist.

    Raised when:
    - A KV read targets a key that does not exist (HTTP 404).
    - A KV list targets an empty or non-existent prefix.
    """


class OpenBaoNamespaceError(OpenBaoError):
    """Namespace operation failed.

    Raised when:
    - Namespace creation fails due to naming conflicts.
    - Namespace deletion fails.
    - Operations within a namespace fail (HTTP 412).
    """


class OpenBaoRateLimitError(OpenBaoError):
    """OpenBao returned HTTP 429 — too many requests.

    Raised when the client exceeds OpenBao's rate limit.
    Callers should retry with exponential backoff.
    """
