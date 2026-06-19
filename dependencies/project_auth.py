"""Project auth dependency — verifies the authenticated user is a project member.

Provides ``require_project_membership`` which can be used as a FastAPI
``Depends`` to guard any project-scoped endpoint.  Supports two modes:

- **Default**: requires the user to have *any* role in the project.
- **``require_owner``**: additionally checks that the user's role is ``"owner"``.

Usage::

    from dependencies.project_auth import require_project_membership
    from fastapi import Depends

    @router.get("/projects/{project_id}/sessions")
    async def list_sessions(
        project_id: UUID,
        _: None = Depends(require_project_membership),
    ):
        ...
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Path, Request, status

from core.exceptions import NotFoundError
from repositories.project_repository import ProjectRepository
from dependencies.db import get_db
from sqlalchemy.ext.asyncio import AsyncSession


async def require_project_membership(
    request: Request,
    project_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Unified authentication + authorization for project-scoped endpoints.

    Verifies that:
    1. The request has a valid authenticated user (401 if missing).
    2. The organization ID is present (401 if missing).
    3. The project exists within the organization (404 if missing).
    4. The authenticated user is a member of the project (403 if not).

    Use this as the sole auth dependency for all ``/v1/projects/...``
    endpoints — it replaces both ``require_org_id`` and a separate
    membership check.

    Raises:
        HTTPException 401: If the user is not authenticated.
        HTTPException 403: If the user is not a project member.
        HTTPException 404: If the project does not exist.
    """
    user_id: str | None = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    org_id: str | None = getattr(request.state, "org_id", None)
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organization context is required",
        )

    repo = ProjectRepository(db)
    project = await repo.get_by_id(
        organization_id=org_id,
        project_id=project_id,
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    member = await repo.get_member(
        project_id=project_id,
        user_id=UUID(user_id),
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this project",
        )


async def require_project_owner(
    request: Request,
    project_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Verify the authenticated user is an owner of the given project.

    Like ``require_project_membership`` but additionally checks the
    ``owner`` role.

    Raises:
        HTTPException 401: If the user is not authenticated.
        HTTPException 403: If the user is not a project owner.
        HTTPException 404: If the project does not exist.
    """
    user_id: str | None = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    repo = ProjectRepository(db)
    project = await repo.get_by_id(
        organization_id=request.state.org_id,
        project_id=project_id,
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    member = await repo.get_member(
        project_id=project_id,
        user_id=UUID(user_id),
    )
    if member is None or member.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project owner access required",
        )
