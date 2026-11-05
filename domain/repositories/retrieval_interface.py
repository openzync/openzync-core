"""Retrieval repository interface — vector + ranked query contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


class RetrievalRepository(Protocol):
    """Persistence contract for hybrid-retrieval vector legs.

    Implementations own the pgvector SQL (including the HNSW-compatible
    ``ORDER BY embedding <=>`` form). Services pass resolved values
    only — never SQL fragments.
    """

    async def vector_search_episodes(
        self,
        embedding: list[float],
        project_id: UUID,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return nearest-neighbour episodes for a query embedding."""
        ...

    async def vector_search_facts(
        self,
        embedding: list[float],
        project_id: UUID,
        limit: int,
        effective_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return nearest-neighbour facts valid at ``effective_time``."""
        ...

    async def ranked_execute(self, stmt: Any) -> list[dict[str, Any]]:
        """Execute a ranked SELECT and map rows to dicts (score rounded)."""
        ...
