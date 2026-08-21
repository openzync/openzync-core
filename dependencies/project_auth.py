"""Project auth dependency — verifies the authenticated user is a project member.

Provides ``require_project_membership`` which can be used as a FastAPI
``Depends`` to guard any project-scoped endpoint.  Requires the user to
have *any* role in the project (JWT) or the API key to be scoped to the
project.

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
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002 (runtime import — FastAPI resolves annotation names)
)

from dependencies.db import get_db
from repositories.project_repository import ProjectRepository


async def require_project_membership(
    request: Request,
    project_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Unified authentication + authorization for project-scoped endpoints.

    Supports two authentication modes:

    **JWT (dashboard user)**: Verifies:
    1. The request has a valid authenticated user (401 if missing).
    2. The organization ID is present (401 if missing).
    3. The project exists within the organization (404 if missing).
    4. The authenticated user is a member of the project (403 if not).

    **API key**: Skips user-level checks; instead verifies:
    1. The organization ID is present (401 if missing).
    2. The API key is scoped to the requested project (403 if mismatch).

    Use this as the sole auth dependency for all ``/v1/projects/...``
    endpoints — it replaces both ``require_org_id`` and a separate
    membership check.

    Raises:
        HTTPException 401: If neither JWT nor API key auth is present.
        HTTPException 403: If the credential lacks project access.
        HTTPException 404: If the project does not exist.
    """
    org_id: str | None = getattr(request.state, "org_id", None)
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Organization context is required",
        )

    auth_type: str | None = getattr(request.state, "auth_type", None)

    # ── API key auth: skip user/membership checks, verify project scope ──
    if auth_type == "api_key":
        api_key_project_id: str | None = getattr(
            request.state, "api_key_project_id", None
        )
        if api_key_project_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key is not scoped to any project. "
                "Create a project-scoped API key.",
            )
        if UUID(api_key_project_id) != project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API key is scoped to a different project",
            )
        # Project existence is verified implicitly — a project_id FK
        # constraint guarantees it, and the API key itself was created
        # against a real project.
        return

    # ── JWT (dashboard user) auth ────────────────────────────────────────
    user_id: str | None = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
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
