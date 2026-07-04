"""FastAPI dependencies for authentication and authorization.

Provides five levels of auth dependency:

1. ``get_org_id`` — Optional auth.  Returns the org ID if authenticated,
   ``None`` otherwise.  Works with both API keys and JWT tokens.

2. ``require_org_id`` — Mandatory auth.  Raises 401 if not authenticated.

3. ``require_scope(scope_name)`` — Dependency factory.  Checks that the
   authenticated API key has a specific scope.  Raises 403 if missing.
   For JWT-authenticated users, scopes are implicitly granted.

4. ``get_dashboard_user`` — Returns ``request.state.user_id`` if the
   request is authenticated via JWT (dashboard session), ``None`` otherwise.

5. ``get_current_user_id`` — Returns the authenticated user's UUID.
   Works with both JWT and API-key auth.  Raises 401 if not authenticated.

All dependencies rely on ``request.state`` attributes set by
:class:`AuthMiddleware <openzync.middleware.auth.AuthMiddleware>`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ═══════════════════════════════════════════════════════════════════════════════
# Bearer token scheme — auto-adds to OpenAPI docs
# ═══════════════════════════════════════════════════════════════════════════════

bearer_scheme = HTTPBearer(auto_error=False)
"""FastAPI ``HTTPBearer`` scheme with ``auto_error=False``.

When used as a dependency, this extracts the ``Authorization: Bearer <token>``
header and populates the OpenAPI docs with a security scheme.  Setting
``auto_error=False`` means it does not raise on missing headers — our
middleware handles that.
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Dependency functions
# ═══════════════════════════════════════════════════════════════════════════════


async def get_org_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),  # noqa: ARG001
) -> str | None:
    """Extract the authenticated organization ID from the request state.

    The ``org_id`` is set by :class:`AuthMiddleware` after verifying the
    API key or JWT token.  If no authentication was provided (public
    endpoint), this returns ``None``.

    The ``credentials`` parameter is included to ensure FastAPI parses the
    ``Authorization`` header and adds it to the OpenAPI schema.  Actual
    authentication is handled by the middleware.

    Args:
        request: The incoming HTTP request.
        credentials: Parsed Bearer credentials (used for OpenAPI schema only).

    Returns:
        The organization UUID string, or ``None`` if not authenticated.
    """
    return getattr(request.state, "org_id", None)


async def require_org_id(
    org_id: str | None = Depends(get_org_id),
) -> str:
    """Require a valid authenticated organization ID.

    Works with both API keys and JWT tokens.  Raises a 401 error if the
    request has no valid authentication.

    Args:
        org_id: The organization ID from :func:`get_org_id`.

    Returns:
        The authenticated organization UUID string.

    Raises:
        HTTPException: 401 if no valid authentication is present.
    """
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Authentication Required",
                "status": 401,
                "detail": (
                    "A valid API key or JWT token is required for this endpoint. "
                    "Provide it via the Authorization: Bearer <token> header."
                ),
            },
        )
    return org_id


def require_scope(required_scope: str):
    """Dependency factory that checks for a specific API key scope.

    For JWT-authenticated dashboard users, all scopes are implicitly
    granted (they have full access to their organization).  For API-key
    authenticated requests, the key's scopes are checked.

    Use this to protect endpoints that require elevated permissions:

    .. code-block:: python

        @router.post("/admin/orgs")
        async def admin_action(
            org_id: str = Depends(require_scope("admin:write")),
        ):
            ...

    Args:
        required_scope: The scope string the API key must possess (e.g.
            ``"admin:write"``, ``"sessions:read"``).

    Returns:
        A dependency callable that returns the ``org_id`` if the scope is
        present, or raises 403.
    """

    async def _scope_checker(
        request: Request,
        org_id: str = Depends(require_org_id),
    ) -> str:
        """Inner dependency that performs the scope check.

        Args:
            request: The incoming HTTP request.
            org_id: The authenticated organization ID.

        Returns:
            The ``org_id`` if scope check passes.

        Raises:
            HTTPException: 403 if the required scope is missing.
        """
        auth_type: str | None = getattr(request.state, "auth_type", None)

        # JWT-authenticated dashboard users have full access
        if auth_type == "jwt":
            return org_id

        scopes: list[str] = getattr(request.state, "api_key_scopes", [])
        if required_scope not in scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "type": "https://errors.openzync.tech/authorization_error",
                    "title": "Insufficient Permissions",
                    "status": 403,
                    "detail": (
                        f"Your API key does not have the required scope: "
                        f"'{required_scope}'.  Available scopes: {scopes}"
                    ),
                },
            )
        return org_id

    return _scope_checker


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard-specific dependencies
# ═══════════════════════════════════════════════════════════════════════════════


async def get_dashboard_user(
    request: Request,
    org_id: str = Depends(require_org_id),
) -> str:
    """Require a JWT-authenticated dashboard user.

    Returns the ``user_id`` from the JWT claims.  Raises 401 if the
    request is authenticated via API key instead of JWT.

    Args:
        request: The incoming HTTP request.
        org_id: The authenticated organization ID (from ``require_org_id``).

    Returns:
        The dashboard user's UUID string.

    Raises:
        HTTPException: 401 if not a JWT-authenticated session.
    """
    auth_type: str | None = getattr(request.state, "auth_type", None)
    if auth_type != "jwt":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Dashboard Authentication Required",
                "status": 401,
                "detail": (
                    "This endpoint requires a JWT token (dashboard session). "
                    "API key authentication is not sufficient."
                ),
            },
        )

    user_id: str | None = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Invalid Session",
                "status": 401,
                "detail": "The JWT token does not contain a valid user identifier.",
            },
        )
    return user_id


async def get_current_user_id(
    request: Request,
    org_id: str = Depends(require_org_id),
) -> UUID:
    """Require an authenticated user and return their UUID.

    Works with both JWT tokens and API keys.  The user ID is extracted
    from ``request.state.user_id`` (set by :class:`AuthMiddleware`).

    Use this dependency in project-scoped endpoints to obtain the
    ``created_by`` value for attribution.

    Args:
        request: The incoming HTTP request.
        org_id: The authenticated organization ID (from ``require_org_id``).

    Returns:
        The authenticated user's UUID.

    Raises:
        HTTPException: 401 if the user is not authenticated.
    """
    user_id: str | None = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "type": "https://errors.openzync.tech/authentication_error",
                "title": "Authentication Required",
                "status": 401,
                "detail": (
                    "A valid user session or API key is required for this endpoint. "
                    "Provide it via the Authorization: Bearer <token> header."
                ),
            },
        )
    return UUID(user_id)
