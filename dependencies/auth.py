"""FastAPI dependencies for authentication and authorization.

Provides three levels of auth dependency:

1. ``get_org_id`` — Optional auth.  Returns the org ID if authenticated,
   ``None`` otherwise.  Use for endpoints that behave differently for
   authenticated vs. anonymous users.

2. ``require_org_id`` — Mandatory auth.  Raises 401 if not authenticated.
   Use for all endpoints that require a valid API key / session.

3. ``require_scope(scope_name)`` — Dependency factory.  Checks that the
   authenticated API key has a specific scope.  Raises 403 if missing.

All dependencies rely on ``request.state.org_id`` and
``request.state.api_key_scopes`` set by :class:`AuthMiddleware
<memgraph.middleware.auth.AuthMiddleware>`.
"""

from __future__ import annotations

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
    API key.  If no authentication was provided (public endpoint), this
    returns ``None``.

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

    This dependency **must** be used on any endpoint that requires
    authentication.  It raises a 401 error if the request has no valid
    API key.

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
                "type": "https://errors.memgraph.dev/authentication_error",
                "title": "Authentication Required",
                "status": 401,
                "detail": (
                    "A valid API key is required for this endpoint. "
                    "Provide it via the Authorization: Bearer <key> header."
                ),
            },
        )
    return org_id


def require_scope(required_scope: str):
    """Dependency factory that checks for a specific API key scope.

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
        scopes: list[str] = getattr(request.state, "api_key_scopes", [])
        if required_scope not in scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "type": "https://errors.memgraph.dev/authorization_error",
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
