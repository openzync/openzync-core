"""Context assembly endpoint — HTTP adapter layer only.

Provides:
- ``GET /v1/projects/{project_id}/context`` — assemble a context block for
  LLM injection from a natural-language query, scoped to a project.

Every handler is a thin adapter that:
1. Extracts input from the request (path params, query params).
2. Calls the service layer.
3. Returns a Pydantic response.

No business logic. No database queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.db import get_db
from dependencies.org_config import get_org_config
from dependencies.project_auth import require_project_membership
from packages.graph_backend.postgres import PostgresGraphBackend
from schemas.context import ContextResponse
from schemas.organization_config import OrgConfigBase
from services.context_service import ContextService

router = APIRouter(
    prefix="/v1/projects/{project_id}/context",
    tags=["Memory"],
)


@router.get(
    "",
    response_model=ContextResponse,
    summary="Assemble context block for LLM injection",
    description="Assemble a context block for a project from a natural-language "
    "query.  The context is assembled from recent episodes, extracted facts, "
    "and knowledge-graph entities via hybrid search (vector + BM25 + RRF). "
    "Results are cached in Redis for 30 seconds.",
    responses={
        200: {"description": "Context block assembled successfully."},
        401: {"description": "Missing or invalid authentication."},
        403: {"description": "Not a member of this project."},
        422: {"description": "Validation error (e.g., empty query)."},
    },
)
async def get_context(
    request: Request,
    query: str = Query(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural-language query describing the context needed.",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Maximum items per source type (1–100).",
    ),
    format: str = Query(  # noqa: A002
        default="text",
        pattern=r"^(text|json)$",
        description='Output format — "text" for plain-text or "json" '
        "for structured JSON.",
    ),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_project_membership),
    org_config: OrgConfigBase = Depends(get_org_config),
    response: Response = None,  # type: ignore[assignment]
) -> ContextResponse:
    """Get an assembled context block for LLM injection.

    The context is assembled from the project's memory using a hybrid
    retrieval pipeline:

    1. **Vector search** (pgvector cosine similarity) — semantic matching.
    2. **BM25 search** (PostgreSQL full-text ``ts_rank``) — keyword matching.
    3. **Graph BFS** (entity-relationship traversal) — graph-aware context.
    4. **RRF merge** — Reciprocal Rank Fusion across all three sources.

    Results are formatted either as plain text (with section headers,
    source labels, and provenance metadata) or as a structured JSON
    object (with typed arrays per source category).  The formatted
    result is cached in Redis for 30 seconds — subsequent identical
    queries return instantaneously.

    Args:
        request: The FastAPI request object — used to access
            ``request.app.state.redis`` and org/project IDs.
        query: A natural-language query describing the context needed.
        limit: Maximum items per source type.
        format: Output format (``"text"`` or ``"json"``).
        db: An async SQLAlchemy session (injected).
        org_config: Org-level configuration (injected).

    Returns:
        A ``ContextResponse`` with the assembled context string and
        assembly metadata (cache status, timing, source counts).
    """
    org_id = UUID(request.state.org_id)
    project_id = UUID(request.path_params["project_id"])

    # ── Assemble context ────────────────────────────────────────────────
    redis = getattr(request.app.state, "redis", None) if request else None
    graph_backend = PostgresGraphBackend(db=db)
    service = ContextService(
        db, org_id, redis, graph_backend=graph_backend, org_config=org_config
    )
    result = await service.assemble(
        project_id=project_id,
        query=query,
        limit=limit,
        format=format,
    )

    # Set X-Cache header for observability
    is_cache_hit = (
        result.get("metadata", {}).get("cache_hit", False)
        if result.get("metadata")
        else False
    )
    response.headers["X-Cache"] = "HIT" if is_cache_hit else "MISS"

    return ContextResponse(
        context=result["context"],
        metadata=result.get("metadata"),
    )
