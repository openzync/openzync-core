"""Global search router — ``GET /v1/search`` endpoint.

Scoped to the authenticated user's organization and membership.  Returns
matching projects, users, and sessions as a flat sorted list.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import get_current_user_id, require_org_id
from dependencies.db import get_db
from schemas.search import GlobalSearchResponse
from services.global_search_service import GlobalSearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/search", tags=["Search"])


@router.get(
    "",
    summary="Global search across resources",
    description="Search across projects, users, and sessions within your organization.",
    responses={
        200: {"description": "Search results returned successfully."},
        401: {"description": "Missing or invalid authentication."},
        422: {"description": "Validation error (e.g., empty query)."},
    },
)
async def global_search(
    request: Request,  # noqa: ARG001 — kept for consistency with existing patterns
    query: str = Query(..., alias="q", min_length=1, max_length=200, description="Search query string."),
    limit: int = Query(default=10, ge=1, le=50, description="Maximum results."),
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
    user_id: UUID = Depends(get_current_user_id),
) -> GlobalSearchResponse:
    """Search across the organization for projects, users, and sessions.

    Results are scoped to resources the authenticated user can access:

    * **Projects** — the user must be a project member.
    * **Users** — any user in the same organization.
    * **Sessions** — the user must be a member of the session's project.

    Args:
        request: The incoming HTTP request (unused, kept for pattern consistency).
        query: The search query (1–200 characters).
        limit: Maximum results to return (1–50, default 10).
        db: An async SQLAlchemy session (injected).
        org_id: The authenticated organization ID (injected).
        user_id: The authenticated user's UUID (injected).

    Returns:
        A :class:`GlobalSearchResponse` with matching results and the
        original query string.
    """
    service = GlobalSearchService(db, UUID(org_id), user_id)
    results = await service.search(query, limit=limit)
    return GlobalSearchResponse(results=results, query=query)
