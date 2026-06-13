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

5. ``require_project_access`` — Verifies that a project belongs to the
   authenticated org and that a user is a member of that project.

All dependencies rely on ``request.state`` attributes set by
:class:`AuthMiddleware <openzep.middleware.auth.AuthMiddleware>`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from models.project import Project
from repositories.project_repository import ProjectRepository

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
                "type": "https://errors.openzep.dev/authentication_error",
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
                    "type": "https://errors.openzep.dev/authorization_error",
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
                "type": "https://errors.openzep.dev/authentication_error",
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
                "type": "https://errors.openzep.dev/authentication_error",
                "title": "Invalid Session",
                "status": 401,
                "detail": "The JWT token does not contain a valid user identifier.",
            },
        )
    return user_id


# ═══════════════════════════════════════════════════════════════════════════════
# Project-level authorization
# ═══════════════════════════════════════════════════════════════════════════════


async def require_project_access(
    request: Request,
    org_id: str = Depends(require_org_id),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Verify that a project belongs to the authenticated org and that
    the user (from JWT or path param) is a project member.

    This dependency should be used on project-scoped endpoints::

        @router.get("/v1/projects/{project_id}/{user_id}/sessions")
        async def list_sessions(
            project_id: UUID,
            user_id: UUID,
            _: str = Depends(require_project_access),
            ...
        )

    The ``project_id`` and ``user_id`` are read from the request path params.
    The ``org_id`` comes from the authentication context.

    Returns:
        The ``org_id`` if access is granted.

    Raises:
        HTTPException: 404 if the project doesn't exist or
            403 if the user is not a member.
    """
    # Extract path parameters
    path_params = request.path_params
    project_id_str: str | None = path_params.get("project_id")
    user_id_str: str | None = path_params.get("user_id")

    if project_id_str is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_id is required in the path",
        )

    project_id = UUID(project_id_str)
    org_uuid = UUID(org_id) if isinstance(org_id, str) else org_id

    # Verify project exists and belongs to the org
    repo = ProjectRepository(db)
    project = await repo.get_by_id(org_uuid, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_id} not found",
        )

    # Verify the authenticated user (JWT or API key owner) is a project member.
    # The dashboard admin (from JWT) manages users' data within the project,
    # so membership is always checked against the authenticated actor, not the
    # path user_id.  For API-key auth the path user_id is the actor.
    auth_type: str | None = getattr(request.state, "auth_type", None)
    if auth_type == "jwt":
        # Dashboard JWT session — check membership for the JWT user
        jwt_user: str | None = getattr(request.state, "user_id", None)
        if jwt_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="JWT token does not contain a user identifier",
            )
        check_user_id = UUID(jwt_user)
    elif user_id_str is not None:
        # API-key auth — the path user_id is the actor
        check_user_id = UUID(user_id_str)
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user_id is required in the path for API-key auth",
        )

    is_member = await repo.is_member(project_id, check_user_id)
    if not is_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User {check_user_id} is not a member of project {project_id}",
        )

    return org_id
