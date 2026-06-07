"""Context assembly endpoint — HTTP adapter layer only.

Provides:
- ``GET /v1/users/{user_id}/context`` — assemble a context block for LLM
  injection from a natural-language query.

Every handler is a thin adapter that:
1. Extracts input from the request (path params, query params).
2. Resolves the user to verify existence and org ownership.
3. Calls the service layer.
4. Returns a Pydantic response.

No business logic. No database queries.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import NotFoundError
from dependencies.auth import require_org_id
from dependencies.db import get_db
from packages.graphiti_client.backends.postgres import PostgresGraphBackend
from repositories.user_repository import UserRepository
from schemas.context import ContextResponse
from services.context_service import ContextService

router = APIRouter(
    prefix="/v1/users/{user_id}/context",
    tags=["Memory"],
)


@router.get(
    "",
    response_model=ContextResponse,
    summary="Assemble context block for LLM injection",
    description="Assemble a context block for a user from a natural-language "
    "query.  The context is assembled from recent episodes, extracted facts, "
    "and knowledge-graph entities via hybrid search (vector + BM25 + RRF). "
    "Results are cached in Redis for 30 seconds.",
    responses={
        200: {"description": "Context block assembled successfully."},
        401: {"description": "Missing or invalid authentication."},
        404: {"description": "User not found in organization."},
        422: {"description": "Validation error (e.g., empty query)."},
    },
)
async def get_context(
    user_id: UUID,
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
    org_id: str = Depends(require_org_id),
    response: Response = None,  # type: ignore[assignment]
    request: Request = None,  # type: ignore[assignment]
    # NOTE: ``request`` is injected via FastAPI's dependency resolver.
    # The default ``None`` is never used — it is always populated by
    # the framework when the endpoint is called.
) -> ContextResponse:
    """Get an assembled context block for LLM injection.

    The context is assembled from the user's memory using a hybrid
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
        user_id: The UUID of the user to retrieve context for.
        query: A natural-language query describing the context needed.
        limit: Maximum items per source type.
        format: Output format (``"text"`` or ``"json"``).
        db: An async SQLAlchemy session (injected).
        org_id: The authenticated organization ID (injected).
        request: The FastAPI request object — used to access
            ``request.app.state.redis``.

    Returns:
        A ``ContextResponse`` with the assembled context string and
        assembly metadata (cache status, timing, source counts).

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

    # ── Assemble context ────────────────────────────────────────────────
    redis = getattr(request.app.state, "redis", None) if request else None
    graph_backend = PostgresGraphBackend(db=db)
    service = ContextService(db, org_uuid, redis, graph_backend=graph_backend)
    result = await service.assemble(
        user_id=user_id,
        query=query,
        limit=limit,
        format=format,
    )

    # Set X-Cache header for observability
    is_cache_hit = result.get("metadata", {}).get("cache_hit", False) if result.get("metadata") else False
    response.headers["X-Cache"] = "HIT" if is_cache_hit else "MISS"

    return ContextResponse(
        context=result["context"],
        metadata=result.get("metadata"),
    )
