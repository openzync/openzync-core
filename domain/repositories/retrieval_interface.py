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

    def build_episode_vector_stmt(
        self,
        embedding: list[float],
        project_id: UUID,
        limit: int,
    ) -> Any:
        """Build the episode nearest-neighbour statement (no I/O)."""
        ...

    def build_fact_vector_stmt(
        self,
        embedding: list[float],
        project_id: UUID,
        limit: int,
        effective_time: datetime | None = None,
    ) -> Any:
        """Build the fact nearest-neighbour statement (no I/O)."""
        ...

    async def ranked_execute(self, stmt: Any) -> list[dict[str, Any]]:
        """Execute a ranked SELECT and map rows to dicts (score rounded)."""
        ...
