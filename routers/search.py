"""Search endpoint — HTTP adapter layer only.

Provides:
- ``GET /v1/projects/{project_id}/search`` — hybrid search across a
  project's memory (episodes, facts, entities).

Every handler is a thin adapter that:
1. Extracts input from the request (path params, query params).
2. Calls the service layer.
3. Returns the raw search results.

No business logic. No database queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from dependencies.org_config import get_org_config
from dependencies.project_auth import require_project_membership
from packages.graph_backend.postgres import PostgresGraphBackend
from schemas.organization_config import OrgConfigBase
from services.hybrid_retriever import HybridRetriever

router = APIRouter(
    prefix="/v1/projects/{project_id}/search",
    tags=["Memory"],
)


@router.get(
    "",
    summary="Hybrid search across project memory",
    description="Search across a project's memory using hybrid retrieval "
    "(vector + BM25 + RRF).  Returns episodes, facts, and optionally "
    "entities matching the query.  Results can be filtered by type "
    "using the ``types`` parameter.",
    responses={
        200: {"description": "Search results returned successfully."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        422: {"description": "Validation error (e.g., empty query)."},
    },
)
async def search_memory(
    request: Request,
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
    _: None = Depends(require_project_membership),
    org_config: OrgConfigBase = Depends(get_org_config),
) -> dict:
    """Hybrid search across a project's memory.

    Searches across the project's episodes (conversation history), extracted
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
        request: The FastAPI request object — used to access org/project IDs.
        query: The search query string.
        limit: Maximum results per source type.
        types: Comma-separated result type filter.
        db: An async SQLAlchemy session (injected).
        org_config: Org-level configuration (injected).

    Returns:
        A dict with ``query`` (the original query), ``results`` (the
        filtered and merged result list), and ``total`` (the count of
        results returned).
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    # ── Run hybrid search ───────────────────────────────────────────────
    graph_backend = PostgresGraphBackend(db=db)
    retriever = HybridRetriever(
        db, org_id, graph_backend=graph_backend, org_config=org_config
    )
    results = await retriever.hybrid_search(
        query=query,
        project_id=project_id,
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
