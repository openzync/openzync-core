"""FastAPI dependencies for extracting common request parameters.

Provides reusable validated extractors for ``org_id`` (from auth middleware
state) and ``project_id`` (from path parameters).  Using these instead of
inline ``request.state.org_id`` / ``request.path_params["project_id"]``
ensures consistent validation and error messages across all endpoints.

Usage in a router::

    from dependencies.request import get_current_org_id, get_project_id

    @router.get("/items")
    async def list_items(
        org_id: UUID = Depends(get_current_org_id),
        project_id: UUID = Depends(get_project_id),
    ):
        ...
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Request

from core.exceptions import AuthenticationError, ValidationError


async def get_current_org_id(request: Request) -> UUID:
    """Extract and validate the organization ID from request state.

    The auth middleware sets ``request.state.org_id`` during request
    processing.  Raises ``AuthenticationError`` if the middleware has not
    populated it (e.g., missing or invalid auth context).

    Returns:
        The authenticated organization's UUID.

    Raises:
        AuthenticationError: If ``request.state.org_id`` is missing or empty.
    """
    raw: str | None = getattr(request.state, "org_id", None)
    if not raw:
        raise AuthenticationError("Organization context not set")
    return UUID(raw)


async def get_project_id(request: Request) -> UUID:
    """Extract and validate ``project_id`` from the request path.

    The path parameter is expected to be present on routes with
    ``/{project_id}/...``.  Raises ``ValidationError`` if the parameter is
    missing or empty.

    Returns:
        The project UUID from the URL path.

    Raises:
        ValidationError: If ``project_id`` is missing or empty in the path.
    """
    raw: str | None = request.path_params.get("project_id")
    if not raw:
        raise ValidationError("Project ID is required in path")
    return UUID(raw)
