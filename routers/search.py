"""Search endpoint — HTTP adapter layer only.

Provides:
- ``GET /v1/users/{user_id}/search`` — hybrid search across a user's
  memory (episodes, facts, entities).

Every handler is a thin adapter that:
1. Extracts input from the request (path params, query params).
2. Resolves the user to verify existence and org ownership.
3. Calls the service layer.
4. Returns the raw search results.

No business logic. No database queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from dependencies.auth import require_org_id
from dependencies.db import get_db
from repositories.user_repository import UserRepository
from services.hybrid_retriever import HybridRetriever

router = APIRouter(
    prefix="/v1/users/{user_id}/search",
    tags=["Memory"],
)


@router.get(
    "",
    summary="Hybrid search across user memory",
    description="Search across a user's memory using hybrid retrieval "
    "(vector + BM25 + RRF).  Returns episodes, facts, and optionally "
    "entities matching the query.  Results can be filtered by type "
    "using the ``types`` parameter.",
    responses={
        200: {"description": "Search results returned successfully."},
        401: {"description": "Missing or invalid authentication."},
        404: {"description": "User not found in organization."},
        422: {"description": "Validation error (e.g., empty query)."},
    },
)
async def search_memory(
    request: Request,
    user_id: UUID,
    query: str = Query(
        ...,
        min_length=1,
        max_length=2000,
        description="Search query string.",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Maximum results per source type (1–100).",
    ),
    types: str = Query(
        default="episodes,facts",
        description="Comma-separated list of result types to include. "
        "Valid values: ``episodes``, ``facts``, ``entities``, "
        "``communities``.",
    ),
    db: AsyncSession = Depends(get_db),
    org_id: str = Depends(require_org_id),
) -> dict:
    """Hybrid search across a user's memory.

    Searches across the user's episodes (conversation history), extracted
    facts (knowledge triplets), and knowledge-graph entities using a
    three-legged hybrid retrieval pipeline:

    1. **Vector search** — pgvector cosine similarity for semantic matching.
    2. **BM25 search** — PostgreSQL full-text ``ts_rank`` for keyword matching.
    3. **RRF merge** — Reciprocal Rank Fusion to combine and rank results.

    Use the ``types`` parameter to filter which result categories to
    include (default: ``"episodes,facts"``).  Pass ``"entities"`` to
    also include graph entity results (requires a configured graph
    backend).

    Args:
        user_id: The UUID of the user to search across.
        query: The search query string.
        limit: Maximum results per source type.
        types: Comma-separated result type filter.
        db: An async SQLAlchemy session (injected).
        org_id: The authenticated organization ID (injected).

    Returns:
        A dict with ``query`` (the original query), ``results`` (the
        filtered and merged result list), and ``total`` (the count of
        results returned).

    Raises:
        NotFoundError: If the user does not exist in the organization.
    """
    org_uuid = UUID(org_id)

    # ── Resolve user ────────────────────────────────────────────────────
    # Verify the user exists and belongs to the authenticated organization.
    user_repo = UserRepository(db)
    user = await user_repo.get_by_uuid(org_uuid, user_id)
    if user is None:
        raise NotFoundError(
            f"User {user_id} not found in organization {org_id}",
        )

    # ── Run hybrid search ───────────────────────────────────────────────
    graph_backend = getattr(request.app.state, "graph_backend", None)
    retriever = HybridRetriever(db, org_uuid, graph_backend=graph_backend)
    results = await retriever.hybrid_search(
        query=query,
        user_id=user_id,
        limit=limit,
    )

    # ── Filter by requested types ───────────────────────────────────────
    # The types parameter is a comma-separated string of source category
    # names (e.g. "episodes,facts").  Only the requested categories are
    # included in the response.
    type_filter = set(t.strip() for t in types.split(","))
    data: list[dict] = []

    for source_type in type_filter:
        data.extend(results.get(source_type, []))

    return {
        "query": query,
        "results": data,
        "total": len(data),
    }
